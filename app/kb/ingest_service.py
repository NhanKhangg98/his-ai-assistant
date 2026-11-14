"""Incremental Knowledge Base Ingestion Engine tracking staging Git commit SHA and SHA-256 diff."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.db.models import CodeMapEntry, IngestionRun, KbChunk, KbFile, utc_now
from app.kb.code_chunker import CodeChunker, CodeMapDraft, should_ignore_path
from app.kb.doc_chunker import DocumentChunker, RawChunkDraft
from app.kb.embeddings import DeterministicEmbedding, build_lexical_token_string
from app.security.dlp import AllBoundaryDLP
from sample_data.init_sample_repo import ensure_sample_git_repo

DOC_EXTENSIONS: frozenset[str] = frozenset({".md", ".docx", ".pdf"})
CODE_EXTENSIONS: frozenset[str] = frozenset({".cs", ".ts", ".tsx"})
SUPPORTED_EXTENSIONS: frozenset[str] = DOC_EXTENSIONS | CODE_EXTENSIONS


@dataclass(slots=True)
class IngestSummary:
    """Result returned by an incremental ingestion run."""

    run_id: str
    branch: str
    commit_sha: str
    repo_path: str
    files_added: int
    files_modified: int
    files_deleted: int
    files_skipped: int
    chunks_created: int
    code_maps_created: int
    secrets_redacted: int
    pii_masked: int
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "repo_path": self.repo_path,
            "files_added": self.files_added,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "files_skipped": self.files_skipped,
            "chunks_created": self.chunks_created,
            "code_maps_created": self.code_maps_created,
            "secrets_redacted": self.secrets_redacted,
            "pii_masked": self.pii_masked,
            "status": self.status,
        }


def resolve_git_branch_commit_sha(repo_dir: Path, branch: str = "staging") -> str:
    """Read the 40-char commit SHA of refs/heads/<branch> (auto-initializing sample repo)."""
    ensure_sample_git_repo(repo_dir)
    for ref_candidate in (f"refs/heads/{branch}", branch, "HEAD"):
        proc = subprocess.run(
            ["git", "rev-parse", ref_candidate],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        sha = proc.stdout.strip()
        if proc.returncode == 0 and len(sha) == 40:
            return sha
    raise RuntimeError(f"Could not resolve commit SHA for branch '{branch}' in {repo_dir}")


def compute_file_sha256(file_path: Path) -> str:
    """Return hex SHA-256 digest of file bytes."""
    h = hashlib.sha256()
    h.update(file_path.read_bytes())
    return h.hexdigest()


class IncrementalIngestService:
    """Scans staging repository, processes only added/modified/deleted files, and applies DLP."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def ingest_repository(self, repo_path: str | Path, branch: str = "staging") -> IngestSummary:
        repo_dir = Path(repo_path).resolve()
        commit_sha = resolve_git_branch_commit_sha(repo_dir, branch=branch)

        run = IngestionRun(
            branch=branch,
            commit_sha=commit_sha,
            repo_path=str(repo_dir),
            status="RUNNING",
        )
        self.session.add(run)
        self.session.flush()

        # Discover eligible files on disk (excluding bin/, obj/, node_modules/, .git/)
        disk_files: dict[str, Path] = {}
        for p in sorted(repo_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(repo_dir).as_posix()
            if should_ignore_path(rel):
                continue
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                disk_files[rel] = p

        existing_files = {f.file_path: f for f in self.session.scalars(select(KbFile)).all()}

        added_count = 0
        modified_count = 0
        deleted_count = 0
        skipped_count = 0
        chunks_created = 0
        code_maps_created = 0
        secrets_redacted = 0
        pii_masked = 0

        # 1. Handle DELETED files
        for old_rel, old_kb_file in list(existing_files.items()):
            if old_rel not in disk_files:
                self.session.execute(delete(KbChunk).where(KbChunk.file_id == old_kb_file.id))
                self.session.execute(
                    delete(CodeMapEntry).where(CodeMapEntry.file_id == old_kb_file.id)
                )
                self.session.delete(old_kb_file)
                deleted_count += 1

        self.session.flush()

        # 2. Handle ADDED, MODIFIED, and UNCHANGED files
        for rel_path, abs_path in disk_files.items():
            file_hash = compute_file_sha256(abs_path)
            existing = existing_files.get(rel_path)

            if existing is not None and existing.file_hash == file_hash:
                existing.last_run_id = run.id
                existing.last_commit_sha = commit_sha
                skipped_count += 1
                continue

            ext = abs_path.suffix.lower()
            source_kind = "doc" if ext in DOC_EXTENSIONS else "code"

            if existing is None:
                added_count += 1
                kb_file = KbFile(
                    last_run_id=run.id,
                    file_path=rel_path,
                    file_hash=file_hash,
                    file_ext=ext,
                    source_kind=source_kind,
                    his_module="Core",
                    last_commit_sha=commit_sha,
                )
                self.session.add(kb_file)
                self.session.flush()
            else:
                modified_count += 1
                kb_file = existing
                kb_file.last_run_id = run.id
                kb_file.file_hash = file_hash
                kb_file.last_commit_sha = commit_sha
                kb_file.updated_at = utc_now()
                self.session.execute(delete(KbChunk).where(KbChunk.file_id == kb_file.id))
                self.session.execute(delete(CodeMapEntry).where(CodeMapEntry.file_id == kb_file.id))
                self.session.flush()

            if source_kind == "doc":
                raw_chunks = DocumentChunker.chunk_file(abs_path, rel_path)
                code_maps: list[CodeMapDraft] = []
            else:
                raw_chunks, code_maps = CodeChunker.chunk_code_file(abs_path, rel_path)

            if raw_chunks:
                kb_file.his_module = raw_chunks[0].his_module
            elif code_maps:
                kb_file.his_module = code_maps[0].his_module

            # Persist CodeMapEntry rows + 1-to-1 synchronized KbChunk(chunk_type='code_map')
            chunk_idx = 0
            for cm in code_maps:
                dlp_res = AllBoundaryDLP.sanitize_with_stats(cm.summary_text)
                secrets_redacted += dlp_res.secrets_redacted
                pii_masked += dlp_res.pii_masked

                entry = CodeMapEntry(
                    file_id=kb_file.id,
                    entry_type=cm.entry_type,
                    http_method=cm.http_method,
                    route_path=cm.route_path,
                    controller_or_class=cm.controller_or_class,
                    action_or_symbol=cm.action_or_symbol,
                    his_module=cm.his_module,
                    start_line=cm.start_line,
                    end_line=cm.end_line,
                    summary_text=dlp_res.text,
                )
                self.session.add(entry)
                code_maps_created += 1

                cm_heading = f"{cm.controller_or_class}.{cm.action_or_symbol}"
                cm_Draft = RawChunkDraft(
                    chunk_type="code_map",
                    his_module=cm.his_module,
                    heading_or_symbol=cm_heading,
                    content=dlp_res.text,
                    start_line=cm.start_line,
                    end_line=cm.end_line,
                    metadata={
                        "file": rel_path,
                        "heading": cm_heading,
                        "page": None,
                        "startLine": cm.start_line,
                        "endLine": cm.end_line,
                        "hisModule": cm.his_module,
                        "entryType": cm.entry_type,
                        "httpMethod": cm.http_method,
                        "routePath": cm.route_path,
                    },
                )
                self._insert_sanitized_chunk(
                    kb_file=kb_file,
                    chunk_index=chunk_idx,
                    draft=cm_Draft,
                    already_sanitized=True,
                )
                chunk_idx += 1
                chunks_created += 1

            # Persist Document or Source Code chunks
            for draft in raw_chunks:
                s_red, p_mask = self._insert_sanitized_chunk(
                    kb_file=kb_file,
                    chunk_index=chunk_idx,
                    draft=draft,
                    already_sanitized=False,
                )
                secrets_redacted += s_red
                pii_masked += p_mask
                chunk_idx += 1
                chunks_created += 1

        # Update PostgreSQL TSVECTOR column when running on PostgreSQL dialect
        self.session.flush()
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            self.session.execute(
                text(
                    "UPDATE kb_chunks SET content_tsv = to_tsvector('simple', lexical_tokens) "
                    "WHERE content_tsv IS NULL;"
                )
            )

        run.files_added = added_count
        run.files_modified = modified_count
        run.files_deleted = deleted_count
        run.files_skipped = skipped_count
        run.chunks_created = chunks_created
        run.code_maps_created = code_maps_created
        run.secrets_redacted = secrets_redacted
        run.pii_masked = pii_masked
        run.status = "SUCCESS"
        run.completed_at = utc_now()
        self.session.commit()

        return IngestSummary(
            run_id=run.id,
            branch=run.branch,
            commit_sha=run.commit_sha,
            repo_path=run.repo_path,
            files_added=added_count,
            files_modified=modified_count,
            files_deleted=deleted_count,
            files_skipped=skipped_count,
            chunks_created=chunks_created,
            code_maps_created=code_maps_created,
            secrets_redacted=secrets_redacted,
            pii_masked=pii_masked,
            status=run.status,
        )

    def _insert_sanitized_chunk(
        self,
        kb_file: KbFile,
        chunk_index: int,
        draft: RawChunkDraft,
        already_sanitized: bool = False,
    ) -> tuple[int, int]:
        if already_sanitized:
            clean_text = draft.content
            s_red, p_mask = 0, 0
        else:
            res = AllBoundaryDLP.sanitize_with_stats(draft.content)
            clean_text = res.text
            s_red, p_mask = res.secrets_redacted, res.pii_masked

        clean_heading = AllBoundaryDLP.sanitize_text(draft.heading_or_symbol)
        clean_meta = AllBoundaryDLP.sanitize_obj(draft.metadata)
        combined_for_index = f"{clean_heading} {draft.his_module} {clean_text}"
        lex_tokens = build_lexical_token_string(combined_for_index)
        embedding_vec = DeterministicEmbedding.encode(combined_for_index)

        chunk = KbChunk(
            file_id=kb_file.id,
            chunk_index=chunk_index,
            chunk_type=draft.chunk_type,
            his_module=draft.his_module,
            heading_or_symbol=clean_heading,
            page_number=draft.page_number,
            start_line=draft.start_line,
            end_line=draft.end_line,
            content=clean_text,
            lexical_tokens=lex_tokens,
            embedding=embedding_vec,
            metadata_json=clean_meta,
        )
        self.session.add(chunk)
        return s_red, p_mask

    def get_status(self) -> dict[str, Any]:
        """Return latest ingestion run and current counts of files, chunks, and code maps."""
        latest_run = self.session.scalar(
            select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(1)
        )
        total_files = self.session.scalar(select(func.count()).select_from(KbFile)) or 0
        total_code_maps = self.session.scalar(select(func.count()).select_from(CodeMapEntry)) or 0

        rows = self.session.execute(
            select(KbChunk.chunk_type, func.count()).group_by(KbChunk.chunk_type)
        ).all()
        chunks_by_type = {"doc": 0, "code_map": 0, "source_code": 0}
        for c_type, cnt in rows:
            chunks_by_type[str(c_type)] = int(cnt)

        if latest_run is None:
            return {
                "command": "his-kb status",
                "latest_run": None,
                "files_indexed": total_files,
                "chunks_by_type": chunks_by_type,
                "code_map_entries_count": total_code_maps,
            }

        return {
            "command": "his-kb status",
            "latest_run": {
                "run_id": latest_run.id,
                "branch": latest_run.branch,
                "commit_sha": latest_run.commit_sha,
                "status": latest_run.status,
                "files_indexed": total_files,
                "files_added": latest_run.files_added,
                "files_modified": latest_run.files_modified,
                "files_deleted": latest_run.files_deleted,
                "files_skipped": latest_run.files_skipped,
                "chunks_by_type": chunks_by_type,
                "code_map_entries_count": total_code_maps,
                "dlp_stats": {
                    "secrets_redacted": latest_run.secrets_redacted,
                    "pii_masked": latest_run.pii_masked,
                },
            },
        }
