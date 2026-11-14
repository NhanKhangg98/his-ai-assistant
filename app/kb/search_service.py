"""Hybrid Search Service (Vector + Lexical RRF k=60) with Strict RBAC Pre-Filtering & Citations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import KbChunk, KbFile, User
from app.kb.embeddings import DeterministicEmbedding, extract_normalized_tokens
from app.security.dlp import AllBoundaryDLP

RRF_K = 60
VALID_ROLES: frozenset[str] = frozenset({"BA", "QC", "Dev"})


@dataclass(slots=True)
class SearchResultItem:
    """Single search hit with RRF score, DLP-sanitized content, and source citation."""

    chunk_id: str
    chunk_type: str
    score: float
    content: str
    citation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunkId": self.chunk_id,
            "chunkType": self.chunk_type,
            "score": round(self.score, 6),
            "content": self.content,
            "citation": self.citation,
        }


def resolve_allowed_chunk_types(role: str) -> tuple[str, ...]:
    """Return allowed chunk_type values for RBAC pre-filtering before Top-K."""
    if role in ("BA", "QC"):
        return ("doc", "code_map")
    if role == "Dev":
        return ("doc", "code_map", "source_code")
    raise ValueError(f"Unsupported role for RBAC search: {role!r}")


class HybridSearchService:
    """Executes RBAC pre-filtered Hybrid Search (Vector Cosine + Lexical FTS) fused via RRF."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve_user_role(self, user_id: str | None, explicit_role: str | None) -> str:
        """Resolve and validate role from users table or explicit role parameter."""
        if user_id:
            user = self.session.scalar(select(User).where(User.user_id == user_id))
            if user is not None:
                if explicit_role and explicit_role != user.role:
                    raise PermissionError(
                        f"User '{user_id}' has role '{user.role}', "
                        f"cannot impersonate '{explicit_role}'."
                    )
                return user.role
        if explicit_role in VALID_ROLES:
            return explicit_role
        raise ValueError(
            f"Invalid or unknown user/role: user_id={user_id!r}, role={explicit_role!r}"
        )

    def search(
        self,
        query: str,
        role: str,
        top_k: int = 5,
        his_module: str | None = None,
    ) -> dict[str, Any]:
        """Run Hybrid Search with strict RBAC pre-filtering before ranking and Top-K slicing."""
        clean_query = AllBoundaryDLP.sanitize_text(query.strip())
        allowed_types = resolve_allowed_chunk_types(role)
        top_k = max(1, min(top_k, 20))

        # 1. RBAC Pre-Filter at SQL level BEFORE any vector/lexical ranking or LIMIT
        stmt = (
            select(KbChunk, KbFile.file_path)
            .join(KbFile, KbChunk.file_id == KbFile.id)
            .where(KbChunk.chunk_type.in_(allowed_types))
        )
        if his_module:
            stmt = stmt.where(KbChunk.his_module == his_module)

        candidates = self.session.execute(stmt).all()
        if not candidates:
            return {
                "query": clean_query,
                "role": role,
                "totalResults": 0,
                "results": [],
            }

        query_vec = DeterministicEmbedding.encode(clean_query)
        query_tokens = set(extract_normalized_tokens(clean_query))

        # 2. Compute Vector Cosine Similarity and Lexical Match Score on RBAC-filtered candidates
        vec_scored: list[tuple[str, float]] = []
        lex_scored: list[tuple[str, float]] = []
        chunk_map: dict[str, tuple[KbChunk, str]] = {}

        for chunk, file_path in candidates:
            chunk_map[chunk.id] = (chunk, file_path)
            v_sim = DeterministicEmbedding.cosine_similarity(query_vec, chunk.embedding)
            vec_scored.append((chunk.id, v_sim))

            doc_tokens = set((chunk.lexical_tokens or "").split())
            if query_tokens and doc_tokens:
                overlap = len(query_tokens & doc_tokens)
                exact_sub = (
                    1.5
                    if any(qt in (chunk.heading_or_symbol or "").lower() for qt in query_tokens)
                    else 0.0
                )
                l_score = (overlap / max(len(query_tokens), 1)) + exact_sub
            else:
                l_score = 0.0
            lex_scored.append((chunk.id, l_score))

        vec_scored.sort(key=lambda x: x[1], reverse=True)
        lex_scored.sort(key=lambda x: x[1], reverse=True)

        vec_rank = {cid: rank for rank, (cid, _) in enumerate(vec_scored, start=1)}
        lex_rank = {cid: rank for rank, (cid, _) in enumerate(lex_scored, start=1)}
        vec_sim_map = dict(vec_scored)
        lex_sim_map = dict(lex_scored)

        # 3. Reciprocal Rank Fusion (RRF, k=60)
        fused: list[tuple[str, float]] = []
        for cid in chunk_map:
            r_v = vec_rank[cid]
            r_l = lex_rank[cid]
            rrf_score = (1.0 / (RRF_K + r_v)) + (1.0 / (RRF_K + r_l))
            # Tie-breaker using normalized similarity
            tie_break = 0.001 * (vec_sim_map[cid] + 0.1 * lex_sim_map[cid])
            fused.append((cid, rrf_score + tie_break))

        fused.sort(key=lambda x: x[1], reverse=True)
        top_Matches = fused[:top_k]

        results: list[dict[str, Any]] = []
        for cid, score in top_Matches:
            chunk, file_path = chunk_map[cid]
            meta = chunk.metadata_json or {}
            citation = {
                "file": meta.get("file", file_path),
                "heading": meta.get("heading", chunk.heading_or_symbol),
                "page": chunk.page_number,
                "startLine": chunk.start_line,
                "endLine": chunk.end_line,
                "hisModule": chunk.his_module,
            }
            item = SearchResultItem(
                chunk_id=chunk.id,
                chunk_type=chunk.chunk_type,
                score=score,
                content=AllBoundaryDLP.sanitize_text(chunk.content),
                citation=AllBoundaryDLP.sanitize_obj(citation),
            )
            results.append(item.to_dict())

        return {
            "query": clean_query,
            "role": role,
            "totalResults": len(results),
            "results": results,
        }
