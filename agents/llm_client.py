"""LangChain chat-model factory. Every agent below calls get_chat_model()
instead of constructing a provider client directly -- swapping Claude <->
GPT is an env var change (LLM_PROVIDER), never a code change.

invoke_and_audit() is the single choke point every agent calls instead of
hand-rolling model.invoke() + timing + audit_entry construction -- it also
traces the call through observability_sdk (init'd once in flow.py), so
adding observability to a new agent means calling this helper, not wiring
a decorator into every agent file.
"""
from __future__ import annotations

import time

import common.config as C
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from sha_observability_sdk import observe as sh_observe, capture_generation_response
from langchain_core.language_models.chat_models import BaseChatModel


def get_chat_model() -> BaseChatModel:
    # Temperature is passed only when LLM_TEMPERATURE is explicitly set, so
    # the default is byte-identical to the previous behaviour (no temperature
    # sent at all). Classification gains nothing from sampling variety and a
    # non-zero temperature makes runs irreproducible, which undermines the
    # audit trail -- but reasoning-family models reject any non-default
    # temperature outright, and the provider/deployment here is env-driven.
    # So this is opt-in (LLM_TEMPERATURE=0) rather than hardcoded.
    kwargs = {} if C.LLM_TEMPERATURE is None else {"temperature": C.LLM_TEMPERATURE}

    provider = C.LLM_PROVIDER
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=C.LLM_MODEL or "claude-sonnet-5", **kwargs)
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=C.LLM_MODEL or "gpt-5", **kwargs)
    if provider == "azure":
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(
            azure_endpoint=C.AZURE_OPENAI_ENDPOINT,
            api_key=C.AZURE_OPENAI_API_KEY,
            api_version=C.AZURE_OPENAI_API_VERSION,
            azure_deployment=C.LLM_MODEL or C.AZURE_LLM_DEPLOYMENT,
            **kwargs,
        )
    raise ValueError(f"unknown LLM_PROVIDER: {provider!r} (expected 'anthropic', 'openai', or 'azure')")


@sh_observe(as_type="generation", model=C.LLM_MODEL or "unknown")
def invoke_and_audit(
    model: BaseChatModel,
    messages: list[tuple[str, str]],
    call_type: str,
) -> tuple[str, dict[str, object]]:
    """Calls model.invoke(messages), timing it and tracing it through the
    observability SDK. Returns (response_text, audit_entry) -- audit_entry
    is shaped for dbo.llm_audit, same contract every agent already returned
    when it built this dict by hand. success stays True here (the call
    completed); callers flip it to False themselves if response parsing
    fails, same as before.
    """
    system_prompt = next((content for role, content in messages if role == "system"), None)
    request_text = "\n".join(content for role, content in messages if role != "system")
    started = time.monotonic()
    response = model.invoke(messages)
    capture_generation_response(response)
    latency_ms = int((time.monotonic() - started) * 1000)

    response_text = str(response.content)
    audit_entry: dict[str, object] = {
        "call_type": call_type,
        "system_prompt": system_prompt,
        "request_messages": request_text,
        "response_text": response_text,
        "latency_ms": latency_ms,
        "success": True,
    }
    return response_text, audit_entry
