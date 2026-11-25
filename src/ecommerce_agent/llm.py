"""Phase 3D: course model gateway adapter (Portkey, OpenAI-compatible chat completions).

The adapter only transports messages and reports usage. It never sees credentials in traces and
never decides anything; budgets, tools, validation, and authorization live elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.portkey.ai/v1"
DEFAULT_MODEL = "@30875-fall26-openai/gpt-5-nano"


class ModelError(Exception):
    """A failed gateway request. `uncertain` means the provider may have processed and billed it."""

    def __init__(self, message, *, uncertain: bool, retryable: bool):
        super().__init__(message)
        self.uncertain, self.retryable = uncertain, retryable


@dataclass(frozen=True)
class ModelReply:
    message: dict  # assistant message in chat-completions form, ready to append
    finish_reason: str
    input_tokens: int
    output_tokens: int  # includes reasoning tokens, as billed
    reasoning_tokens: int
    model: str


def load_env(path: Path = Path(".env")):
    """Load KEY=VALUE lines without overriding the real environment. Values are never logged."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class GatewayClient:
    def __init__(self, *, model=None, base_url=None, reasoning_effort="low", timeout=30.0):
        from openai import OpenAI

        load_env()
        key = os.environ.get("PORTKEY_API_KEY")
        if not key:
            raise RuntimeError("PORTKEY_API_KEY is not set (see .env)")
        self.model = model or os.environ.get("PORTKEY_MODEL", DEFAULT_MODEL)
        self.reasoning_effort = reasoning_effort
        self._client = OpenAI(
            base_url=base_url or os.environ.get("PORTKEY_BASE_URL", DEFAULT_BASE_URL),
            api_key="unused",  # Portkey authenticates with its own header.
            default_headers={"x-portkey-api-key": key},
            timeout=timeout,
            max_retries=0,  # Retries are budgeted by the agent loop, not hidden here.
        )

    def complete(self, messages, tools, *, tool_choice="required", max_output_tokens):
        import openai

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                reasoning_effort=self.reasoning_effort,
                max_completion_tokens=max_output_tokens,
            )
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            raise ModelError(type(exc).__name__, uncertain=True, retryable=True) from exc
        except openai.APIStatusError as exc:
            retryable = exc.status_code in (408, 409, 429) or exc.status_code >= 500
            raise ModelError(
                f"{type(exc).__name__} {exc.status_code}", uncertain=False, retryable=retryable
            ) from exc
        choice = response.choices[0]
        message = {"role": "assistant", "content": choice.message.content or ""}
        if choice.message.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in choice.message.tool_calls
            ]
        usage = response.usage
        details = getattr(usage, "completion_tokens_details", None)
        return ModelReply(
            message=message,
            finish_reason=choice.finish_reason or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else max_output_tokens,
            reasoning_tokens=(getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
            model=response.model or self.model,
        )
