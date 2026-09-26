"""服务端业务模块。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from spe.domain.extensions.appeal_cases import AppealState, Verdict
from spe.domain.policy_ast import PolicyDocument, RestrictionKind
from spe.domain.reason_codes import ReasonCode
from spe.domain.session import SessionStatus

# --- Policy -----------------------------------------------------------------


class PublishPolicyRequest(BaseModel):
    """封装领域状态与业务约束。"""

    document: PolicyDocument


class PublishPolicyResponse(BaseModel):
    policy_id: str
    tenant_id: str
    version: int
    name: str


class ValidationIssueOut(BaseModel):
    path: str
    message: str


class CheckPolicyResponse(BaseModel):
    ok: bool
    issues: list[ValidationIssueOut] = Field(default_factory=list)


class PreviewRequest(BaseModel):
    """封装领域状态与业务约束。"""

    user_id: str
    birth_date: date
    daily_usage_seconds: int = Field(default=0, ge=0)
    session_elapsed_seconds: int = Field(default=0, ge=0)
    at: datetime | None = None
    version: int | None = None


class TraceStepOut(BaseModel):
    rule: str
    outcome: str
    reason: str | None
    detail: str


class PreviewResponse(BaseModel):
    allowed: bool
    reason: ReasonCode
    trace: list[TraceStepOut]


# --- Sessions ---------------------------------------------------------------


class StartSessionRequest(BaseModel):
    user_id: str
    birth_date: date
    idempotency_key: str | None = None


class HeartbeatRequest(BaseModel):
    seq: int = Field(ge=1)
    watched_seconds_total: int = Field(ge=0)


class SessionOut(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    policy_id: str
    policy_version: int
    status: SessionStatus
    birth_date: date
    started_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    total_watched_seconds: int


class ActionResponse(BaseModel):
    """封装领域状态与业务约束。"""

    ok: bool
    reason: ReasonCode
    session: SessionOut | None = None
    trace: list[TraceStepOut] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


# --- Replay -----------------------------------------------------------------


class ReplayStepOut(BaseModel):
    seq: int
    credited_seconds: int
    per_day: dict[str, int]
    total_watched_seconds: int
    age: int
    allowed: bool
    reason: str
    trace: list[TraceStepOut]


class ReplayResponse(BaseModel):
    session_id: str
    policy_version: int
    steps: list[ReplayStepOut]


# --- Appeals ----------------------------------------------------------------


class AppealEvidenceIn(BaseModel):
    """立案或补证时提交的一条证据。"""

    label: str = Field(min_length=1, max_length=100)
    detail: str = Field(default="", max_length=2000)


class OpenAppealRequest(BaseModel):
    """立案请求：锚定会话（可选）+ 可申诉的拒绝决定。"""

    user_id: str = Field(min_length=1, max_length=64)
    denial_reason: ReasonCode
    session_id: str | None = None
    birth_date: date | None = None
    evidence: list[AppealEvidenceIn] = Field(default_factory=list, max_length=20)


class AdjudicateAppealRequest(BaseModel):
    """裁决请求：维持、纠正资料或发放有期限的例外。"""

    verdict: Verdict
    reason: str = Field(min_length=1, max_length=2000)
    corrected_birth_date: date | None = None
    exception_kind: RestrictionKind | None = None
    exception_ttl_seconds: int | None = Field(default=None, gt=0)


class ReopenAppealRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class AppealCaseOut(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    session_id: str | None
    denial_reason: ReasonCode
    policy_id: str
    policy_version: int
    state: AppealState
    assignee_id: str | None
    verdict: Verdict | None
    verdict_reason: str | None
    correction: dict | None
    exception_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class AppealEvidenceOut(BaseModel):
    id: int
    label: str
    detail: str
    added_by: str
    added_at: datetime


class AppealEventOut(BaseModel):
    action: str
    actor_id: str
    detail: dict
    occurred_at: datetime


class DecisionSnapshotOut(BaseModel):
    """立案时固定的策略轨迹。"""

    evaluated_at: datetime
    timezone: str
    context: dict[str, Any]
    allowed: bool
    reason: ReasonCode
    trace: list[TraceStepOut]


class ExceptionGrantOut(BaseModel):
    id: str
    user_id: str
    kind: RestrictionKind
    case_id: str
    granted_by: str
    reason: str
    created_at: datetime
    expires_at: datetime
    active: bool


class AppealExplanation(BaseModel):
    """解释接口的完整响应：案件、策略轨迹、证据、时间线与生效中的例外。"""

    case: AppealCaseOut
    snapshot: DecisionSnapshotOut
    evidence: list[AppealEvidenceOut]
    timeline: list[AppealEventOut]
    active_exception: ExceptionGrantOut | None
