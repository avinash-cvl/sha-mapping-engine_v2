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

import logging
import random
import time

import common.config as C
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# sha_observability_sdk lives outside this repo and is not pip-installable
# (no pyproject of its own), so it has to be found on the filesystem. The
# original single append assumed it sat directly beside the repo's parent;
# it actually lives one level deeper, in the accelerator checkout, so an
# ordinary `uv run` failed with ModuleNotFoundError unless the caller
# happened to set PYTHONPATH by hand. Try the known locations, and let
# SHA_OBSERVABILITY_SDK_PATH override for a checkout somewhere else.
_repo_root = Path(__file__).resolve().parent.parent
_sdk_candidates = [
    os.environ.get("SHA_OBSERVABILITY_SDK_PATH"),
    _repo_root.parent,                                  # d:/Himalaya
    _repo_root.parent / "SKUHarmonizationAccelerator",  # where it actually is
]
for _candidate in _sdk_candidates:
    if _candidate and (Path(_candidate) / "sha_observability_sdk").is_dir():
        if str(_candidate) not in sys.path:
            sys.path.append(str(_candidate))
        break

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


def _retryable_exception_types() -> tuple[type[BaseException], ...]:
    """Errors worth retrying: transient conditions where an identical retry
    can plausibly get a different result (capacity freed up, network blip
    resolved). Built lazily from whichever provider package is actually
    installed rather than imported unconditionally at module load -- this
    file is shared by all three providers (azure/openai/anthropic), and only
    one's SDK is guaranteed to be present in a given environment.

    Deliberately NOT included: openai.BadRequestError (400) and its
    Anthropic/other-provider equivalents. A content-filter rejection,
    a malformed-request error, or any other 4xx is the API correctly
    rejecting THIS prompt -- retrying sends the identical prompt again and
    gets the identical rejection. See common.config's LLM_RETRY_* comment
    for the incident that made this distinction matter: one such rejection,
    unretried and unhandled above this function, killed a 907-SKU run.
    """
    types: list[type[BaseException]] = []
    try:
        import openai
        types += [
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,  # 5xx -- Azure's own fault, not the prompt's
        ]
    except ImportError:
        pass
    try:
        import anthropic
        types += [
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ]
    except ImportError:
        pass
    return tuple(types)


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

    Retries a transient failure (rate limit, timeout, connection reset,
    Azure 5xx) up to LLM_RETRY_ATTEMPTS times with exponential backoff and
    jitter -- the jitter matters when many worker threads hit a rate limit
    at once, so they do not all retry on the exact same schedule and
    re-collide. A non-retryable error (content filter, bad request) is
    re-raised immediately on the first attempt: retrying it cannot help,
    and every second spent doing so is a second this run's operator has no
    visibility into.
    """
    system_prompt = next((content for role, content in messages if role == "system"), None)
    request_text = "\n".join(content for role, content in messages if role != "system")
    retryable = _retryable_exception_types()

    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            response = model.invoke(messages)
            break
        except retryable as exc:
            if attempt >= C.LLM_RETRY_ATTEMPTS:
                logger.error(
                    "LLM call failed after %d attempt(s), giving up | "
                    "call_type=%s | error=%s",
                    attempt, call_type, exc,
                )
                raise
            delay = C.LLM_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)
            logger.warning(
                "LLM call failed, retrying in %.1fs (attempt %d/%d) | "
                "call_type=%s | error=%s",
                delay, attempt, C.LLM_RETRY_ATTEMPTS, call_type, exc,
            )
            time.sleep(delay)
        except Exception as exc:
            # Not in the retryable set -- a content-filter rejection or any
            # other permanent error. Fail this one call now rather than
            # retrying something that cannot change its own answer.
            logger.error(
                "LLM call failed with a non-retryable error | "
                "call_type=%s | error=%s",
                call_type, exc,
            )
            raise

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
