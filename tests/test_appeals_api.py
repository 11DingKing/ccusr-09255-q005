"""限制决定申诉 API 的行为测试：立案、认领、补证、裁决、复开与解释。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from tests.conftest import TENANT_A, TENANT_B, birth_for_age, headers, sample_policy

pytestmark = pytest.mark.asyncio

AGENT_1 = "agent-1"
AGENT_2 = "agent-2"


def agent_headers(tenant: str = TENANT_A, agent: str = AGENT_1) -> dict[str, str]:
    return {**headers(tenant), "X-Agent-ID": agent}


async def _publish(client, tenant: str = TENANT_A, **kw) -> None:
    resp = await client.post(
        "/v1/policies", json={"document": sample_policy(**kw)}, headers=headers(tenant)
    )
    assert resp.status_code == 201


async def _start(client, tenant: str = TENANT_A, user_id: str = "u1", age: int = 20, **kw):
    return await client.post(
        "/v1/sessions",
        json={"user_id": user_id, "birth_date": birth_for_age(age).isoformat(), **kw},
        headers=headers(tenant),
    )


async def _hb(client, sid: str, seq: int, total: int, tenant: str = TENANT_A):
    return await client.post(
        f"/v1/sessions/{sid}/heartbeat",
        json={"seq": seq, "watched_seconds_total": total},
        headers=headers(tenant),
    )


async def _exhaust_daily(client, tenant: str = TENANT_A, user_id: str = "u1") -> str:
    """每日 100s 额度被一个会话耗尽，返回被限额结束的会话 id。"""
    await _publish(
        client,
        tenant,
        min_age=None,
        daily_limit_seconds=100,
        session_limit_seconds=None,
        bedtime=None,
    )
    sid = (await _start(client, tenant, user_id)).json()["session"]["id"]
    assert (await _hb(client, sid, 1, 90, tenant)).json()["reason"] == "HEARTBEAT_APPLIED"
    last = await _hb(client, sid, 2, 180, tenant)
    assert last.json()["reason"] == "SESSION_ENDED_BY_LIMIT"
    assert last.json()["extra"]["limit_reason"] == "DENIED_DAILY_LIMIT_REACHED"
    # 额度已耗尽，再次开播被拒绝。
    assert (await _start(client, tenant, user_id)).json()[
        "reason"
    ] == "DENIED_DAILY_LIMIT_REACHED"
    return sid


async def _open(client, tenant: str = TENANT_A, **payload):
    payload.setdefault("user_id", "u1")
    payload.setdefault("denial_reason", "DENIED_DAILY_LIMIT_REACHED")
    return await client.post("/v1/appeals", json=payload, headers=headers(tenant))


async def _open_daily_case(client, tenant: str = TENANT_A) -> str:
    """构造一个每日额度申诉案件并返回案件 id。"""
    sid = await _exhaust_daily(client, tenant)
    resp = await _open(client, tenant, session_id=sid)
    assert resp.status_code == 201
    return resp.json()["id"]


async def _claim(client, case_id: str, tenant: str = TENANT_A, agent: str = AGENT_1):
    return await client.post(
        f"/v1/appeals/{case_id}/claim", headers=agent_headers(tenant, agent)
    )


async def _adjudicate(
    client, case_id: str, tenant: str = TENANT_A, agent: str = AGENT_1, **payload
):
    payload.setdefault("reason", "客服核实")
    return await client.post(
        f"/v1/appeals/{case_id}/adjudicate",
        json=payload,
        headers=agent_headers(tenant, agent),
    )


# --- 立案 -------------------------------------------------------------------


async def test_open_appeal_binds_decision_policy_session_and_evidence(client) -> None:
    sid = await _exhaust_daily(client)
    resp = await _open(
        client, session_id=sid, evidence=[{"label": "截图", "detail": "拒绝页面"}]
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "opened"
    assert body["session_id"] == sid
    assert body["user_id"] == "u1"
    assert body["denial_reason"] == "DENIED_DAILY_LIMIT_REACHED"
    assert body["policy_version"] == 1
    assert body["assignee_id"] is None
    assert body["verdict"] is None

    # 解释接口：立案时固定的策略轨迹、证据与时间线。
    explain = await client.get(f"/v1/appeals/{body['id']}", headers=headers())
    assert explain.status_code == 200
    data = explain.json()
    assert data["case"]["id"] == body["id"]
    assert data["snapshot"]["allowed"] is False
    assert data["snapshot"]["reason"] == "DENIED_DAILY_LIMIT_REACHED"
    assert data["snapshot"]["context"]["daily_usage_seconds"] == 100
    deny_steps = [s for s in data["snapshot"]["trace"] if s["outcome"] == "deny"]
    assert deny_steps and deny_steps[0]["rule"] == "daily_limit"
    assert [e["label"] for e in data["evidence"]] == ["截图"]
    assert [e["action"] for e in data["timeline"]] == ["opened", "evidence_added"]
    assert data["timeline"][0]["actor_id"] == "user:u1"
    assert data["active_exception"] is None


async def test_open_appeal_for_start_denial_without_session(client) -> None:
    await _publish(client, min_age=18, bedtime=None)
    denied = await _start(client, age=15)
    assert denied.json()["reason"] == "DENIED_UNDER_MIN_AGE"

    # 开播即被拒绝没有会话，案件锚定当前生效的策略版本。
    resp = await _open(
        client,
        denial_reason="DENIED_UNDER_MIN_AGE",
        birth_date=birth_for_age(15).isoformat(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"] is None
    assert body["policy_version"] == 1

    explain = await client.get(f"/v1/appeals/{body['id']}", headers=headers())
    snapshot = explain.json()["snapshot"]
    assert snapshot["reason"] == "DENIED_UNDER_MIN_AGE"
    assert snapshot["context"]["user_age"] == 15


async def test_open_appeal_requires_birth_date_without_session(client) -> None:
    await _publish(client, min_age=18, bedtime=None)
    resp = await _open(client, denial_reason="DENIED_UNDER_MIN_AGE")
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "REJECTED_APPEAL_INVALID"


async def test_open_appeal_rejects_unknown_session(client) -> None:
    await _publish(client, bedtime=None)
    resp = await _open(client, session_id="missing-session")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "REJECTED_SESSION_NOT_FOUND"


async def test_open_appeal_rejects_session_of_another_user(client) -> None:
    sid = await _exhaust_daily(client)
    resp = await _open(client, user_id="someone-else", session_id=sid)
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "REJECTED_APPEAL_INVALID"


async def test_open_appeal_rejects_non_appealable_reason(client) -> None:
    await _publish(client, bedtime=None)
    resp = await _open(client, denial_reason="DENIED_SESSION_LIMIT_REACHED")
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "REJECTED_APPEAL_NOT_APPEALABLE"


async def test_duplicate_open_rejected(client) -> None:
    """重复立案：同一拒绝事件只能有一个案件，结案后也只能复开。"""
    sid = await _exhaust_daily(client)
    first = await _open(client, session_id=sid)
    assert first.status_code == 201
    case_id = first.json()["id"]

    dup = await _open(client, session_id=sid)
    assert dup.status_code == 409
    assert dup.json()["detail"]["reason"] == "REJECTED_APPEAL_DUPLICATE"

    # 裁决结案后仍不能就同一事件重复立案。
    assert (await _claim(client, case_id)).status_code == 200
    assert (await _adjudicate(client, case_id, verdict="uphold")).status_code == 200
    again = await _open(client, session_id=sid)
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "REJECTED_APPEAL_DUPLICATE"


# --- 认领 -------------------------------------------------------------------


async def test_concurrent_claim_single_winner(file_client) -> None:
    """并发认领：N 个客服同时认领同一案件，只有一个成功。"""
    case_id = await _open_daily_case(file_client)
    results = await asyncio.gather(
        *[
            file_client.post(
                f"/v1/appeals/{case_id}/claim",
                headers=agent_headers(agent=f"agent-{i}"),
            )
            for i in range(5)
        ]
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(200) == 1
    assert statuses.count(409) == 4

    explain = await file_client.get(f"/v1/appeals/{case_id}", headers=headers())
    case = explain.json()["case"]
    assert case["state"] == "reviewing"
    assert case["assignee_id"] in {f"agent-{i}" for i in range(5)}


async def test_claim_twice_conflicts(client) -> None:
    case_id = await _open_daily_case(client)
    assert (await _claim(client, case_id)).status_code == 200
    second = await _claim(client, case_id, agent=AGENT_2)
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "REJECTED_APPEAL_STATE_CONFLICT"


# --- 权限隔离 ---------------------------------------------------------------


async def test_only_assignee_can_adjudicate_and_only_once(client) -> None:
    case_id = await _open_daily_case(client)

    # 未认领时不能裁决。
    early = await _adjudicate(client, case_id, verdict="uphold")
    assert early.status_code == 409

    assert (await _claim(client, case_id, agent=AGENT_1)).status_code == 200

    # 其他客服不能裁决别人认领的案件。
    forbidden = await _adjudicate(client, case_id, agent=AGENT_2, verdict="uphold")
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["reason"] == "REJECTED_APPEAL_FORBIDDEN"

    ok = await _adjudicate(client, case_id, verdict="uphold", reason="维持原决定")
    assert ok.status_code == 200
    assert ok.json()["state"] == "resolved"
    assert ok.json()["verdict"] == "uphold"
    assert ok.json()["resolved_at"] is not None

    # 裁决是一次性的：已裁决案件不能再次裁决。
    again = await _adjudicate(client, case_id, verdict="uphold")
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "REJECTED_APPEAL_STATE_CONFLICT"


async def test_tenant_isolation(client) -> None:
    """租户 B 看不到、认领不了、也裁决不了租户 A 的案件。"""
    sid = await _exhaust_daily(client, TENANT_A)
    case_id = (await _open(client, TENANT_A, session_id=sid)).json()["id"]

    # 租户 B 不能锚定租户 A 的会话立案。
    anchored = await _open(client, TENANT_B, session_id=sid)
    assert anchored.status_code == 404

    explained = await client.get(f"/v1/appeals/{case_id}", headers=headers(TENANT_B))
    assert explained.status_code == 404
    assert (await _claim(client, case_id, tenant=TENANT_B)).status_code == 404
    evidence = await client.post(
        f"/v1/appeals/{case_id}/evidence",
        json={"label": "x"},
        headers=agent_headers(TENANT_B),
    )
    assert evidence.status_code == 404
    adjudicated = await _adjudicate(client, case_id, tenant=TENANT_B, verdict="uphold")
    assert adjudicated.status_code == 404
    reopen = await client.post(
        f"/v1/appeals/{case_id}/reopen",
        json={"reason": "r"},
        headers=agent_headers(TENANT_B),
    )
    assert reopen.status_code == 404


# --- 裁决 -------------------------------------------------------------------


async def test_adjudication_does_not_rewrite_history(client) -> None:
    """裁决不得篡改历史使用量：账本与会话累计保持原样。"""
    sid = await _exhaust_daily(client)
    case_id = (await _open(client, session_id=sid)).json()["id"]
    await _claim(client, case_id)
    await _adjudicate(client, case_id, verdict="uphold")

    usage = await client.get(f"/v1/sessions/{sid}/usage", headers=headers())
    assert usage.json()["session"]["total_watched_seconds"] == 100
    assert usage.json()["extra"]["daily_today_seconds"] == 100


async def test_grant_exception_allows_then_expires(client, clock) -> None:
    """例外到期：期限内豁免每日额度，到期后拒绝恢复。"""
    sid = await _exhaust_daily(client)
    case_id = (await _open(client, session_id=sid)).json()["id"]
    await _claim(client, case_id)
    adj = await _adjudicate(
        client,
        case_id,
        verdict="grant_exception",
        exception_kind="daily_limit",
        exception_ttl_seconds=3600,
    )
    assert adj.status_code == 200
    assert adj.json()["verdict"] == "grant_exception"
    assert adj.json()["exception_id"]

    # 例外生效期间：每日额度被豁免，历史账本仍照常累计（不篡改）。
    started = await _start(client)
    body = started.json()
    assert body["reason"] == "SESSION_STARTED"
    waived = [s for s in body["trace"] if s["outcome"] == "waived"]
    assert waived and waived[0]["rule"] == "daily_limit"
    sid2 = body["session"]["id"]
    hb = await _hb(client, sid2, 1, 90)
    assert hb.json()["reason"] == "HEARTBEAT_APPLIED"
    assert hb.json()["extra"]["credited_seconds"] == 90  # 不再受每日 100s 截断

    explain = await client.get(f"/v1/appeals/{case_id}", headers=headers())
    active = explain.json()["active_exception"]
    assert active["kind"] == "daily_limit"
    assert active["active"] is True

    # 到期后豁免失效，每日额度再次拒绝开播。
    clock.advance(3601)
    await client.post(f"/v1/sessions/{sid2}/end", headers=headers())
    denied = await _start(client)
    assert denied.json()["reason"] == "DENIED_DAILY_LIMIT_REACHED"
    expired = await client.get(f"/v1/appeals/{case_id}", headers=headers())
    assert expired.json()["active_exception"] is None


async def test_grant_exception_kind_must_match_appealed_denial(client) -> None:
    """授权范围：年龄申诉不能发放每日额度例外。"""
    await _publish(client, min_age=18, bedtime=None)
    case_id = (
        await _open(
            client,
            denial_reason="DENIED_UNDER_MIN_AGE",
            birth_date=birth_for_age(15).isoformat(),
        )
    ).json()["id"]
    await _claim(client, case_id)
    bad = await _adjudicate(
        client,
        case_id,
        verdict="grant_exception",
        exception_kind="daily_limit",
        exception_ttl_seconds=60,
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["reason"] == "REJECTED_APPEAL_INVALID"


async def test_grant_exception_requires_bounded_ttl(client) -> None:
    case_id = await _open_daily_case(client)
    await _claim(client, case_id)

    missing = await _adjudicate(
        client, case_id, verdict="grant_exception", exception_kind="daily_limit"
    )
    assert missing.status_code == 422

    too_long = await _adjudicate(
        client,
        case_id,
        verdict="grant_exception",
        exception_kind="daily_limit",
        exception_ttl_seconds=31 * 24 * 3600,
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["reason"] == "REJECTED_APPEAL_INVALID"


async def test_bedtime_appeal_with_exception(client, clock) -> None:
    """休息时段拒绝的申诉、例外发放与到期。"""
    clock.set(datetime(2026, 7, 24, 23, 0, 0, tzinfo=UTC))
    await _publish(
        client,
        min_age=None,
        daily_limit_seconds=None,
        session_limit_seconds=None,
        bedtime=("22:00:00", "06:00:00"),
    )
    denied = await _start(client)
    assert denied.json()["reason"] == "DENIED_BEDTIME_CURFEW"

    case_id = (
        await _open(
            client,
            denial_reason="DENIED_BEDTIME_CURFEW",
            birth_date=birth_for_age(20).isoformat(),
        )
    ).json()["id"]
    await _claim(client, case_id)
    adj = await _adjudicate(
        client,
        case_id,
        verdict="grant_exception",
        exception_kind="bedtime",
        exception_ttl_seconds=3600,
    )
    assert adj.status_code == 200

    started = await _start(client)
    assert started.json()["reason"] == "SESSION_STARTED"
    sid = started.json()["session"]["id"]

    # 例外到期后仍在休息时段内，拒绝恢复。
    clock.advance(3601)
    await client.post(f"/v1/sessions/{sid}/end", headers=headers())
    again = await _start(client)
    assert again.json()["reason"] == "DENIED_BEDTIME_CURFEW"


async def test_correct_profile_verdict_fixes_future_starts(client) -> None:
    """纠正资料：平台档案以裁决为准，覆盖客户端上报值。"""
    await _publish(client, min_age=18, bedtime=None)
    wrong_birth = birth_for_age(15).isoformat()
    denied = await client.post(
        "/v1/sessions",
        json={"user_id": "u9", "birth_date": wrong_birth},
        headers=headers(),
    )
    assert denied.json()["reason"] == "DENIED_UNDER_MIN_AGE"

    case_id = (
        await _open(
            client,
            user_id="u9",
            denial_reason="DENIED_UNDER_MIN_AGE",
            birth_date=wrong_birth,
        )
    ).json()["id"]
    await _claim(client, case_id)
    corrected = birth_for_age(20).isoformat()
    adj = await _adjudicate(
        client,
        case_id,
        verdict="correct_profile",
        corrected_birth_date=corrected,
        reason="用户提交身份证明，出生日期登记有误",
    )
    assert adj.status_code == 200
    assert adj.json()["correction"] == {"birth_date": corrected}

    # 客户端仍上报旧的错误日期，平台以纠正后的资料为准。
    started = await client.post(
        "/v1/sessions",
        json={"user_id": "u9", "birth_date": wrong_birth},
        headers=headers(),
    )
    body = started.json()
    assert body["reason"] == "SESSION_STARTED"
    assert body["session"]["birth_date"] == corrected


async def test_uphold_verdict_rejects_side_effect_payloads(client) -> None:
    case_id = await _open_daily_case(client)
    await _claim(client, case_id)
    resp = await _adjudicate(
        client,
        case_id,
        verdict="uphold",
        exception_kind="daily_limit",
        exception_ttl_seconds=60,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "REJECTED_APPEAL_INVALID"


# --- 补证 -------------------------------------------------------------------


async def test_evidence_supplement_until_resolved(client) -> None:
    case_id = (
        await _open(
            client,
            session_id=await _exhaust_daily(client),
            evidence=[{"label": "初始材料", "detail": "截图"}],
        )
    ).json()["id"]

    added = await client.post(
        f"/v1/appeals/{case_id}/evidence",
        json={"label": "补充说明", "detail": "文字说明"},
        headers=agent_headers(),
    )
    assert added.status_code == 201
    assert added.json()["added_by"] == AGENT_1

    explain = await client.get(f"/v1/appeals/{case_id}", headers=headers())
    assert [e["label"] for e in explain.json()["evidence"]] == ["初始材料", "补充说明"]

    # 裁决后不能再补证，需先复开。
    await _claim(client, case_id)
    await _adjudicate(client, case_id, verdict="uphold")
    late = await client.post(
        f"/v1/appeals/{case_id}/evidence",
        json={"label": "迟到材料"},
        headers=agent_headers(),
    )
    assert late.status_code == 409


# --- 复开 -------------------------------------------------------------------


async def test_reopen_returns_case_to_queue(client) -> None:
    sid = await _exhaust_daily(client)
    case_id = (
        await _open(client, session_id=sid, evidence=[{"label": "初始材料"}])
    ).json()["id"]
    await _claim(client, case_id, agent=AGENT_1)
    await _adjudicate(client, case_id, agent=AGENT_1, verdict="uphold", reason="维持")

    reopened = await client.post(
        f"/v1/appeals/{case_id}/reopen",
        json={"reason": "用户补充了新证据"},
        headers=agent_headers(),
    )
    assert reopened.status_code == 200
    body = reopened.json()
    assert body["state"] == "opened"
    assert body["assignee_id"] is None
    assert body["verdict"] is None
    assert body["resolved_at"] is None

    # 复开后可由其他客服重新认领并重新裁决。
    assert (await _claim(client, case_id, agent=AGENT_2)).status_code == 200
    adj = await _adjudicate(
        client,
        case_id,
        agent=AGENT_2,
        verdict="grant_exception",
        exception_kind="daily_limit",
        exception_ttl_seconds=600,
        reason="重新评估后发放例外",
    )
    assert adj.status_code == 200

    explain = await client.get(f"/v1/appeals/{case_id}", headers=headers())
    actions = [e["action"] for e in explain.json()["timeline"]]
    assert actions == [
        "opened",
        "evidence_added",
        "claimed",
        "adjudicated",
        "reopened",
        "claimed",
        "adjudicated",
    ]


async def test_reopen_requires_resolved_state(client) -> None:
    case_id = await _open_daily_case(client)
    resp = await client.post(
        f"/v1/appeals/{case_id}/reopen",
        json={"reason": "r"},
        headers=agent_headers(),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "REJECTED_APPEAL_STATE_CONFLICT"
