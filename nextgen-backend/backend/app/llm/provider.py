"""
LLM Provider — abstract interface with OpenAI implementation.
Abstracted so we can swap providers (Anthropic, local, etc.) later.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger("nextgen.llm")


@dataclass
class LLMResponse:
    """Standardized LLM response across providers."""
    text: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model: str
    provider: str
    cost_cents: int  # Estimated cost in cents (ZAR)


class LLMProvider(ABC):
    """Abstract LLM provider interface."""

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> LLMResponse:
        """Send a completion request and return standardized response."""
        ...

    @abstractmethod
    async def complete_with_history(
        self,
        system_prompt: str,
        messages: list[dict],  # [{"role": "user"/"assistant", "content": "..."}]
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> LLMResponse:
        """Send a completion request with conversation history."""
        ...


class OpenAIProvider(LLMProvider):
    """OpenAI GPT provider implementation."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self.api_key)
        return self._client

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> LLMResponse:
        return await self.complete_with_history(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def complete_with_history(
        self,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> LLMResponse:
        client = self._get_client()

        full_messages = [{"role": "system", "content": system_prompt}] + messages

        response = await client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        choice = response.choices[0]
        usage = response.usage

        # Estimate cost (gpt-4o-mini pricing approx)
        cost_cents = self._estimate_cost(usage.prompt_tokens, usage.completion_tokens)

        return LLMResponse(
            text=choice.message.content or "",
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            model=self.model,
            provider="openai",
            cost_cents=cost_cents,
        )

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> int:
        """
        Estimate cost in ZAR cents.
        gpt-4o-mini: ~$0.15/1M input, ~$0.60/1M output
        At ~R18/USD, that's R2.70/1M input, R10.80/1M output
        """
        input_cost = (input_tokens / 1_000_000) * 270   # R2.70 in cents
        output_cost = (output_tokens / 1_000_000) * 1080  # R10.80 in cents
        return round(input_cost + output_cost)


# ── Factory ──────────────────────────────────────────────────────────────────

_provider_instance: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    """Get the configured LLM provider singleton."""
    global _provider_instance
    if _provider_instance is None:
        from backend.app.core.config import get_settings
        settings = get_settings()
        _provider_instance = OpenAIProvider(
            api_key=settings.OPENAI_API_KEY,
            model=settings.LLM_DEFAULT_MODEL,
        )
    return _provider_instance
