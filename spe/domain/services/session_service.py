"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from spe.domain.clock import Clock
from spe.domain.events import DomainEvent
from spe.domain.extensions.appeal_cases import ExceptionGrant
from spe.domain.ids import IdGenerator
from spe.domain.policy_ast import ApprovedException, PolicyDocument, RestrictionKind
from spe.domain.policy_interpreter import EvalContext, evaluate
from spe.domain.reason_codes import ReasonCode
from spe.domain.repositories import (
    DailyUsageLedger,
    ExceptionGrantRepository,
    HeartbeatSink,
    OutboxRepository,
    PolicyRepository,
    ProfileCorrectionRepository,
    SessionRepository,
)
from spe.domain.services.responses import ActionResult
from spe.domain.session import Session, SessionStatus
from spe.domain.timeutil import age_at, local_day_key, split_watch_window


class ActiveSessionExists(Exception):
    """封装领域状态与业务约束。"""


def _document_with_grants(
    document: PolicyDocument, user_id: str, grants: Sequence[ExceptionGrant]
) -> PolicyDocument:
    """把申诉例外合并进策略文档副本，由评估器按既有豁免规则处理。"""
    merged = document.model_copy(deep=True)
    merged.rules.exceptions.extend(
        ApprovedException(
            exception_id=grant.id,
            subject_user_id=user_id,
            waives=grant.kind,
            approved_by=grant.granted_by,
            reason="appeal-granted exception",
        )
        for grant in grants
    )
    return merged


