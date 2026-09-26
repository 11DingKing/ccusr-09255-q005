"""服务端业务模块。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from spe.domain.events import DomainEvent
from spe.domain.extensions.appeal_cases import (
    AppealCase,
    CaseEvent,
    EvidenceItem,
    ExceptionGrant,
    ProfileCorrection,
)
from spe.domain.policy_ast import PolicyDocument
from spe.domain.session import Session


class PolicyRecord(Protocol):
    """封装领域状态与业务约束。"""

    @property
    def id(self) -> str: ...
    @property
    def tenant_id(self) -> str: ...
    @property
    def version(self) -> int: ...
    @property
    def document(self) -> PolicyDocument: ...


class PolicyRepository(Protocol):
    """封装领域状态与业务约束。"""

    async def next_version(self, tenant_id: str) -> int:
        """执行确定性的业务处理。"""
        ...

    async def add(
        self, tenant_id: str, version: int, document: PolicyDocument, policy_id: str
    ) -> PolicyRecord:
        """执行确定性的业务处理。"""
        ...

    async def get_active(self, tenant_id: str) -> PolicyRecord | None:
        """执行确定性的业务处理。"""
        ...

    async def get_version(self, tenant_id: str, version: int) -> PolicyRecord | None:
        """执行确定性的业务处理。"""
        ...

    async def get_by_id(self, tenant_id: str, policy_id: str) -> PolicyRecord | None:
        """执行确定性的业务处理。"""
        ...


class SessionRepository(Protocol):
    """封装领域状态与业务约束。"""

    async def add(self, session: Session) -> None: ...

    async def get(self, tenant_id: str, session_id: str) -> Session | None: ...

    async def get_for_update(
        self, tenant_id: str, session_id: str
    ) -> Session | None:
        """执行确定性的业务处理。"""
        ...

    async def get_active_for_user(self, tenant_id: str, user_id: str) -> Session | None: ...

    async def get_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> Session | None: ...

    async def save(self, session: Session, idempotency_key: str | None = None) -> None: ...

    async def update_birth_date(
        self, tenant_id: str, session_id: str, birth_date: date, updated_at: datetime
    ) -> None:
        """申诉裁决纠正资料时同步会话锚定的出生日期（不影响用量）。"""
        ...


class DailyUsageLedger(Protocol):
    """封装领域状态与业务约束。"""

    async def get_seconds(self, tenant_id: str, user_id: str, local_day: str) -> int:
        """执行确定性的业务处理。"""
        ...

    async def add_seconds(
        self, tenant_id: str, user_id: str, local_day: str, seconds: int
    ) -> int:
        """执行确定性的业务处理。"""
        ...


class OutboxRepository(Protocol):
    """封装领域状态与业务约束。"""

    async def add(self, event: DomainEvent) -> None: ...


class HeartbeatSink(Protocol):
    """封装领域状态与业务约束。"""

    async def record(
        self,
        tenant_id: str,
        session_id: str,
        seq: int,
        watched_seconds_total: int,
        credited_seconds: int,
        occurred_at: object,
    ) -> None: ...


class AppealCaseRepository(Protocol):
    """申诉案件存取；认领与保存使用乐观并发，避免客服互相覆盖。"""

    async def add(self, case: AppealCase) -> None:
        """插入案件；同一拒绝事件重复立案时抛出 DuplicateAppeal。"""
        ...

    async def get(self, tenant_id: str, case_id: str) -> AppealCase | None: ...

    async def claim(self, tenant_id: str, case_id: str, agent_id: str, now: datetime) -> bool:
        """原子地把 opened 案件置为 reviewing；返回 False 表示已被认领或不存在。"""
        ...

    async def save(self, case: AppealCase, expected_version: int) -> bool:
        """按期望版本号更新；版本不匹配返回 False（并发冲突）。"""
        ...


class AppealEvidenceRepository(Protocol):
    """案件证据，只增不改。"""

    async def add(self, item: EvidenceItem) -> EvidenceItem: ...

    async def list_for_case(self, tenant_id: str, case_id: str) -> list[EvidenceItem]: ...


class AppealEventRepository(Protocol):
    """案件时间线，只增不改。"""

    async def add(self, event: CaseEvent) -> None: ...

    async def list_for_case(self, tenant_id: str, case_id: str) -> list[CaseEvent]: ...


class ExceptionGrantRepository(Protocol):
    """裁决发放的有期限例外。"""

    async def add(self, grant: ExceptionGrant) -> None: ...

    async def list_active(
        self, tenant_id: str, user_id: str, now: datetime
    ) -> list[ExceptionGrant]: ...

    async def find_active_for_case(
        self, tenant_id: str, case_id: str, now: datetime
    ) -> ExceptionGrant | None: ...


class ProfileCorrectionRepository(Protocol):
    """裁决确认的资料纠正，按（租户, 用户）唯一。"""

    async def upsert(self, correction: ProfileCorrection) -> None: ...

    async def get(self, tenant_id: str, user_id: str) -> ProfileCorrection | None: ...
