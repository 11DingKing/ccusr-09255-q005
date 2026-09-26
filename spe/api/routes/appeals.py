"""限制决定申诉的 HTTP 路由：立案、认领、补证、裁决、复开与解释。"""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException, status

from spe.api.deps import AgentId, Svc, TenantId
from spe.api.schemas.http import (
    AdjudicateAppealRequest,
    AppealCaseOut,
    AppealEventOut,
    AppealEvidenceIn,
    AppealEvidenceOut,
    AppealExplanation,
    DecisionSnapshotOut,
    ExceptionGrantOut,
    OpenAppealRequest,
    ReopenAppealRequest,
)
from spe.domain.extensions.appeal_cases import AppealCase, ExceptionGrant
from spe.domain.reason_codes import ReasonCode
from spe.domain.services.appeal_service import AppealError

router = APIRouter(prefix="/v1/appeals", tags=["appeals"])

_STATUS_BY_REASON = {
    ReasonCode.REJECTED_APPEAL_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ReasonCode.REJECTED_SESSION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ReasonCode.REJECTED_POLICY_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ReasonCode.REJECTED_APPEAL_DUPLICATE: status.HTTP_409_CONFLICT,
    ReasonCode.REJECTED_APPEAL_STATE_CONFLICT: status.HTTP_409_CONFLICT,
    ReasonCode.REJECTED_APPEAL_FORBIDDEN: status.HTTP_403_FORBIDDEN,
}


def _raise(err: AppealError) -> NoReturn:
    code = _STATUS_BY_REASON.get(err.reason, status.HTTP_422_UNPROCESSABLE_CONTENT)
    raise HTTPException(status_code=code, detail={"reason": err.reason.value}) from err


def _case_out(case: AppealCase) -> AppealCaseOut:
    return AppealCaseOut(**case.__dict__)


def _grant_out(grant: ExceptionGrant, now) -> ExceptionGrantOut:
    return ExceptionGrantOut(**grant.__dict__, active=grant.active(now))


@router.post("", response_model=AppealCaseOut, status_code=status.HTTP_201_CREATED)
async def open_appeal(body: OpenAppealRequest, tenant_id: TenantId, svc: Svc) -> AppealCaseOut:
    """立案：固定拒绝决定、策略版本、会话与初始证据。"""
    try:
        case = await svc.appeal_service.open(
            tenant_id=tenant_id,
            user_id=body.user_id,
            denial_reason=body.denial_reason,
            session_id=body.session_id,
            birth_date=body.birth_date,
            evidence=[(item.label, item.detail) for item in body.evidence],
            actor_id=f"user:{body.user_id}",
        )
    except AppealError as err:
        _raise(err)
    return _case_out(case)


@router.post("/{case_id}/claim", response_model=AppealCaseOut)
async def claim_appeal(
    case_id: str, tenant_id: TenantId, agent_id: AgentId, svc: Svc
) -> AppealCaseOut:
    """认领：并发下只有一个客服成功。"""
    try:
        case = await svc.appeal_service.claim(
            tenant_id=tenant_id, case_id=case_id, agent_id=agent_id
        )
    except AppealError as err:
        _raise(err)
    return _case_out(case)


@router.post(
    "/{case_id}/evidence",
    response_model=AppealEvidenceOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_evidence(
    case_id: str, body: AppealEvidenceIn, tenant_id: TenantId, agent_id: AgentId, svc: Svc
) -> AppealEvidenceOut:
    """补证：立案后、裁决前都可以补充证据。"""
    try:
        item = await svc.appeal_service.add_evidence(
            tenant_id=tenant_id,
            case_id=case_id,
            label=body.label,
            detail=body.detail,
            actor_id=agent_id,
        )
    except AppealError as err:
        _raise(err)
    return AppealEvidenceOut(**item.__dict__)


@router.post("/{case_id}/adjudicate", response_model=AppealCaseOut)
async def adjudicate_appeal(
    case_id: str, body: AdjudicateAppealRequest, tenant_id: TenantId, agent_id: AgentId, svc: Svc
) -> AppealCaseOut:
    """裁决：一次性处理，只有认领人可以执行。"""
    try:
        case = await svc.appeal_service.adjudicate(
            tenant_id=tenant_id,
            case_id=case_id,
            agent_id=agent_id,
            verdict=body.verdict,
            reason=body.reason,
            corrected_birth_date=body.corrected_birth_date,
            exception_kind=body.exception_kind,
            exception_ttl_seconds=body.exception_ttl_seconds,
        )
    except AppealError as err:
        _raise(err)
    return _case_out(case)


@router.post("/{case_id}/reopen", response_model=AppealCaseOut)
async def reopen_appeal(
    case_id: str, body: ReopenAppealRequest, tenant_id: TenantId, agent_id: AgentId, svc: Svc
) -> AppealCaseOut:
    """复开：已裁决案件回到待认领队列。"""
    try:
        case = await svc.appeal_service.reopen(
            tenant_id=tenant_id, case_id=case_id, agent_id=agent_id, reason=body.reason
        )
    except AppealError as err:
        _raise(err)
    return _case_out(case)


@router.get("/{case_id}", response_model=AppealExplanation)
async def explain_appeal(case_id: str, tenant_id: TenantId, svc: Svc) -> AppealExplanation:
    """解释：当时的策略轨迹、证据、裁决时间线与生效中的例外。"""
    try:
        case, evidence, timeline, grant = await svc.appeal_service.explain(
            tenant_id=tenant_id, case_id=case_id
        )
    except AppealError as err:
        _raise(err)
    return AppealExplanation(
        case=_case_out(case),
        snapshot=DecisionSnapshotOut(**case.snapshot),
        evidence=[AppealEvidenceOut(**item.__dict__) for item in evidence],
        timeline=[AppealEventOut(**event.__dict__) for event in timeline],
        active_exception=_grant_out(grant, svc.clock.now()) if grant else None,
    )
