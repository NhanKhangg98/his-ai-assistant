"""Typer CLI for HIS Knowledge Base: his-kb ingest | search | status | eval."""

from __future__ import annotations

import json

import typer

from app.config import get_settings
from app.db.session import init_db, session_scope
from app.kb.eval_service import EvalService
from app.kb.ingest_service import IncrementalIngestService
from app.kb.search_service import HybridSearchService
from app.security.log_filter import install_global_log_sanitizer

app = typer.Typer(
    name="his-kb",
    help="HIS 315 Knowledge Base CLI: ingest docs & staging code, RBAC search, status & eval.",
    add_completion=False,
)


@app.command("ingest")
def cmd_ingest(
    repo_path: str = typer.Option(
        None,
        "--repo-path",
        help="Path to HIS staging repository (defaults to HIS_REPO_PATH).",
    ),
    branch: str = typer.Option(
        None,
        "--branch",
        help="Target Git branch to index (defaults to staging).",
    ),
) -> None:
    """Incrementally ingest HIS documents (.md, .docx, .pdf) and staging code (.cs, .ts, .tsx)."""
    install_global_log_sanitizer()
    settings = get_settings()
    init_db(settings.database_url)
    target_repo = repo_path or settings.his_repo_path
    target_branch = branch or settings.his_staging_branch

    with session_scope(settings.database_url) as session:
        service = IncrementalIngestService(session)
        summary = service.ingest_repository(repo_path=target_repo, branch=target_branch)
        typer.echo(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))


@app.command("search")
def cmd_search(
    query: str = typer.Argument(..., help="Natural language or technical search query."),
    role: str = typer.Option(
        "Dev",
        "--role",
        "-r",
        help="Caller RBAC role: BA | QC | Dev (BA/QC only see doc + code_map).",
    ),
    top_k: int = typer.Option(5, "--top-k", "-k", help="Number of results to return (1..20)."),
    his_module: str | None = typer.Option(
        None, "--module", "-m", help="Optional HIS module filter (Registration, Pharmacy, Billing)."
    ),
) -> None:
    """Search HIS knowledge base using Hybrid RRF (Vector + Lexical) with RBAC pre-filtering."""
    install_global_log_sanitizer()
    settings = get_settings()
    init_db(settings.database_url)

    with session_scope(settings.database_url) as session:
        service = HybridSearchService(session)
        result = service.search(query=query, role=role, top_k=top_k, his_module=his_module)
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("status")
def cmd_status() -> None:
    """Display latest ingestion run, indexed staging commit SHA, chunk counts, and DLP stats."""
    install_global_log_sanitizer()
    settings = get_settings()
    init_db(settings.database_url)

    with session_scope(settings.database_url) as session:
        service = IncrementalIngestService(session)
        status_payload = service.get_status()
        typer.echo(json.dumps(status_payload, ensure_ascii=False, indent=2))


@app.command("eval")
def cmd_eval(
    dataset: str = typer.Option(
        "./sample_data/eval_questions.json",
        "--dataset",
        "-d",
        help="Path to evaluation dataset JSON (10 questions).",
    ),
    k: int = typer.Option(5, "--k", "-k", help="Top-K cutoff for Recall@K calculation."),
) -> None:
    """Run retrieval evaluation on 10 sample questions and print Recall@5."""
    install_global_log_sanitizer()
    settings = get_settings()
    init_db(settings.database_url)

    with session_scope(settings.database_url) as session:
        eval_svc = EvalService(session)
        report = eval_svc.evaluate(dataset_path=dataset, k=k)
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["passed"]:
            raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
