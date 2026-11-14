"""Part A & All-Boundary DLP Test Suite: Chunkers, Incremental Ingest, RBAC Search, Eval & DLP."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import app as cli_app
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
from app.kb.code_chunker import CodeChunker, _split_oversized_code_chunk, should_ignore_path
from app.kb.eval_service import EvalService
from app.kb.ingest_service import IncrementalIngestService
from app.kb.search_service import HybridSearchService
from app.security.dlp import AllBoundaryDLP
from app.security.log_filter import SanitizingLogFilter, install_global_log_sanitizer
from sample_data.init_sample_repo import (
    CANARY_BHYT_1,
    CANARY_BHYT_2,
    CANARY_CCCD_1,
    CANARY_DB_PASSWORD,
    CANARY_EMAIL_1,
    CANARY_OPENAI_KEY,
    CANARY_PATIENT_NAME_1,
    CANARY_PATIENT_NAME_2,
    CANARY_PHONE_1,
)


def test_doc_and_code_chunkers_preserve_methods_and_skip_ignored_dirs() -> None:
    """Verify State-Machine Lexer does not split inside methods and skips bin/obj/node_modules."""
    assert should_ignore_path("src/HIS.Registration.Api/bin/Debug/IgnoredArtifact.cs")
    assert should_ignore_path("src/HIS.Registration.Api/obj/Debug/IgnoredObj.cs")
    assert should_ignore_path("frontend/node_modules/fake-lib/IgnoredModule.ts")

    repo_dir = Path("./sample_data/his_staging").resolve()
    ctrl_path = (
        repo_dir
        / "src"
        / "HIS.Registration.Api"
        / "Controllers"
        / "PatientRegistrationController.cs"
    )
    chunks, code_maps = CodeChunker.chunk_code_file(
        ctrl_path,
        "src/HIS.Registration.Api/Controllers/PatientRegistrationController.cs",
    )

    assert len(chunks) == 2
    symbols = [c.heading_or_symbol for c in chunks]
    assert "PatientRegistrationController.RegisterOutpatientBhytAsync" in symbols
    assert "PatientRegistrationController.VerifyFiveYearContinuousBhytAsync" in symbols

    # Verify method with embedded braces in strings/comments is kept 100% intact
    reg_chunk = next(
        c
        for c in chunks
        if c.heading_or_symbol == "PatientRegistrationController.RegisterOutpatientBhytAsync"
    )
    assert '[HttpPost("outpatient-bhyt")]' in reg_chunk.content
    assert "FiveYearApplied = qualifiesFiveYear" in reg_chunk.content

    # Verify Code Map extracted exact route and HTTP verb without leaking method body
    routes = {(cm.http_method, cm.route_path) for cm in code_maps}
    assert ("POST", "/api/v1/registration/outpatient-bhyt") in routes
    assert ("GET", "/api/v1/registration/bhyt-five-year-check/{cardNo}") in routes
    for cm in code_maps:
        assert "qualifiesFiveYear" not in cm.summary_text

    # Verify oversized method (>4000 chars) keeps signature header on every sub-chunk
    big_lines = (
        ["public void HugeMethod() {"] + [f"    int x{i} = {i};" for i in range(300)] + ["}"]
    )
    sub_chunks = _split_oversized_code_chunk(
        symbol="BigClass.HugeMethod",
        signature_header="BigClass.HugeMethod()",
        lines=big_lines,
        start_line=1,
        his_module="Core",
        rel_path="src/BigClass.cs",
    )
    assert len(sub_chunks) >= 2
    for sc in sub_chunks:
        assert "// Signature Context: BigClass.HugeMethod()" in sc.content


def test_incremental_ingestion_and_staging_commit_sha(db_session: Session) -> None:
    """Verify incremental SHA-256 diffing (added/skipped/modified/deleted) and real Git SHA."""
    repo_dir = Path("./sample_data/his_staging").resolve()
    ingest_svc = IncrementalIngestService(db_session)

    # 1. Initial ingest indexes all 9 valid files (ignoring bin, obj, node_modules)
    run1 = ingest_svc.ingest_repository(repo_dir, branch="staging")
    assert len(run1.commit_sha) == 40
    assert run1.files_added == 9
    assert run1.files_modified == 0
    assert run1.files_deleted == 0
    assert run1.files_skipped == 0
    assert run1.secrets_redacted >= 2
    assert run1.pii_masked >= 8

    # Verify ignored files were never indexed
    indexed_paths = set(db_session.scalars(select(KbFile.file_path)).all())
    assert not any("bin/" in p or "obj/" in p or "node_modules/" in p for p in indexed_paths)

    # 2. Second ingest with no changes skips all 9 files
    run2 = ingest_svc.ingest_repository(repo_dir, branch="staging")
    assert run2.files_added == 0
    assert run2.files_modified == 0
    assert run2.files_deleted == 0
    assert run2.files_skipped == 9

    # 3. Add a temporary markdown file, ingest -> 1 added, 9 skipped; then delete -> 1 deleted
    temp_doc = repo_dir / "docs" / "temp_incremental_note.md"
    try:
        temp_doc.write_text(
            "# Quy định tạm thời Registration\nNội dung kiểm thử nạp tăng dần.",
            encoding="utf-8",
        )
        run3 = ingest_svc.ingest_repository(repo_dir, branch="staging")
        assert run3.files_added == 1
        assert run3.files_skipped == 9

        temp_doc.write_text(
            "# Quy định tạm thời Registration (Đã cập nhật)\nNội dung sửa đổi.",
            encoding="utf-8",
        )
        run4 = ingest_svc.ingest_repository(repo_dir, branch="staging")
        assert run4.files_modified == 1
        assert run4.files_skipped == 9
    finally:
        if temp_doc.exists():
            temp_doc.unlink()

    run5 = ingest_svc.ingest_repository(repo_dir, branch="staging")
    assert run5.files_deleted == 1
    assert run5.files_skipped == 9


def test_rbac_search_pre_filtering_and_citations(client: Any, db_session: Session) -> None:
    """Verify BA and QC only receive doc + code_map (0 source_code), while Dev sees source_code."""
    ingest_svc = IncrementalIngestService(db_session)
    ingest_svc.ingest_repository("./sample_data/his_staging", branch="staging")

    # Query specifically targeting C# method implementation details
    code_query = "RegisterOutpatientBhytAsync qualifiesFiveYear CopayRate OutpatientVisit"

    for restricted_role, uid in (("BA", "ba_user"), ("QC", "qc_user")):
        resp = client.post(
            "/search",
            json={"userId": uid, "role": restricted_role, "query": code_query, "topK": 10},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == restricted_role
        assert len(data["results"]) > 0
        for item in data["results"]:
            assert item["chunkType"] in ("doc", "code_map")
            assert item["chunkType"] != "source_code"
            assert "file" in item["citation"]
            assert "heading" in item["citation"]

    # Dev role MUST receive source_code chunks for the exact same query
    dev_resp = client.post(
        "/search",
        json={"userId": "dev_user", "role": "Dev", "query": code_query, "topK": 10},
    )
    assert dev_resp.status_code == 200
    dev_data = dev_resp.json()
    dev_types = {item["chunkType"] for item in dev_data["results"]}
    assert "source_code" in dev_types


def test_eval_recall_at_5_and_cli_commands(db_session: Session) -> None:
    """Verify his-kb CLI (ingest, status, search, eval) and Recall@5 >= 0.80."""
    runner = CliRunner()

    res_ingest = runner.invoke(cli_app, ["ingest"])
    assert res_ingest.exit_code == 0

    res_status = runner.invoke(cli_app, ["status"])
    assert res_status.exit_code == 0
    assert '"branch": "staging"' in res_status.stdout

    res_search = runner.invoke(
        cli_app, ["search", "kiểm tra thông tuyến BHYT 5 năm liên tục", "--role", "BA"]
    )
    assert res_search.exit_code == 0
    assert '"chunkType": "source_code"' not in res_search.stdout

    eval_svc = EvalService(db_session)
    report = eval_svc.evaluate("./sample_data/eval_questions.json", k=5)
    assert report["total_questions"] == 10
    assert report["recall_at_5"] >= 0.80
    assert report["passed"] is True


def test_all_boundary_dlp_zero_leak_across_all_10_tables_and_logs(
    db_session: Session,
) -> None:
    """Verify 0% leakage of Canary Secrets and 3-layer VN Patient PII across DB & logs."""
    IncrementalIngestService(db_session).ingest_repository(
        "./sample_data/his_staging", branch="staging"
    )
    search_svc = HybridSearchService(db_session)
    _ = search_svc.search(
        query=(
            f"Tra cứu bệnh nhân {CANARY_PATIENT_NAME_1} CCCD {CANARY_CCCD_1} "
            f"BHYT {CANARY_BHYT_1} SĐT {CANARY_PHONE_1} Password={CANARY_DB_PASSWORD}"
        ),
        role="BA",
    )

    # Test free-text Vietnamese patient name without context label + JWT + Private Key
    raw_sample = (
        f"Hôm nay {CANARY_PATIENT_NAME_1} và Lê Minh Tuấn khám ngoại trú, "
        f"thẻ {CANARY_BHYT_1}, CCCD {CANARY_CCCD_1}, SĐT {CANARY_PHONE_1}, "
        f"email {CANARY_EMAIL_1}, sinh ngày 15/08/1985, hồ sơ BA-2026-009812, "
        f"key={CANARY_OPENAI_KEY}, jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        f"eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U "
        f"-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAtest\n-----END RSA PRIVATE KEY-----"
    )
    sanitized = AllBoundaryDLP.sanitize_text(raw_sample)
    for forbidden in (
        CANARY_PATIENT_NAME_1,
        "Lê Minh Tuấn",
        CANARY_BHYT_1,
        CANARY_CCCD_1,
        CANARY_PHONE_1,
        CANARY_EMAIL_1,
        "15/08/1985",
        "BA-2026-009812",
        CANARY_OPENAI_KEY,
        "MIIEpAIBAAKCAQEAtest",
    ):
        assert forbidden not in sanitized

    # Verify SanitizingLogFilter scrubs log records
    install_global_log_sanitizer()
    log_filter = SanitizingLogFilter()
    record = logging.LogRecord(
        name="his.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"Patient {CANARY_PATIENT_NAME_2} BHYT {CANARY_BHYT_2} Key {CANARY_OPENAI_KEY}",
        args=(),
        exc_info=None,
    )
    log_filter.filter(record)
    assert CANARY_PATIENT_NAME_2 not in str(record.msg)
    assert CANARY_BHYT_2 not in str(record.msg)
    assert CANARY_OPENAI_KEY not in str(record.msg)

    # Scan every row of all 10 tables in the database for any Canary Secret or PII
    canary_values = (
        CANARY_DB_PASSWORD,
        CANARY_OPENAI_KEY,
        CANARY_PATIENT_NAME_1,
        CANARY_PATIENT_NAME_2,
        CANARY_CCCD_1,
        CANARY_BHYT_1,
        CANARY_BHYT_2,
        CANARY_PHONE_1,
        CANARY_EMAIL_1,
    )
    all_models = (
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
    )
    for model in all_models:
        rows = db_session.scalars(select(model)).all()
        for row in rows:
            serialized = str(row.__dict__)
            for canary in canary_values:
                assert canary not in serialized, (
                    f"DLP leak detected in table {model.__tablename__}: found {canary!r}"
                )
