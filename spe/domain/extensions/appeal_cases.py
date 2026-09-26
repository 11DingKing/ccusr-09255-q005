"""限制决定申诉的领域模型与状态规则。

用户对年龄、休息时段或每日额度的拒绝决定提出申诉时，系统立案并把
拒绝决定、固定策略版本、关联会话与证据绑定到同一个案件。客服认领后
在授权范围内作出一次性裁决：维持原决定、纠正资料，或发放有期限的
例外。裁决不得篡改历史使用量（每日账本与会话累计保持只读）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from spe.domain.policy_ast import RestrictionKind
from spe.domain.reason_codes import ReasonCode

# 可申诉的拒绝决定 -> 对应的限制类型。其余拒绝（如单次时长）不可申诉。
APPEALABLE_DENIALS: dict[ReasonCode, RestrictionKind] = {
    ReasonCode.DENIED_UNDER_MIN_AGE: RestrictionKind.MIN_AGE,
    ReasonCode.DENIED_BEDTIME_CURFEW: RestrictionKind.BEDTIME,
    ReasonCode.DENIED_DAILY_LIMIT_REACHED: RestrictionKind.DAILY_LIMIT,
}

# 例外授权的最长期限，防止客服发放事实上的永久豁免。
MAX_EXCEPTION_TTL = timedelta(days=30)


class AppealState(StrEnum):
    """申诉案件的生命周期状态。"""

    OPENED = "opened"
    """已立案，等待客服认领。"""

    REVIEWING = "reviewing"
    """已被认领，审核中；只有认领人可以裁决。"""

    RESOLVED = "resolved"
    """已裁决（一次性）；复开后才允许再次认领与裁决。"""


class Verdict(StrEnum):
    """裁决结论。"""

    UPHOLD = "uphold"
    """维持原拒绝决定，无副作用。"""

    CORRECT_PROFILE = "correct_profile"
    """纠正资料（如出生日期），写入权威档案并同步锚定会话。"""

    GRANT_EXCEPTION = "grant_exception"
    """发放有期限的例外，豁免与申诉限制同类的限制。"""


class CaseAction(StrEnum):
    """写入案件时间线的动作类型。"""

    OPENED = "opened"
    CLAIMED = "claimed"
    EVIDENCE_ADDED = "evidence_added"
    ADJUDICATED = "adjudicated"
    REOPENED = "reopened"


def incident_key(user_id: str, session_id: str | None, denial_reason: ReasonCode) -> str:
    """同一用户就同一次拒绝事件（会话 + 拒绝原因）只能立一个案件。"""
    return f"{user_id}|{session_id or '-'}|{denial_reason.value}"


@dataclass(frozen=True)
class AppealCase:
    """申诉案件：拒绝决定、策略版本、会话与证据的固定关联。"""

    id: str
    tenant_id: str
    user_id: str
    session_id: str | None
    denial_reason: ReasonCode
    policy_id: str
    policy_version: int
    snapshot: dict[str, Any]
    """立案时固定的策略轨迹与评估上下文（评估时间、年龄、当日用量、逐条规则结果）。"""
    state: AppealState
    assignee_id: str | None
    verdict: Verdict | None
    verdict_reason: str | None
    correction: dict[str, Any] | None
    exception_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None

    @property
    def restriction_kind(self) -> RestrictionKind:
        """该案件所申诉的限制类型（授权范围：例外只能豁免这一类）。"""
        return APPEALABLE_DENIALS[self.denial_reason]


@dataclass(frozen=True)
class EvidenceItem:
    """案件证据，只增不改；id 为 0 表示尚未持久化。"""

    id: int
    tenant_id: str
    case_id: str
    label: str
    detail: str
    added_by: str
    added_at: datetime


@dataclass(frozen=True)
class CaseEvent:
    """案件时间线条目，只增不改，供解释接口回放。"""

    tenant_id: str
    case_id: str
    action: str
    actor_id: str
    detail: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True)
class ExceptionGrant:
    """裁决发放的有期限例外，按（租户, 用户, 限制类型）在评估时生效。"""

    id: str
    tenant_id: str
    user_id: str
    kind: RestrictionKind
    case_id: str
    granted_by: str
    reason: str
    created_at: datetime
    expires_at: datetime

    def active(self, now: datetime) -> bool:
        return now < self.expires_at


@dataclass(frozen=True)
class ProfileCorrection:
    """裁决确认的资料纠正；开播评估以此为准，覆盖客户端上报值。"""

    tenant_id: str
    user_id: str
    birth_date: date
    source_case_id: str
    corrected_by: str
    updated_at: datetime
