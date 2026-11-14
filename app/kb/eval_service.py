"""Evaluation service computing Recall@K on the 10-question benchmark dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.kb.search_service import HybridSearchService


class EvalService:
    """Runs retrieval evaluation over the 10 sample questions and reports Recall@K."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.search_service = HybridSearchService(session)

    def evaluate(
        self,
        dataset_path: str | Path = "./sample_data/eval_questions.json",
        k: int = 5,
    ) -> dict[str, Any]:
        path = Path(dataset_path).resolve()
        questions: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))

        hits = 0
        details: list[dict[str, Any]] = []

        for q in questions:
            qid = q["question_id"]
            role = q.get("role", "Dev")
            query = q["query"]
            expected_file = q["expected_file"]
            expected_kw = q.get("expected_keyword", "").lower()

            res = self.search_service.search(query=query, role=role, top_k=k)
            matched_rank: int | None = None

            for rank_idx, item in enumerate(res["results"], start=1):
                cit_file = item["citation"].get("file", "")
                content_low = item["content"].lower()
                if cit_file == expected_file and (not expected_kw or expected_kw in content_low):
                    matched_rank = rank_idx
                    break
                if cit_file == expected_file and matched_rank is None:
                    matched_rank = rank_idx

            is_hit = matched_rank is not None and matched_rank <= k
            if is_hit:
                hits += 1

            details.append(
                {
                    "question_id": qid,
                    "role": role,
                    "query": query,
                    "expected_file": expected_file,
                    "hit": is_hit,
                    "matched_rank": matched_rank,
                }
            )

        total = len(questions)
        recall_at_k = round(hits / max(total, 1), 4)
        return {
            "command": "his-kb eval",
            "total_questions": total,
            "k": k,
            "hits_at_k": hits,
            "recall_at_5": recall_at_k,
            "target_threshold": 0.8,
            "passed": recall_at_k >= 0.8,
            "details": details,
        }
