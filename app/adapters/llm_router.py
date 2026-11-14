"""LLM Router with Primary -> Fallback Failover, Outbound DLP, and Monthly Token & Cost Quotas."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.interfaces import LLMProviderProtocol, LLMResponse
from app.config import Settings, get_settings
from app.db.models import LlmUsageLedger, User, utc_now
from app.security.dlp import AllBoundaryDLP

logger = logging.getLogger(__name__)

# Cost per 1,000 tokens in USD for deterministic accounting
COST_PER_1K_TOKENS_USD = Decimal("0.002000")


class LLMProviderError(RuntimeError):
    """Raised when an LLM provider experiences a transient or service failure."""


class QuotaExceededError(PermissionError):
    """Raised when a user or role exceeds their monthly token or USD cost ceiling."""


def _compute_tokens_and_cost(prompt: str, completion: str) -> tuple[int, int, int, Decimal]:
    p_tok = max(10, len(prompt.split()) * 2)
    c_tok = max(15, len(completion.split()) * 2)
    tot = p_tok + c_tok
    cost = (Decimal(tot) * COST_PER_1K_TOKENS_USD / Decimal(1000)).quantize(Decimal("0.000001"))
    return p_tok, c_tok, tot, cost


class PrimaryMockLLMProvider(LLMProviderProtocol):
    """Primary Mock LLM Provider that can simulate failures for failover verification."""

    provider_name = "primary-mock"

    def __init__(self, simulate_failure: bool = False) -> None:
        self.simulate_failure = simulate_failure

    def generate(self, prompt: str, context: dict[str, Any] | None = None) -> LLMResponse:
        if self.simulate_failure or get_settings().llm_simulate_primary_failure:
            raise LLMProviderError("Primary LLM provider timeout / HTTP 503 simulated failure.")

        ctx = context or {}
        action = ctx.get("action", "general")
        story_key = ctx.get("story_key", "HIS-STORY")
        completion = (
            f"[PrimaryMockLLM:{action}:{story_key}] Phân tích và tổng hợp tự động dựa trên "
            f"ngữ cảnh HIS-KB đã qua kiểm duyệt DLP."
        )
        p_tok, c_tok, tot, cost = _compute_tokens_and_cost(prompt, completion)
        return LLMResponse(
            provider_name=self.provider_name,
            content=completion,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=tot,
            cost_usd=cost,
            used_fallback=False,
        )


class FallbackMockLLMProvider(LLMProviderProtocol):
    """Resilient Fallback Mock LLM Provider invoked automatically when Primary fails."""

    provider_name = "fallback-mock"

    def generate(self, prompt: str, context: dict[str, Any] | None = None) -> LLMResponse:
        ctx = context or {}
        action = ctx.get("action", "general")
        story_key = ctx.get("story_key", "HIS-STORY")
        completion = (
            f"[FallbackMockLLM:{action}:{story_key}] Đã chuyển dự phòng tự động (Failover) "
            f"và hoàn tất xử lý dựa trên HIS-KB."
        )
        p_tok, c_tok, tot, cost = _compute_tokens_and_cost(prompt, completion)
        return LLMResponse(
            provider_name=self.provider_name,
            content=completion,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=tot,
            cost_usd=cost,
            used_fallback=True,
        )


class LLMRouter:
    """Coordinates outbound DLP, monthly Token/Cost USD quota enforcement, and LLM failover."""

    def __init__(
        self,
        session: Session,
        primary_provider: LLMProviderProtocol | None = None,
        fallback_provider: LLMProviderProtocol | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.primary = primary_provider or PrimaryMockLLMProvider(
            simulate_failure=self.settings.llm_simulate_primary_failure
        )
        self.fallback = fallback_provider or FallbackMockLLMProvider()

    @staticmethod
    def current_usage_month() -> str:
        return utc_now().strftime("%Y-%m")

    def _lock_user_and_check_quota(
        self,
        user_id: str,
        user_role: str,
        estimated_tokens: int,
        estimated_cost_usd: Decimal,
    ) -> str:
        """Lock user row (SELECT FOR UPDATE on Postgres) and verify monthly token & USD quotas."""
        stmt = select(User).where(User.user_id == user_id)
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        self.session.scalar(stmt)

        month_str = self.current_usage_month()

        # 1. Aggregate current month usage for user_id
        u_tokens, u_cost = self.session.execute(
            select(
                func.coalesce(func.sum(LlmUsageLedger.total_tokens), 0),
                func.coalesce(func.sum(LlmUsageLedger.cost_usd), Decimal("0.000000")),
            ).where(
                LlmUsageLedger.user_id == user_id,
                LlmUsageLedger.usage_month == month_str,
            )
        ).one()

        user_tokens_used = int(u_tokens or 0)
        user_cost_used = Decimal(str(u_cost or "0"))

        max_u_tokens = self.settings.monthly_user_token_quota
        max_u_cost = Decimal(str(self.settings.monthly_user_cost_usd_quota))

        if user_tokens_used + estimated_tokens > max_u_tokens:
            raise QuotaExceededError(
                f"Monthly token quota exceeded for user '{user_id}': "
                f"used={user_tokens_used}, requested={estimated_tokens}, ceiling={max_u_tokens}"
            )
        if user_cost_used + estimated_cost_usd > max_u_cost:
            raise QuotaExceededError(
                f"Monthly USD cost quota exceeded for user '{user_id}': "
                f"used=${user_cost_used}, requested=${estimated_cost_usd}, ceiling=${max_u_cost}"
            )

        # 2. Aggregate current month usage for user_role
        r_tokens, r_cost = self.session.execute(
            select(
                func.coalesce(func.sum(LlmUsageLedger.total_tokens), 0),
                func.coalesce(func.sum(LlmUsageLedger.cost_usd), Decimal("0.000000")),
            ).where(
                LlmUsageLedger.user_role == user_role,
                LlmUsageLedger.usage_month == month_str,
            )
        ).one()

        role_tokens_used = int(r_tokens or 0)
        role_cost_used = Decimal(str(r_cost or "0"))

        max_r_tokens = self.settings.monthly_role_token_quota
        max_r_cost = Decimal(str(self.settings.monthly_role_cost_usd_quota))

        if role_tokens_used + estimated_tokens > max_r_tokens:
            raise QuotaExceededError(
                f"Monthly token quota exceeded for role '{user_role}': "
                f"used={role_tokens_used}, requested={estimated_tokens}, ceiling={max_r_tokens}"
            )
        if role_cost_used + estimated_cost_usd > max_r_cost:
            raise QuotaExceededError(
                f"Monthly USD cost quota exceeded for role '{user_role}': "
                f"used=${role_cost_used}, requested=${estimated_cost_usd}, ceiling=${max_r_cost}"
            )

        return month_str

    def generate_with_governance(
        self,
        user_id: str,
        user_role: str,
        story_key: str,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Sanitize outbound prompt, enforce monthly quotas, call Primary -> Fallback."""
        sanitized_prompt = AllBoundaryDLP.sanitize_text(prompt)
        sanitized_ctx = AllBoundaryDLP.sanitize_obj(context or {})

        est_p, est_c, est_tot, est_cost = _compute_tokens_and_cost(
            sanitized_prompt, "estimated completion"
        )
        month_str = self._lock_user_and_check_quota(
            user_id=user_id,
            user_role=user_role,
            estimated_tokens=est_tot,
            estimated_cost_usd=est_cost,
        )

        try:
            response = self.primary.generate(sanitized_prompt, sanitized_ctx)
        except Exception as exc:
            logger.warning(
                "Primary LLM provider '%s' failed (%s). Failing over to fallback provider '%s'.",
                self.primary.provider_name,
                exc,
                self.fallback.provider_name,
            )
            response = self.fallback.generate(sanitized_prompt, sanitized_ctx)
            response.used_fallback = True

        response.content = AllBoundaryDLP.sanitize_text(response.content)

        ledger = LlmUsageLedger(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            usage_month=month_str,
            provider_name=response.provider_name,
            used_fallback=response.used_fallback,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            cost_usd=response.cost_usd,
        )
        self.session.add(ledger)
        self.session.flush()
        return response
