"""服务端业务模块。"""

from __future__ import annotations

from datetime import UTC, datetime

from spe.domain.extensions.appeal_cases import (
    AppealCase,
    AppealState,
    CaseEvent,
    EvidenceItem,
    ExceptionGrant,
    ProfileCorrection,
    Verdict,
)
from spe.domain.policy_ast import PolicyDocument, RestrictionKind
from spe.domain.reason_codes import ReasonCode
from spe.domain.session import Session, SessionStatus
from spe.infra.db.models import (
    AppealCaseModel,
    AppealEventModel,
    AppealEvidenceModel,
    ExceptionGrantModel,
    PolicyModel,
    ProfileCorrectionModel,
    SessionModel,
)


def _aware(value: datetime) -> datetime:
    """SQLite 不保存时区；读回的朴素时间一律按 UTC 解释。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class PolicyRecordAdapter:
    """封装领域状态与业务约束。"""

    def __init__(self, model: PolicyModel) -> None:
        self._model = model
        self._document = PolicyDocument.model_validate(model.document)

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def tenant_id(self) -> str:
        return self._model.tenant_id

    @property
    def version(self) -> int:
        return self._model.version

    @property
    def document(self) -> PolicyDocument:
        return self._document


def session_to_domain(model: SessionModel) -> Session:
    return Session(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        policy_id=model.policy_id,
        policy_version=model.policy_version,
        status=SessionStatus(model.status),
        birth_date=model.birth_date,
        started_at=model.started_at,
        updated_at=model.updated_at,
        ended_at=model.ended_at,
        last_seq=model.last_seq,
        watched_seconds_marker=model.watched_seconds_marker,
        total_watched_seconds=model.total_watched_seconds,
    )


def apply_session_to_model(session: Session, model: SessionModel) -> None:
    model.status = session.status.value
    model.updated_at = session.updated_at
    model.ended_at = session.ended_at
    model.last_seq = session.last_seq
    model.watched_seconds_marker = session.watched_seconds_marker
    model.total_watched_seconds = session.total_watched_seconds


def appeal_case_to_domain(model: AppealCaseModel) -> AppealCase:
    return AppealCase(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        session_id=model.session_id,
        denial_reason=ReasonCode(model.denial_reason),
        policy_id=model.policy_id,
        policy_version=model.policy_version,
        snapshot=model.snapshot,
        state=AppealState(model.state),
        assignee_id=model.assignee_id,
        verdict=Verdict(model.verdict) if model.verdict else None,
        verdict_reason=model.verdict_reason,
        correction=model.correction,
        exception_id=model.exception_id,
        version=model.version,
        created_at=_aware(model.created_at),
        updated_at=_aware(model.updated_at),
        resolved_at=_aware(model.resolved_at) if model.resolved_at else None,
    )


def evidence_to_domain(model: AppealEvidenceModel) -> EvidenceItem:
    return EvidenceItem(
        id=model.id,
        tenant_id=model.tenant_id,
        case_id=model.case_id,
        label=model.label,
        detail=model.detail,
        added_by=model.added_by,
        added_at=_aware(model.added_at),
    )


def case_event_to_domain(model: AppealEventModel) -> CaseEvent:
    return CaseEvent(
        tenant_id=model.tenant_id,
        case_id=model.case_id,
        action=model.action,
        actor_id=model.actor_id,
        detail=model.detail,
        occurred_at=_aware(model.occurred_at),
    )


def grant_to_domain(model: ExceptionGrantModel) -> ExceptionGrant:
    return ExceptionGrant(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        kind=RestrictionKind(model.kind),
        case_id=model.case_id,
        granted_by=model.granted_by,
        reason=model.reason,
        created_at=_aware(model.created_at),
        expires_at=_aware(model.expires_at),
    )


def correction_to_domain(model: ProfileCorrectionModel) -> ProfileCorrection:
    return ProfileCorrection(
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        birth_date=model.birth_date,
        source_case_id=model.source_case_id,
        corrected_by=model.corrected_by,
        updated_at=_aware(model.updated_at),
    )
