"""Part B Test Suite: 7-Step Workflow, Loop 6->3->4, Idempotency, Recovery, Git Guard & Quota."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.discord_adapter import get_discord_adapter
from app.adapters.git_adapter import (
    ProtectedBranchError,
    _validate_bot_write_branch,
    get_git_adapter,
)
from app.adapters.llm_router import (
    FallbackMockLLMProvider,
    LLMRouter,
    PrimaryMockLLMProvider,
)
from app.config import Settings
from app.db.models import (
    BackgroundTask,
    CodeMapEntry,
    IngestionRun,
    KbChunk,
    KbFile,
    LlmUsageLedger,
    ProcessedMessage,
    User,
    WorkflowAuditLog,
    WorkflowStory,
)
from app.kb.ingest_service import IncrementalIngestService
from app.main import get_worker
from sample_data.init_sample_repo import (
    CANARY_CCCD_1,
    CANARY_DB_PASSWORD,
    CANARY_PATIENT_NAME_1,
)


def test_full_7_step_story_workflow_with_step3_reject_and_step6_bug_loop(
    client: Any, db_session: Session
) -> None:
    """Run HIS-101 end-to-end through Steps 1..7 including Step 3 reject and Step 6->3->4 loop."""
    IncrementalIngestService(db_session).ingest_repository(
        "./sample_data/his_staging", branch="staging"
    )
    worker = get_worker()

    # --- STEP 1: BA Analyze & BA Approve ---
    r1 = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s1-analyze",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba analyze HIS-101",
        },
    )
    assert r1.status_code == 202
    task1_id = r1.json()["taskId"]

    # While Step 1 agent task is still in-flight (RUNNING_AGENT), early approval MUST return 409
    r_early = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s1-early-approve",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba approve HIS-101",
        },
    )
    assert r_early.status_code == 409
    assert r_early.json()["detail"]["errorCode"] == "AGENT_TASK_IN_PROGRESS"

    worker.drain_pending_tasks()
    t1_info = client.get(f"/tasks/{task1_id}").json()
    assert t1_info["status"] == "COMPLETED"
    assert len(t1_info["result"]["missingAc"]) >= 1

    r1_app = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s1-approve",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba approve HIS-101",
        },
    )
    assert r1_app.status_code == 200
    assert r1_app.json()["currentStep"] == 2

    # --- STEP 2: QC Design-Test & QC Approve ---
    r2 = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s2-design",
            "userId": "qc_user",
            "channel": "his-dev",
            "text": "/qc design-test HIS-101",
        },
    )
    assert r2.status_code == 202
    worker.drain_pending_tasks()

    r2_app = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s2-approve",
            "userId": "qc_user",
            "channel": "his-dev",
            "text": "/qc approve HIS-101",
        },
    )
    assert r2_app.status_code == 200
    assert r2_app.json()["currentStep"] == 3

    # --- STEP 3: Dev Plan -> Dev Reject (stays at Step 3) -> Dev Plan -> Dev Approve ---
    r3_a = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s3-plan-1",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev plan HIS-101",
        },
    )
    assert r3_a.status_code == 202
    worker.drain_pending_tasks()

    r3_rej = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s3-reject",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev reject HIS-101 Cần bổ sung xử lý ca cấp cứu ngoại trú",
        },
    )
    assert r3_rej.status_code == 200
    assert r3_rej.json()["currentStep"] == 3
    assert r3_rej.json()["stepStatus"] == "WAITING_AGENT"

    r3_b = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s3-plan-2",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev plan HIS-101",
        },
    )
    assert r3_b.status_code == 202
    worker.drain_pending_tasks()

    r3_app = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s3-approve",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev approve HIS-101",
        },
    )
    assert r3_app.status_code == 200
    assert r3_app.json()["currentStep"] == 4

    # --- STEP 4: Dev Implement (creates bot/HIS-101, opens PR into develop -> Step 5) ---
    r4 = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s4-impl-1",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev implement HIS-101",
        },
    )
    assert r4.status_code == 202
    worker.drain_pending_tasks()

    story_after_s4 = client.get("/stories/HIS-101").json()
    assert story_after_s4["currentStep"] == 5
    assert story_after_s4["stepStatus"] == "WAITING_AGENT"
    assert story_after_s4["gitBranch"] == "bot/HIS-101"
    assert story_after_s4["prId"] == "PR-HIS-101"
    assert story_after_s4["prStatus"] == "OPEN"

    # --- STEP 5: Dev Summarize-PR & Confirm-Merge ---
    r5 = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s5-sum-1",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev summarize-pr HIS-101",
        },
    )
    assert r5.status_code == 202
    worker.drain_pending_tasks()

    r5_merge = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s5-merge-1",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev confirm-merge HIS-101",
        },
    )
    assert r5_merge.status_code == 200
    assert r5_merge.json()["currentStep"] == 6

    # --- STEP 6: QC Test -> QC Bug (loops back to Step 3 with PII-masked bug report!) ---
    r6_test = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s6-test-1",
            "userId": "qc_user",
            "channel": "his-dev",
            "text": "/qc test HIS-101",
        },
    )
    assert r6_test.status_code == 202
    worker.drain_pending_tasks()

    r6_bug = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s6-bug-1",
            "userId": "qc_user",
            "channel": "his-dev",
            "text": (
                f"/qc bug HIS-101 | NullReferenceException khi kiểm tra bệnh nhân "
                f"{CANARY_PATIENT_NAME_1} CCCD {CANARY_CCCD_1} Password={CANARY_DB_PASSWORD}"
            ),
        },
    )
    assert r6_bug.status_code == 202
    worker.drain_pending_tasks()

    story_after_bug = client.get("/stories/HIS-101").json()
    assert story_after_bug["currentStep"] == 3
    assert story_after_bug["stepStatus"] == "WAITING_AGENT"
    assert story_after_bug["loopCount"] == 1

    # --- LOOP 3 -> 4 -> 5 -> 6 -> 7 -> DONE ---
    client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s3-plan-loop",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev plan HIS-101",
        },
    )
    worker.drain_pending_tasks()
    client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s3-app-loop",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev approve HIS-101",
        },
    )
    client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s4-impl-loop",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev implement HIS-101",
        },
    )
    worker.drain_pending_tasks()

    # Verify idempotent PR on loop: only 1 open PR exists for bot/HIS-101 -> develop
    open_prs = worker.git_adapter.list_open_pull_requests(head_branch="bot/HIS-101")
    assert len(open_prs) == 1
    assert open_prs[0].pr_id == "PR-HIS-101"

    client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s5-sum-loop",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev summarize-pr HIS-101",
        },
    )
    worker.drain_pending_tasks()
    client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s5-merge-loop",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev confirm-merge HIS-101",
        },
    )
    client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s6-test-loop",
            "userId": "qc_user",
            "channel": "his-dev",
            "text": "/qc test HIS-101",
        },
    )
    worker.drain_pending_tasks()

    r6_pass = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s6-pass",
            "userId": "qc_user",
            "channel": "his-dev",
            "text": "/qc pass HIS-101",
        },
    )
    assert r6_pass.status_code == 200
    assert r6_pass.json()["currentStep"] == 7

    r7_uat = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s7-uat",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba uat HIS-101",
        },
    )
    assert r7_uat.status_code == 202
    worker.drain_pending_tasks()

    r7_close = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-101-s7-close",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba close HIS-101",
        },
    )
    assert r7_close.status_code == 200
    assert r7_close.json()["stepStatus"] == "DONE"

    # Verify Discord notifications & @role tags and Audit Logs
    assert len(get_discord_adapter().sent_messages) >= 10
    audit_resp = client.get("/audit-logs?story_key=HIS-101").json()
    assert audit_resp["totalLogs"] >= 15


def test_rbac_role_gate_and_step_jumping_and_idempotency(client: Any, db_session: Session) -> None:
    """Verify 403 on wrong role, 409 on step jumping, 202 replay, and 422 on mutated payload."""
    # 1. Wrong Role: Dev trying to run Step 1 BA command -> 403 Forbidden
    r_wrong_role = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-role-403",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/ba analyze HIS-102",
        },
    )
    assert r_wrong_role.status_code == 403
    assert r_wrong_role.json()["detail"]["errorCode"] == "ROLE_FORBIDDEN"

    # 2. Step Jumping: Story HIS-102 is at Step 1, Dev tries Step 3 '/dev plan' -> 409 Conflict
    r_jump = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-step-409",
            "userId": "dev_user",
            "channel": "his-dev",
            "text": "/dev plan HIS-102",
        },
    )
    assert r_jump.status_code == 409
    assert r_jump.json()["detail"]["errorCode"] == "INVALID_STEP_TRANSITION"

    # 3. Valid command + Idempotent Replay with same messageId -> returns identical taskId
    r_first = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-idem-001",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba analyze HIS-102",
        },
    )
    assert r_first.status_code == 202
    first_task_id = r_first.json()["taskId"]
    assert r_first.json()["idempotentReplay"] is False

    r_replay = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-idem-001",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba analyze HIS-102",
        },
    )
    assert r_replay.status_code == 202
    assert r_replay.json()["taskId"] == first_task_id
    assert r_replay.json()["idempotentReplay"] is True

    # 4. Same messageId with mutated payload -> 422 Unprocessable Entity
    r_mismatch = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-idem-001",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba approve HIS-102",
        },
    )
    assert r_mismatch.status_code == 422
    assert r_mismatch.json()["detail"]["errorCode"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"


def test_worker_crash_restart_recovery(client: Any, db_session: Session) -> None:
    """Verify tasks interrupted in RUNNING state are reclaimed and completed after restart."""
    resp = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-crash-001",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba analyze HIS-102",
        },
    )
    assert resp.status_code == 202
    task_id = resp.json()["taskId"]

    # Simulate a crash mid-execution where status was left as 'RUNNING'
    task_row = db_session.scalar(select(BackgroundTask).where(BackgroundTask.id == task_id))
    assert task_row is not None
    task_row.status = "RUNNING"
    task_row.attempts = 1
    db_session.commit()

    worker = get_worker()
    reclaimed = worker.recover_interrupted_tasks(force_reclaim_running=True)
    assert reclaimed == 1

    processed = worker.drain_pending_tasks()
    assert task_id in processed

    task_status = client.get(f"/tasks/{task_id}").json()
    assert task_status["status"] == "COMPLETED"
    assert task_status["attempts"] == 2


def test_git_branch_guard_blocks_all_10_attack_vectors_and_prompt_injection(
    client: Any, db_session: Session
) -> None:
    """Verify all 10 Git attack vectors raise ProtectedBranchError and block Injection."""
    git_adapter = get_git_adapter()

    # 1. Verify GitAdapter has NO merge() method
    assert not hasattr(git_adapter, "merge")

    # 2. Verify all 9 branch/refspec attack vectors raise ProtectedBranchError
    branch_attack_vectors = [
        "main",
        "develop",
        "staging",
        "refs/heads/main",
        "HEAD:develop",
        "+bot/HIS-101:main",
        "bot/../main",
        "bot/main",
        "--force",
    ]
    for malicious_branch in branch_attack_vectors:
        with pytest.raises(ProtectedBranchError):
            _validate_bot_write_branch(malicious_branch)
        with pytest.raises(ProtectedBranchError):
            git_adapter.ensure_bot_branch(malicious_branch)
        with pytest.raises(ProtectedBranchError):
            git_adapter.push_branch(malicious_branch)

    # 3. Attack vector #10: Opening a PR targeting 'main' or 'staging' instead of 'develop'
    git_adapter.ensure_bot_branch("bot/HIS-101", from_ref="develop")
    for forbidden_base in ("main", "staging", "master"):
        with pytest.raises(ProtectedBranchError):
            git_adapter.create_or_update_pull_request(
                head_branch="bot/HIS-101",
                base_branch=forbidden_base,
                title="Malicious PR",
                body="Attempt to target protected branch",
            )

    # 4. Prompt Injection Ticket (HIS-INJECT) test through Step 4
    main_sha_before = git_adapter.get_branch_commit_sha("main")
    develop_sha_before = git_adapter.get_branch_commit_sha("develop")
    staging_sha_before = git_adapter.get_branch_commit_sha("staging")

    worker = get_worker()
    for msg_id, uid, cmd in [
        ("inj-1", "ba_user", "/ba analyze HIS-INJECT"),
        ("inj-2", "ba_user", "/ba approve HIS-INJECT"),
        ("inj-3", "qc_user", "/qc design-test HIS-INJECT"),
        ("inj-4", "qc_user", "/qc approve HIS-INJECT"),
        ("inj-5", "dev_user", "/dev plan HIS-INJECT"),
        ("inj-6", "dev_user", "/dev approve HIS-INJECT"),
        ("inj-7", "dev_user", "/dev implement HIS-INJECT"),
    ]:
        r = client.post(
            "/chat/commands",
            json={"messageId": msg_id, "userId": uid, "channel": "sec-chan", "text": cmd},
        )
        assert r.status_code in (200, 202)
        worker.drain_pending_tasks()

    # Verify main, develop, and staging commit SHAs were NEVER modified by the bot!
    assert git_adapter.get_branch_commit_sha("main") == main_sha_before
    assert git_adapter.get_branch_commit_sha("develop") == develop_sha_before
    assert git_adapter.get_branch_commit_sha("staging") == staging_sha_before

    story_inject = client.get("/stories/HIS-INJECT").json()
    assert story_inject["gitBranch"] == "bot/HIS-INJECT"
    assert story_inject["currentStep"] == 5

    # Verify 0% leakage of Canary PII/Secrets from HIS-INJECT across all 10 tables
    for model in (
        User,
        IngestionRun,
        KbFile,
        KbChunk,
        CodeMapEntry,
        WorkflowStory,
        ProcessedMessage,
        BackgroundTask,
        WorkflowAuditLog,
        LlmUsageLedger,
    ):
        for row in db_session.scalars(select(model)).all():
            dump_str = str(row.__dict__)
            assert CANARY_PATIENT_NAME_1 not in dump_str
            assert CANARY_CCCD_1 not in dump_str
            assert CANARY_DB_PASSWORD not in dump_str


def test_llm_router_failover_and_monthly_token_and_cost_quotas(
    client: Any, db_session: Session
) -> None:
    """Verify Primary -> Fallback LLM failover and Monthly Token & Cost USD Quota enforcement."""
    worker = get_worker()

    # 1. Simulate Primary LLM Failure -> Fallback LLM succeeds seamlessly
    failing_router = LLMRouter(
        session=db_session,
        primary_provider=PrimaryMockLLMProvider(simulate_failure=True),
        fallback_provider=FallbackMockLLMProvider(),
    )
    res_failover = failing_router.generate_with_governance(
        user_id="ba_user",
        user_role="BA",
        story_key="HIS-101",
        prompt="Kiểm tra failover sang Fallback LLM",
        context={"action": "analyze", "story_key": "HIS-101"},
    )
    assert res_failover.used_fallback is True
    assert res_failover.provider_name == "fallback-mock"
    db_session.commit()

    # 2. Enforce Monthly Token Quota ceiling -> blocks LLM call and restores story status
    tight_token_settings = Settings(
        MONTHLY_USER_TOKEN_QUOTA=10,
        MONTHLY_USER_COST_USD_QUOTA=100.0,
        MONTHLY_ROLE_TOKEN_QUOTA=10,
        MONTHLY_ROLE_COST_USD_QUOTA=100.0,
    )
    tight_router = LLMRouter(session=db_session, settings=tight_token_settings)

    r_cmd = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-quota-block-1",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba analyze HIS-101",
        },
    )
    assert r_cmd.status_code == 202
    task_id = r_cmd.json()["taskId"]

    worker.drain_pending_tasks(llm_router_override=tight_router)

    t_info = client.get(f"/tasks/{task_id}").json()
    assert t_info["status"] == "FAILED"
    assert "quota exceeded" in (t_info["error"] or "").lower()

    # Story step_status must be restored back to WAITING_AGENT (not stuck in RUNNING_AGENT)
    s_info = client.get("/stories/HIS-101").json()
    assert s_info["stepStatus"] == "WAITING_AGENT"

    # Audit log must record QUOTA_BLOCKED
    logs = client.get("/audit-logs?story_key=HIS-101").json()["logs"]
    assert any(lg["outcomeStatus"] == "QUOTA_BLOCKED" for lg in logs)

    # 3. Verify Monthly User Cost USD Quota and Monthly Role Cost USD Quota ceilings
    from app.adapters.llm_router import QuotaExceededError

    tight_user_cost_router = LLMRouter(
        session=db_session,
        settings=Settings(
            MONTHLY_USER_TOKEN_QUOTA=999_999,
            MONTHLY_USER_COST_USD_QUOTA=0.000001,
            MONTHLY_ROLE_TOKEN_QUOTA=999_999,
            MONTHLY_ROLE_COST_USD_QUOTA=100.0,
        ),
    )
    with pytest.raises(QuotaExceededError, match="Monthly USD cost quota exceeded for user"):
        tight_user_cost_router.generate_with_governance(
            user_id="ba_user",
            user_role="BA",
            story_key="HIS-101",
            prompt="Check user cost quota",
        )

    tight_role_cost_router = LLMRouter(
        session=db_session,
        settings=Settings(
            MONTHLY_USER_TOKEN_QUOTA=999_999,
            MONTHLY_USER_COST_USD_QUOTA=100.0,
            MONTHLY_ROLE_TOKEN_QUOTA=999_999,
            MONTHLY_ROLE_COST_USD_QUOTA=0.000001,
        ),
    )
    with pytest.raises(QuotaExceededError, match="Monthly USD cost quota exceeded for role"):
        tight_role_cost_router.generate_with_governance(
            user_id="ba_user",
            user_role="BA",
            story_key="HIS-101",
            prompt="Check role cost quota",
        )


def test_idempotency_mutated_pii_payload_422_and_pr_persistence_across_restart(
    client: Any, db_session: Session, tmp_path: Any
) -> None:
    """Verify mutated PII payloads with same messageId return 422 and PR state survives restart."""
    from app.adapters.git_adapter import LocalRepoGitAdapter
    from app.workflow.command_parser import parse_chat_command

    # 1. Story keys like BA-1001 or BN-2025 must not be corrupted by DLP before command parsing
    parsed_ba = parse_chat_command("/ba analyze BA-1001")
    assert parsed_ba.story_key == "BA-1001"
    parsed_bn = parse_chat_command("/ba analyze BN-2025")
    assert parsed_bn.story_key == "BN-2025"

    # 2. Two requests with the same messageId but DIFFERENT raw CCCD numbers must return 422
    r1 = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-pii-diff-1",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba analyze HIS-102 Bệnh nhân CCCD 079090001234",
        },
    )
    assert r1.status_code == 202

    r2 = client.post(
        "/chat/commands",
        json={
            "messageId": "msg-pii-diff-1",
            "userId": "ba_user",
            "channel": "his-dev",
            "text": "/ba analyze HIS-102 Bệnh nhân CCCD 001088999888",
        },
    )
    assert r2.status_code == 422
    assert r2.json()["detail"]["errorCode"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"

    # Ensure neither raw CCCD was stored in processed_messages
    pm = db_session.scalar(
        select(ProcessedMessage).where(ProcessedMessage.message_id == "msg-pii-diff-1")
    )
    assert pm is not None
    assert "079090001234" not in pm.sanitized_text
    assert "001088999888" not in pm.sanitized_text
    assert "[MASKED_CCCD]" in pm.sanitized_text

    # 3. Verify LocalRepoGitAdapter persists PR state across adapter re-instantiation (restart)
    repo_dir = tmp_path / "restart_repo"
    adapter_before = LocalRepoGitAdapter(repo_dir)
    adapter_before.ensure_bot_branch("bot/HIS-999", from_ref="develop")
    pr1 = adapter_before.create_or_update_pull_request(
        head_branch="bot/HIS-999",
        base_branch="develop",
        title="[HIS-999] Initial PR",
        body="Body before restart",
    )
    assert pr1.pr_id == "PR-HIS-999"

    # Simulate process restart by creating a brand new LocalRepoGitAdapter instance
    adapter_after = LocalRepoGitAdapter(repo_dir)
    open_prs = adapter_after.list_open_pull_requests(head_branch="bot/HIS-999")
    assert len(open_prs) == 1
    assert open_prs[0].pr_id == "PR-HIS-999"
