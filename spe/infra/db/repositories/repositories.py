"""服务端业务模块。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from spe.domain.events import DomainEvent
from spe.domain.extensions.appeal_cases import (
    AppealCase,
    AppealState,
    CaseEvent,
    EvidenceItem,
    ExceptionGrant,
    ProfileCorrection,
    incident_key,
)
from spe.domain.policy_ast import PolicyDocument
from spe.domain.repositories import PolicyRecord
from spe.domain.services.appeal_service import DuplicateAppeal
from spe.domain.services.session_service import ActiveSessionExists
from spe.domain.session import Session
from spe.infra.db.models import (
    AppealCaseModel,
    AppealEventModel,
    AppealEvidenceModel,
    DailyUsageLedgerModel,
    ExceptionGrantModel,
    HeartbeatModel,
    OutboxModel,
    PolicyModel,
    ProfileCorrectionModel,
    SessionModel,
)
from spe.infra.db.repositories.mappers import (
    PolicyRecordAdapter,
    appeal_case_to_domain,
    apply_session_to_model,
    case_event_to_domain,
    correction_to_domain,
    evidence_to_domain,
    grant_to_domain,
    session_to_domain,
)


class SqlPolicyRepository:
    """封装领域状态与业务约束。"""

    def __init__(self, db: AsyncSession, clock_now: datetime) -> None:
        self._db = db
        self._now = clock_now

    async def next_version(self, tenant_id: str) -> int:
        stmt = select(func.max(PolicyModel.version)).where(PolicyModel.tenant_id == tenant_id)
        current = (await self._db.execute(stmt)).scalar()
        return (current or 0) + 1

    async def add(
        self, tenant_id: str, version: int, document: PolicyDocument, policy_id: str
    ) -> PolicyRecord:
        # New version becomes active; demote previous active versions.
        await self._db.execute(
            select(PolicyModel).where(
                PolicyModel.tenant_id == tenant_id, PolicyModel.is_active.is_(True)
            )
        )
        for prev in (
            await self._db.execute(
                select(PolicyModel).where(
                    PolicyModel.tenant_id == tenant_id, PolicyModel.is_active.is_(True)
                )
            )
        ).scalars():
            prev.is_active = False

        model = PolicyModel(
            id=policy_id,
            tenant_id=tenant_id,
            version=version,
            name=document.name,
            document=document.model_dump(mode="json"),
            is_active=True,
            created_at=self._now,
        )
        self._db.add(model)
        await self._db.flush()
        return PolicyRecordAdapter(model)

    async def get_active(self, tenant_id: str) -> PolicyRecord | None:
        stmt = (
            select(PolicyModel)
            .where(PolicyModel.tenant_id == tenant_id, PolicyModel.is_active.is_(True))
            .order_by(PolicyModel.version.desc())
            .limit(1)
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return PolicyRecordAdapter(model) if model else None

    async def get_version(self, tenant_id: str, version: int) -> PolicyRecord | None:
        stmt = select(PolicyModel).where(
            PolicyModel.tenant_id == tenant_id, PolicyModel.version == version
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return PolicyRecordAdapter(model) if model else None

    async def get_by_id(self, tenant_id: str, policy_id: str) -> PolicyRecord | None:
        stmt = select(PolicyModel).where(
            PolicyModel.tenant_id == tenant_id, PolicyModel.id == policy_id
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return PolicyRecordAdapter(model) if model else None


class SqlSessionRepository:
    """封装领域状态与业务约束。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, session: Session, idempotency_key: str | None = None) -> None:
        model = SessionModel(
            id=session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            policy_id=session.policy_id,
            policy_version=session.policy_version,
            status=session.status.value,
            idempotency_key=idempotency_key,
            birth_date=session.birth_date,
            started_at=session.started_at,
            updated_at=session.updated_at,
            ended_at=session.ended_at,
            last_seq=session.last_seq,
            watched_seconds_marker=session.watched_seconds_marker,
            total_watched_seconds=session.total_watched_seconds,
        )
        self._db.add(model)
        try:
            await self._db.flush()
        except IntegrityError as exc:  # single-active-session / idempotency guard tripped
            await self._db.rollback()
            raise ActiveSessionExists(str(exc)) from exc

    async def get(self, tenant_id: str, session_id: str) -> Session | None:
        stmt = select(SessionModel).where(
            SessionModel.tenant_id == tenant_id, SessionModel.id == session_id
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return session_to_domain(model) if model else None

    async def get_for_update(self, tenant_id: str, session_id: str) -> Session | None:
        stmt = select(SessionModel).where(
            SessionModel.tenant_id == tenant_id, SessionModel.id == session_id
        )
        # Row-level lock so concurrent heartbeats for one session serialise.
        # SQLite has no row locks (single writer already serialises), so only
        # apply the FOR UPDATE clause on backends that support it.
        if self._db.bind is not None and self._db.bind.dialect.name != "sqlite":
            stmt = stmt.with_for_update()
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return session_to_domain(model) if model else None

    async def get_active_for_user(self, tenant_id: str, user_id: str) -> Session | None:
        stmt = select(SessionModel).where(
            SessionModel.tenant_id == tenant_id,
            SessionModel.user_id == user_id,
            SessionModel.status != "ENDED",
        )
        model = (await self._db.execute(stmt)).scalars().first()
        return session_to_domain(model) if model else None

    async def get_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> Session | None:
        stmt = select(SessionModel).where(
            SessionModel.tenant_id == tenant_id,
            SessionModel.idempotency_key == idempotency_key,
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return session_to_domain(model) if model else None

    async def save(self, session: Session, idempotency_key: str | None = None) -> None:
        stmt = select(SessionModel).where(
            SessionModel.tenant_id == session.tenant_id, SessionModel.id == session.id
        )
        model = (await self._db.execute(stmt)).scalar_one()
        apply_session_to_model(session, model)
        if idempotency_key is not None:
            model.idempotency_key = idempotency_key
        await self._db.flush()

    async def update_birth_date(
        self, tenant_id: str, session_id: str, birth_date: date, updated_at: datetime
    ) -> None:
        stmt = (
            update(SessionModel)
            .where(SessionModel.tenant_id == tenant_id, SessionModel.id == session_id)
            .values(birth_date=birth_date, updated_at=updated_at)
        )
        await self._db.execute(stmt)


class SqlDailyUsageLedger:
    """封装领域状态与业务约束。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_seconds(self, tenant_id: str, user_id: str, local_day: str) -> int:
        stmt = select(DailyUsageLedgerModel.seconds).where(
            DailyUsageLedgerModel.tenant_id == tenant_id,
            DailyUsageLedgerModel.user_id == user_id,
            DailyUsageLedgerModel.local_day == local_day,
        )
        return (await self._db.execute(stmt)).scalar() or 0

    async def add_seconds(
        self, tenant_id: str, user_id: str, local_day: str, seconds: int
    ) -> int:
        if seconds <= 0:
            return await self.get_seconds(tenant_id, user_id, local_day)

        values = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "local_day": local_day,
            "seconds": seconds,
        }
        insert = sqlite_insert
        stmt = insert(DailyUsageLedgerModel).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "user_id", "local_day"],
            set_={"seconds": DailyUsageLedgerModel.seconds + seconds},
        )
        await self._db.execute(stmt)
        await self._db.flush()
        return await self.get_seconds(tenant_id, user_id, local_day)


class SqlOutboxRepository:
    """封装领域状态与业务约束。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, event: DomainEvent) -> None:
        self._db.add(
            OutboxModel(
                event_type=event.event_type,
                tenant_id=event.tenant_id,
                aggregate_id=event.aggregate_id,
                payload=event.payload,
                occurred_at=event.occurred_at,
                published=False,
            )
        )
        await self._db.flush()


class SqlHeartbeatRepository:
    """封装领域状态与业务约束。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record(
        self,
        tenant_id: str,
        session_id: str,
        seq: int,
        watched_seconds_total: int,
        credited_seconds: int,
        occurred_at: datetime,
    ) -> None:
        self._db.add(
            HeartbeatModel(
                session_id=session_id,
                tenant_id=tenant_id,
                seq=seq,
                watched_seconds_total=watched_seconds_total,
                credited_seconds=credited_seconds,
                occurred_at=occurred_at,
            )
        )
        await self._db.flush()

    async def list_for_session(
        self, tenant_id: str, session_id: str
    ) -> list[HeartbeatModel]:
        stmt = (
            select(HeartbeatModel)
            .where(
                HeartbeatModel.tenant_id == tenant_id,
                HeartbeatModel.session_id == session_id,
            )
            .order_by(HeartbeatModel.seq)
        )
        return list((await self._db.execute(stmt)).scalars())


class SqlAppealCaseRepository:
    """申诉案件存取；认领用条件更新保证并发下只有一个赢家。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, case: AppealCase) -> None:
        model = AppealCaseModel(
            id=case.id,
            tenant_id=case.tenant_id,
            incident_key=incident_key(case.user_id, case.session_id, case.denial_reason),
            user_id=case.user_id,
            session_id=case.session_id,
            denial_reason=case.denial_reason.value,
            policy_id=case.policy_id,
            policy_version=case.policy_version,
            snapshot=case.snapshot,
            state=case.state.value,
            assignee_id=case.assignee_id,
            verdict=case.verdict.value if case.verdict else None,
            verdict_reason=case.verdict_reason,
            correction=case.correction,
            exception_id=case.exception_id,
            version=case.version,
            created_at=case.created_at,
            updated_at=case.updated_at,
            resolved_at=case.resolved_at,
        )
        self._db.add(model)
        try:
            await self._db.flush()
        except IntegrityError as exc:  # 同一拒绝事件重复立案
            await self._db.rollback()
            raise DuplicateAppeal(str(exc)) from exc

    async def get(self, tenant_id: str, case_id: str) -> AppealCase | None:
        stmt = select(AppealCaseModel).where(
            AppealCaseModel.tenant_id == tenant_id, AppealCaseModel.id == case_id
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return appeal_case_to_domain(model) if model else None

    async def claim(self, tenant_id: str, case_id: str, agent_id: str, now: datetime) -> bool:
        # 原子条件更新：只有仍处于 opened 的案件能被认领，并发认领天然串行。
        stmt = (
            update(AppealCaseModel)
            .where(
                AppealCaseModel.tenant_id == tenant_id,
                AppealCaseModel.id == case_id,
                AppealCaseModel.state == AppealState.OPENED.value,
            )
            .values(
                state=AppealState.REVIEWING.value,
                assignee_id=agent_id,
                updated_at=now,
                version=AppealCaseModel.version + 1,
            )
        )
        result = await self._db.execute(stmt)
        return result.rowcount == 1

    async def save(self, case: AppealCase, expected_version: int) -> bool:
        stmt = (
            update(AppealCaseModel)
            .where(
                AppealCaseModel.tenant_id == case.tenant_id,
                AppealCaseModel.id == case.id,
                AppealCaseModel.version == expected_version,
            )
            .values(
                state=case.state.value,
                assignee_id=case.assignee_id,
                verdict=case.verdict.value if case.verdict else None,
                verdict_reason=case.verdict_reason,
                correction=case.correction,
                exception_id=case.exception_id,
                resolved_at=case.resolved_at,
                updated_at=case.updated_at,
                version=expected_version + 1,
            )
        )
        result = await self._db.execute(stmt)
        return result.rowcount == 1


class SqlAppealEvidenceRepository:
    """案件证据，只增不改。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, item: EvidenceItem) -> EvidenceItem:
        model = AppealEvidenceModel(
            tenant_id=item.tenant_id,
            case_id=item.case_id,
            label=item.label,
            detail=item.detail,
            added_by=item.added_by,
            added_at=item.added_at,
        )
        self._db.add(model)
        await self._db.flush()
        return evidence_to_domain(model)

    async def list_for_case(self, tenant_id: str, case_id: str) -> list[EvidenceItem]:
        stmt = (
            select(AppealEvidenceModel)
            .where(
                AppealEvidenceModel.tenant_id == tenant_id,
                AppealEvidenceModel.case_id == case_id,
            )
            .order_by(AppealEvidenceModel.id)
        )
        return [evidence_to_domain(m) for m in (await self._db.execute(stmt)).scalars()]


class SqlAppealEventRepository:
    """案件时间线，只增不改。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, event: CaseEvent) -> None:
        self._db.add(
            AppealEventModel(
                tenant_id=event.tenant_id,
                case_id=event.case_id,
                action=event.action,
                actor_id=event.actor_id,
                detail=event.detail,
                occurred_at=event.occurred_at,
            )
        )
        await self._db.flush()

    async def list_for_case(self, tenant_id: str, case_id: str) -> list[CaseEvent]:
        stmt = (
            select(AppealEventModel)
            .where(
                AppealEventModel.tenant_id == tenant_id,
                AppealEventModel.case_id == case_id,
            )
            .order_by(AppealEventModel.id)
        )
        return [case_event_to_domain(m) for m in (await self._db.execute(stmt)).scalars()]


