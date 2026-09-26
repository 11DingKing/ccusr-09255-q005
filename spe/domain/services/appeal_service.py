"""限制决定申诉的应用服务：立案、认领、补证、裁决、复开与解释。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta

from spe.domain.clock import Clock
from spe.domain.events import DomainEvent
from spe.domain.extensions.appeal_cases import (
    APPEALABLE_DENIALS,
    MAX_EXCEPTION_TTL,
    AppealCase,
    AppealState,
    CaseAction,
    CaseEvent,
    EvidenceItem,
    ExceptionGrant,
    ProfileCorrection,
    Verdict,
)
from spe.domain.ids import IdGenerator
from spe.domain.policy_ast import PolicyDocument, RestrictionKind
from spe.domain.policy_interpreter import EvalContext, evaluate
from spe.domain.reason_codes import ReasonCode
from spe.domain.repositories import (
    AppealCaseRepository,
    AppealEventRepository,
    AppealEvidenceRepository,
    DailyUsageLedger,
    ExceptionGrantRepository,
    OutboxRepository,
    PolicyRepository,
    ProfileCorrectionRepository,
    SessionRepository,
)
from spe.domain.session import Session
from spe.domain.timeutil import age_at, local_day_key


class AppealError(Exception):
    """携带原因码的申诉领域错误，由路由层映射为 HTTP 响应。"""

    def __init__(self, reason: ReasonCode, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason.value)


class DuplicateAppeal(Exception):
    """同一拒绝事件已存在申诉案件（唯一约束冲突）。"""


class AppealService:
    """封装申诉案件的状态变更规则；所有副作用与案件更新同事务提交。"""

    def __init__(
        self,
        cases: AppealCaseRepository,
        evidence: AppealEvidenceRepository,
        events: AppealEventRepository,
        exceptions: ExceptionGrantRepository,
        corrections: ProfileCorrectionRepository,
        sessions: SessionRepository,
        policies: PolicyRepository,
        ledger: DailyUsageLedger,
        outbox: OutboxRepository,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._cases = cases
        self._evidence = evidence
        self._events = events
        self._exceptions = exceptions
        self._corrections = corrections
        self._sessions = sessions
        self._policies = policies
        self._ledger = ledger
        self._outbox = outbox
        self._clock = clock
        self._ids = ids

    # -- 立案 -----------------------------------------------------------------

    async def open(
        self,
        *,
        tenant_id: str,
        user_id: str,
        denial_reason: ReasonCode,
        session_id: str | None,
        birth_date: date | None,
        evidence: Sequence[tuple[str, str]],
        actor_id: str,
    ) -> AppealCase:
        """立案：固定拒绝决定、策略版本、会话与初始证据。"""
        if denial_reason not in APPEALABLE_DENIALS:
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_NOT_APPEALABLE,
                f"{denial_reason.value} 不属于可申诉的拒绝决定",
            )
        now = self._clock.now()

        session: Session | None = None
        if session_id is not None:
            session = await self._sessions.get(tenant_id, session_id)
            if session is None:
                raise AppealError(ReasonCode.REJECTED_SESSION_NOT_FOUND)
            if session.user_id != user_id:
                raise AppealError(
                    ReasonCode.REJECTED_APPEAL_INVALID, "会话不属于申诉用户"
                )
            policy = await self._policies.get_by_id(tenant_id, session.policy_id)
            assert policy is not None  # 会话固定的策略必然存在
            subject_birth = session.birth_date
        else:
            # 开播即被拒绝（年龄/休息时段）没有会话，锚定当前生效的策略版本。
            if birth_date is None:
                raise AppealError(
                    ReasonCode.REJECTED_APPEAL_INVALID, "无会话锚点时必须提供出生日期"
                )
            policy = await self._policies.get_active(tenant_id)
            if policy is None:
                raise AppealError(ReasonCode.REJECTED_POLICY_NOT_FOUND)
            subject_birth = birth_date

        snapshot = await self._snapshot(
            tenant_id, user_id, subject_birth, policy.document, session, now
        )
        case = AppealCase(
            id=self._ids.new_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session.id if session else None,
            denial_reason=denial_reason,
            policy_id=policy.id,
            policy_version=policy.version,
            snapshot=snapshot,
            state=AppealState.OPENED,
            assignee_id=None,
            verdict=None,
            verdict_reason=None,
            correction=None,
            exception_id=None,
            version=1,
            created_at=now,
            updated_at=now,
            resolved_at=None,
        )
        try:
            await self._cases.add(case)
        except DuplicateAppeal as exc:
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_DUPLICATE,
                "同一拒绝事件已存在申诉案件，请复开原案件",
            ) from exc

        await self._record(
            case,
            CaseAction.OPENED,
            actor_id,
            now,
            {
                "denial_reason": denial_reason.value,
                "session_id": case.session_id,
                "policy_version": policy.version,
            },
        )
        for label, detail in evidence:
            await self._add_evidence_row(case, label, detail, actor_id, now)
        await self._emit("appeal.opened", case, now)
        return case

    async def _snapshot(
        self,
        tenant_id: str,
        user_id: str,
        birth_date: date,
        document: PolicyDocument,
        session: Session | None,
        now: datetime,
    ) -> dict:
        """用固定的策略版本重放评估，把当时的策略轨迹固化到案件里。"""
        tz = document.rules.timezone
        daily_used = await self._ledger.get_seconds(
            tenant_id, user_id, local_day_key(now, tz)
        )
        ctx = EvalContext(
            now=now,
            user_id=user_id,
            user_age=age_at(birth_date, now, tz),
            daily_usage_seconds=daily_used,
            session_elapsed_seconds=session.total_watched_seconds if session else 0,
        )
        decision = evaluate(document, ctx)
        return {
            "evaluated_at": now.isoformat(),
            "timezone": tz,
            "context": {
                "user_age": ctx.user_age,
                "daily_usage_seconds": daily_used,
                "session_elapsed_seconds": ctx.session_elapsed_seconds,
            },
            "allowed": decision.allowed,
            "reason": decision.reason.value,
            "trace": decision.trace.as_list(),
        }

    # -- 认领 -----------------------------------------------------------------

    async def claim(self, *, tenant_id: str, case_id: str, agent_id: str) -> AppealCase:
        """认领：并发下只有一个客服能认领成功。"""
        now = self._clock.now()
        claimed = await self._cases.claim(tenant_id, case_id, agent_id, now)
        if not claimed:
            case = await self._cases.get(tenant_id, case_id)
            if case is None:
                raise AppealError(ReasonCode.REJECTED_APPEAL_NOT_FOUND)
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_STATE_CONFLICT,
                f"案件当前状态为 {case.state.value}，不能认领",
            )
        case = await self._cases.get(tenant_id, case_id)
        assert case is not None
        await self._record(case, CaseAction.CLAIMED, agent_id, now, {"assignee_id": agent_id})
        await self._emit("appeal.claimed", case, now, assignee_id=agent_id)
        return case

    # -- 补证 -----------------------------------------------------------------

    async def add_evidence(
        self,
        *,
        tenant_id: str,
        case_id: str,
        label: str,
        detail: str,
        actor_id: str,
    ) -> EvidenceItem:
        case = await self._cases.get(tenant_id, case_id)
        if case is None:
            raise AppealError(ReasonCode.REJECTED_APPEAL_NOT_FOUND)
        if case.state is AppealState.RESOLVED:
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_STATE_CONFLICT, "已裁决案件请先复开再补证"
            )
        return await self._add_evidence_row(case, label, detail, actor_id, self._clock.now())

    async def _add_evidence_row(
        self, case: AppealCase, label: str, detail: str, actor_id: str, now: datetime
    ) -> EvidenceItem:
        label = label.strip()
        if not label:
            raise AppealError(ReasonCode.REJECTED_APPEAL_INVALID, "证据标签不能为空")
        item = await self._evidence.add(
            EvidenceItem(
                id=0,
                tenant_id=case.tenant_id,
                case_id=case.id,
                label=label,
                detail=detail.strip(),
                added_by=actor_id,
                added_at=now,
            )
        )
        await self._record(
            case,
            CaseAction.EVIDENCE_ADDED,
            actor_id,
            now,
            {"evidence_id": item.id, "label": label},
        )
        return item

    # -- 裁决 -----------------------------------------------------------------

    async def adjudicate(
        self,
        *,
        tenant_id: str,
        case_id: str,
        agent_id: str,
        verdict: Verdict,
        reason: str,
        corrected_birth_date: date | None,
        exception_kind: RestrictionKind | None,
        exception_ttl_seconds: int | None,
    ) -> AppealCase:
        """裁决：一次性处理；副作用绝不触碰历史使用量。"""
        case = await self._cases.get(tenant_id, case_id)
        if case is None:
            raise AppealError(ReasonCode.REJECTED_APPEAL_NOT_FOUND)
        if case.state is not AppealState.REVIEWING:
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_STATE_CONFLICT, "案件不在审核中，不能裁决"
            )
        if case.assignee_id != agent_id:
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_FORBIDDEN, "只有认领人可以裁决该案件"
            )
        note = reason.strip()
        if not note:
            raise AppealError(ReasonCode.REJECTED_APPEAL_INVALID, "裁决理由不能为空")
        now = self._clock.now()

        correction: dict | None = None
        grant: ExceptionGrant | None = None

        if verdict is Verdict.UPHOLD:
            self._reject_verdict_extras(corrected_birth_date, exception_kind, exception_ttl_seconds)
        elif verdict is Verdict.CORRECT_PROFILE:
            if corrected_birth_date is None:
                raise AppealError(
                    ReasonCode.REJECTED_APPEAL_INVALID, "纠正资料必须提供新的出生日期"
                )
            if corrected_birth_date > now.date():
                raise AppealError(
                    ReasonCode.REJECTED_APPEAL_INVALID, "出生日期不能在未来"
                )
            if exception_kind is not None or exception_ttl_seconds is not None:
                raise AppealError(
                    ReasonCode.REJECTED_APPEAL_INVALID, "纠正资料不能同时发放例外"
                )
            correction = {"birth_date": corrected_birth_date.isoformat()}
        elif verdict is Verdict.GRANT_EXCEPTION:
            if corrected_birth_date is not None:
                raise AppealError(
                    ReasonCode.REJECTED_APPEAL_INVALID, "发放例外不能同时纠正资料"
                )
            expected = case.restriction_kind
            if exception_kind is None or exception_kind is not expected:
                raise AppealError(
                    ReasonCode.REJECTED_APPEAL_INVALID,
                    f"例外类型必须与申诉的限制一致（{expected.value}）",
                )
            max_ttl = int(MAX_EXCEPTION_TTL.total_seconds())
            if exception_ttl_seconds is None or not 0 < exception_ttl_seconds <= max_ttl:
                raise AppealError(
                    ReasonCode.REJECTED_APPEAL_INVALID,
                    f"例外期限必须在 1 秒到 {max_ttl} 秒之间",
                )
            grant = ExceptionGrant(
                id=self._ids.new_id(),
                tenant_id=tenant_id,
                user_id=case.user_id,
                kind=exception_kind,
                case_id=case.id,
                granted_by=agent_id,
                reason=note,
                created_at=now,
                expires_at=now + timedelta(seconds=exception_ttl_seconds),
            )

        updated = replace(
            case,
            state=AppealState.RESOLVED,
            verdict=verdict,
            verdict_reason=note,
            correction=correction,
            exception_id=grant.id if grant else None,
            resolved_at=now,
            updated_at=now,
        )
        if not await self._cases.save(updated, expected_version=case.version):
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_STATE_CONFLICT, "案件已被其他人变更，请刷新后重试"
            )

        # 副作用：资料纠正写入权威档案并同步锚定会话；例外授权落库。
        # 每日账本与会话累计等历史使用量保持只读，不做任何改动。
        if correction is not None and corrected_birth_date is not None:
            await self._corrections.upsert(
                ProfileCorrection(
                    tenant_id=tenant_id,
                    user_id=case.user_id,
                    birth_date=corrected_birth_date,
                    source_case_id=case.id,
                    corrected_by=agent_id,
                    updated_at=now,
                )
            )
            if case.session_id is not None:
                await self._sessions.update_birth_date(
                    tenant_id, case.session_id, corrected_birth_date, now
                )
        if grant is not None:
            await self._exceptions.add(grant)

        await self._record(
            case,
            CaseAction.ADJUDICATED,
            agent_id,
            now,
            {
                "verdict": verdict.value,
                "reason": note,
                "correction": correction,
                "exception_id": grant.id if grant else None,
                "exception_expires_at": grant.expires_at.isoformat() if grant else None,
            },
        )
        await self._emit("appeal.adjudicated", updated, now, verdict=verdict.value)
        return updated

    @staticmethod
    def _reject_verdict_extras(
        corrected_birth_date: date | None,
        exception_kind: RestrictionKind | None,
        exception_ttl_seconds: int | None,
    ) -> None:
        if (
            corrected_birth_date is not None
            or exception_kind is not None
            or exception_ttl_seconds is not None
        ):
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_INVALID, "维持原决定不能附带纠正或例外"
            )

    # -- 复开 -----------------------------------------------------------------

    async def reopen(
        self, *, tenant_id: str, case_id: str, agent_id: str, reason: str
    ) -> AppealCase:
        """复开：已裁决案件回到待认领队列，原裁决保留在时间线中。"""
        case = await self._cases.get(tenant_id, case_id)
        if case is None:
            raise AppealError(ReasonCode.REJECTED_APPEAL_NOT_FOUND)
        if case.state is not AppealState.RESOLVED:
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_STATE_CONFLICT, "只有已裁决的案件可以复开"
            )
        note = reason.strip()
        if not note:
            raise AppealError(ReasonCode.REJECTED_APPEAL_INVALID, "复开必须说明理由")
        now = self._clock.now()
        updated = replace(
            case,
            state=AppealState.OPENED,
            assignee_id=None,
            verdict=None,
            verdict_reason=None,
            correction=None,
            exception_id=None,
            resolved_at=None,
            updated_at=now,
        )
        if not await self._cases.save(updated, expected_version=case.version):
            raise AppealError(
                ReasonCode.REJECTED_APPEAL_STATE_CONFLICT, "案件已被其他人变更，请刷新后重试"
            )
        await self._record(case, CaseAction.REOPENED, agent_id, now, {"reason": note})
        await self._emit("appeal.reopened", updated, now)
        return updated

    # -- 解释 -----------------------------------------------------------------

    async def explain(
        self, *, tenant_id: str, case_id: str
    ) -> tuple[AppealCase, list[EvidenceItem], list[CaseEvent], ExceptionGrant | None]:
        """解释：案件、立案时固定的策略轨迹、证据、时间线与生效中的例外。"""
        case = await self._cases.get(tenant_id, case_id)
        if case is None:
            raise AppealError(ReasonCode.REJECTED_APPEAL_NOT_FOUND)
        now = self._clock.now()
        evidence = await self._evidence.list_for_case(tenant_id, case_id)
        timeline = await self._events.list_for_case(tenant_id, case_id)
        grant = await self._exceptions.find_active_for_case(tenant_id, case_id, now)
        return case, evidence, timeline, grant

    # -- helpers ---------------------------------------------------------------

    async def _record(
        self,
        case: AppealCase,
        action: CaseAction,
        actor_id: str,
        now: datetime,
        detail: dict,
    ) -> None:
        await self._events.add(
            CaseEvent(
                tenant_id=case.tenant_id,
                case_id=case.id,
                action=action.value,
                actor_id=actor_id,
                detail=detail,
                occurred_at=now,
            )
        )

    async def _emit(
        self, event_type: str, case: AppealCase, now: datetime, **payload: object
    ) -> None:
        await self._outbox.add(
            DomainEvent(
                event_type=event_type,
                tenant_id=case.tenant_id,
                aggregate_id=case.id,
                occurred_at=now,
                payload={
                    "case_id": case.id,
                    "user_id": case.user_id,
                    "state": case.state.value,
                    **payload,
                },
            )
        )