class SessionService:
    """封装领域状态与业务约束。"""

    def __init__(
        self,
        sessions: SessionRepository,
        policies: PolicyRepository,
        outbox: OutboxRepository,
        clock: Clock,
        ids: IdGenerator,
        ledger: DailyUsageLedger,
        heartbeats: HeartbeatSink | None = None,
        heartbeat_max_gap_seconds: int = 90,
        exceptions: ExceptionGrantRepository | None = None,
        corrections: ProfileCorrectionRepository | None = None,
    ) -> None:
        self._sessions = sessions
        self._policies = policies
        self._outbox = outbox
        self._clock = clock
        self._ids = ids
        self._ledger = ledger
        self._heartbeats = heartbeats
        self._max_gap = heartbeat_max_gap_seconds
        self._exceptions = exceptions
        self._corrections = corrections

    async def _active_grants(
        self, tenant_id: str, user_id: str, now: datetime
    ) -> list[ExceptionGrant]:
        """裁决发放且尚未过期的例外；未配置存储时视为没有。"""
        if self._exceptions is None:
            return []
        return list(await self._exceptions.list_active(tenant_id, user_id, now))

    # -- start ---------------------------------------------------------------

    async def start(
        self,
        tenant_id: str,
        user_id: str,
        birth_date: date,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        """执行确定性的业务处理。"""
        if idempotency_key is not None:
            existing = await self._sessions.get_by_idempotency_key(tenant_id, idempotency_key)
            if existing is not None:
                return ActionResult.success(
                    ReasonCode.SESSION_STARTED_IDEMPOTENT, session=existing
                )

        policy = await self._policies.get_active(tenant_id)
        if policy is None:
            return ActionResult.rejected(ReasonCode.REJECTED_POLICY_NOT_FOUND)

        now = self._clock.now()
        tz = policy.document.rules.timezone

        # 申诉裁决确认的资料纠正优先于客户端上报的出生日期。
        birth = birth_date
        if self._corrections is not None:
            correction = await self._corrections.get(tenant_id, user_id)
            if correction is not None:
                birth = correction.birth_date

        # Evaluate eligibility at start against the authoritative daily ledger
        # (age + bedtime + already-consumed daily budget; no session usage yet).
        local_day = local_day_key(now, tz)
        daily_used = await self._ledger.get_seconds(tenant_id, user_id, local_day)
        ctx = EvalContext(
            now=now,
            user_id=user_id,
            user_age=age_at(birth, now, tz),
            daily_usage_seconds=daily_used,
            session_elapsed_seconds=0,
        )
        document = policy.document
        grants = await self._active_grants(tenant_id, user_id, now)
        if grants:
            document = _document_with_grants(document, user_id, grants)
        decision = evaluate(document, ctx)
        if not decision.allowed:
            return ActionResult.rejected(decision.reason, trace=decision.trace)

        session = Session(
            id=self._ids.new_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            policy_id=policy.id,
            policy_version=policy.version,
            status=SessionStatus.ACTIVE,
            birth_date=birth,
            started_at=now,
            updated_at=now,
        )
        try:
            await self._sessions.add(session, idempotency_key=idempotency_key)
        except ActiveSessionExists:
            # Lost a concurrent race: another start committed first.
            current = await self._sessions.get_active_for_user(tenant_id, user_id)
            return ActionResult.rejected(
                ReasonCode.REJECTED_ACTIVE_SESSION_EXISTS, session=current
            )

        await self._emit("session.started", session, now, version=session.policy_version)
        return ActionResult.success(
            ReasonCode.SESSION_STARTED, session=session, trace=decision.trace
        )

    # -- heartbeat -----------------------------------------------------------

    async def heartbeat(
        self,
        tenant_id: str,
        session_id: str,
        seq: int,
        watched_seconds_total: int,
    ) -> ActionResult:
        """执行确定性的业务处理。"""
        session = await self._sessions.get_for_update(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        if session.status is SessionStatus.ENDED:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_ENDED, session=session)
        if session.status is not SessionStatus.ACTIVE:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_ACTIVE, session=session)

        # Idempotent / ordered guard.
        if seq <= session.last_seq:
            return ActionResult.rejected(
                ReasonCode.HEARTBEAT_IGNORED_STALE,
                session=session,
                received_seq=seq,
                last_seq=session.last_seq,
            )

        now = self._clock.now()
        policy = await self._policies.get_by_id(tenant_id, session.policy_id)
        assert policy is not None  # pinned policy must exist
        tz = policy.document.rules.timezone

        proposed = max(
            0, min(watched_seconds_total - session.watched_seconds_marker, self._max_gap)
        )

        grants = await self._active_grants(session.tenant_id, session.user_id, now)
        waived = frozenset(grant.kind for grant in grants)
        credited, per_day, hit_limit, limit_reason = await self._credit(
            session, policy.document, tz, now, proposed, waived
        )

        # Advance the marker/seq regardless of how much was creditable, so the
        # cumulative accounting stays monotonic and idempotent.
        session.watched_seconds_marker = max(session.watched_seconds_marker, watched_seconds_total)
        session.last_seq = seq
        session.updated_at = now

        if self._heartbeats is not None:
            await self._heartbeats.record(
                tenant_id=tenant_id,
                session_id=session_id,
                seq=seq,
                watched_seconds_total=watched_seconds_total,
                credited_seconds=credited,
                occurred_at=now,
            )

        if hit_limit:
            session.status = SessionStatus.ENDED
            session.ended_at = now
            await self._sessions.save(session)
            await self._emit("session.ended", session, now, cause=limit_reason)
            return ActionResult.rejected(
                ReasonCode.SESSION_ENDED_BY_LIMIT,
                session=session,
                credited_seconds=credited,
                per_day=per_day,
                limit_reason=limit_reason,
            )

        await self._sessions.save(session)
        await self._emit("session.heartbeat", session, now, credited_seconds=credited)
        return ActionResult.success(
            ReasonCode.HEARTBEAT_APPLIED,
            session=session,
            credited_seconds=credited,
            per_day=per_day,
        )

    async def _credit(
        self,
        session: Session,
        document: PolicyDocument,
        timezone: str,
        now: datetime,
        proposed: int,
        waived: frozenset[RestrictionKind],
    ) -> tuple[int, dict[str, int], bool, str | None]:
        """执行确定性的业务处理。"""
        session_cap = (
            document.rules.session_limit.max_seconds
            if document.rules.session_limit
            else None
        )
        daily_cap = (
            document.rules.daily_limit.max_seconds if document.rules.daily_limit else None
        )
        # 申诉例外豁免每日额度上限；账本仍照常累计，历史用量不受影响。
        daily_waived = RestrictionKind.DAILY_LIMIT in waived

        credited = 0
        per_day: dict[str, int] = {}
        hit_limit = False
        limit_reason: str | None = None

        for day, segment_seconds in split_watch_window(now, proposed, timezone):
            allow = segment_seconds

            # Bound by remaining session budget.
            if session_cap is not None:
                session_remaining = session_cap - session.total_watched_seconds
                if allow >= session_remaining:
                    allow = max(0, session_remaining)
                    hit_limit = True
                    limit_reason = ReasonCode.DENIED_SESSION_LIMIT_REACHED.value

            # Bound by remaining daily budget for this specific local day.
            if daily_cap is not None and not daily_waived:
                already = await self._ledger.get_seconds(session.tenant_id, session.user_id, day)
                day_remaining = daily_cap - (already + per_day.get(day, 0))
                if allow >= day_remaining:
                    allow = max(0, day_remaining)
                    hit_limit = True
                    limit_reason = ReasonCode.DENIED_DAILY_LIMIT_REACHED.value

            if allow > 0:
                await self._ledger.add_seconds(session.tenant_id, session.user_id, day, allow)
                session.total_watched_seconds += allow
                credited += allow
                per_day[day] = per_day.get(day, 0) + allow

            if hit_limit:
                break

        return credited, per_day, hit_limit, limit_reason

    # -- pause / resume ------------------------------------------------------

    async def pause(self, tenant_id: str, session_id: str) -> ActionResult:
        session = await self._sessions.get_for_update(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        if session.status is SessionStatus.ENDED:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_ENDED, session=session)
        if session.status is not SessionStatus.ACTIVE:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_ACTIVE, session=session)

        now = self._clock.now()
        session.status = SessionStatus.PAUSED
        session.updated_at = now
        await self._sessions.save(session)
        await self._emit("session.paused", session, now)
        return ActionResult.success(ReasonCode.SESSION_PAUSED, session=session)

    async def resume(self, tenant_id: str, session_id: str) -> ActionResult:
        session = await self._sessions.get_for_update(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        if session.status is SessionStatus.ENDED:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_ENDED, session=session)
        if session.status is not SessionStatus.PAUSED:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_PAUSED, session=session)

        now = self._clock.now()
        session.status = SessionStatus.ACTIVE
        session.updated_at = now
        await self._sessions.save(session)
        await self._emit("session.resumed", session, now)
        return ActionResult.success(ReasonCode.SESSION_RESUMED, session=session)

    # -- end -----------------------------------------------------------------

    async def end(self, tenant_id: str, session_id: str) -> ActionResult:
        session = await self._sessions.get_for_update(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        if session.status is SessionStatus.ENDED:
            # Ending an already-ended session is idempotent.
            return ActionResult.success(ReasonCode.SESSION_ENDED, session=session)

        now = self._clock.now()
        session.status = SessionStatus.ENDED
        session.ended_at = now
        session.updated_at = now
        await self._sessions.save(session)
        await self._emit("session.ended", session, now, cause="client")
        return ActionResult.success(ReasonCode.SESSION_ENDED, session=session)

    # -- usage query ---------------------------------------------------------

    async def get_usage(self, tenant_id: str, session_id: str) -> ActionResult:
        session = await self._sessions.get(tenant_id, session_id)
        if session is None:
            return ActionResult.rejected(ReasonCode.REJECTED_SESSION_NOT_FOUND)
        # Report the authoritative daily total for the session's current local day.
        policy = await self._policies.get_by_id(tenant_id, session.policy_id)
        daily_today = 0
        if policy is not None:
            day = local_day_key(self._clock.now(), policy.document.rules.timezone)
            daily_today = await self._ledger.get_seconds(tenant_id, session.user_id, day)
        return ActionResult.success(
            ReasonCode.ALLOWED, session=session, daily_today_seconds=daily_today
        )

    # -- helpers -------------------------------------------------------------

    async def _emit(self, event_type: str, session: Session, now, **payload) -> None:
        await self._outbox.add(
            DomainEvent(
                event_type=event_type,
                tenant_id=session.tenant_id,
                aggregate_id=session.id,
                occurred_at=now,
                payload={
                    "session_id": session.id,
                    "user_id": session.user_id,
                    "status": session.status.value,
                    **payload,
                },
            )
        )