class SqlExceptionGrantRepository:
    """有期限例外授权；生效判断在 SQL 层按 expires_at 过滤。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, grant: ExceptionGrant) -> None:
        self._db.add(
            ExceptionGrantModel(
                id=grant.id,
                tenant_id=grant.tenant_id,
                user_id=grant.user_id,
                kind=grant.kind.value,
                case_id=grant.case_id,
                granted_by=grant.granted_by,
                reason=grant.reason,
                created_at=grant.created_at,
                expires_at=grant.expires_at,
            )
        )
        await self._db.flush()

    async def list_active(
        self, tenant_id: str, user_id: str, now: datetime
    ) -> list[ExceptionGrant]:
        stmt = (
            select(ExceptionGrantModel)
            .where(
                ExceptionGrantModel.tenant_id == tenant_id,
                ExceptionGrantModel.user_id == user_id,
                ExceptionGrantModel.expires_at > now,
            )
            .order_by(ExceptionGrantModel.created_at)
        )
        return [grant_to_domain(m) for m in (await self._db.execute(stmt)).scalars()]

    async def find_active_for_case(
        self, tenant_id: str, case_id: str, now: datetime
    ) -> ExceptionGrant | None:
        stmt = (
            select(ExceptionGrantModel)
            .where(
                ExceptionGrantModel.tenant_id == tenant_id,
                ExceptionGrantModel.case_id == case_id,
                ExceptionGrantModel.expires_at > now,
            )
            .order_by(ExceptionGrantModel.created_at.desc())
            .limit(1)
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return grant_to_domain(model) if model else None


class SqlProfileCorrectionRepository:
    """资料纠正，按（租户, 用户） upsert。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def upsert(self, correction: ProfileCorrection) -> None:
        values = {
            "tenant_id": correction.tenant_id,
            "user_id": correction.user_id,
            "birth_date": correction.birth_date,
            "source_case_id": correction.source_case_id,
            "corrected_by": correction.corrected_by,
            "updated_at": correction.updated_at,
        }
        stmt = sqlite_insert(ProfileCorrectionModel).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "user_id"],
            set_={
                "birth_date": correction.birth_date,
                "source_case_id": correction.source_case_id,
                "corrected_by": correction.corrected_by,
                "updated_at": correction.updated_at,
            },
        )
        await self._db.execute(stmt)
        await self._db.flush()

    async def get(self, tenant_id: str, user_id: str) -> ProfileCorrection | None:
        stmt = select(ProfileCorrectionModel).where(
            ProfileCorrectionModel.tenant_id == tenant_id,
            ProfileCorrectionModel.user_id == user_id,
        )
        model = (await self._db.execute(stmt)).scalar_one_or_none()
        return correction_to_domain(model) if model else None
