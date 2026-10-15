"""The LLM client: one chat completion over an OpenAI-compatible endpoint."""

import json
import re
import time
import urllib.error
from typing import NotRequired, Required, TypedDict, cast

import markers
import telemetry
from common import env, request


class _Message(TypedDict):
    content: str | None


class _Choice(TypedDict, total=False):
    message: Required[_Message]
    finish_reason: str | None


class _Usage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int


class _Completion(TypedDict):
    choices: list[_Choice]
    usage: NotRequired[_Usage | None]


class LLMRejected(RuntimeError):
    """The LLM rejected this very prompt; sending it again would fail the same way."""


# Коды отказа именно из-за промпта (длина, контент-фильтр) в OpenAI-теле ошибки:
# {"error": {"code": …, "type": …}}.
_PROMPT_REJECTION_CODES = frozenset(
    {
        "context_length_exceeded",
        "string_above_max_length",
        "content_filter",
        "content_policy_violation",
    }
)
# litellm-прокси кладёт в code HTTP-статус, а класс своего исключения — в начало message
# ("litellm.ContextWindowExceededError: …"); имена классов — его стабильные коды ошибок.
_GATEWAY_REJECTION = re.compile(
    r"\blitellm\.(?:ContextWindowExceededError|ContentPolicyViolationError)\b"
)
# Последний запасной путь — признаки в тексте: content_filter от провайдера `openai` litellm
# в ContentPolicyViolationError не переводит (tests/test_runner.py, LITELLM_PROXY_ERRORS).
_PROMPT_REJECTION = re.compile(
    r"context.length|maximum context|too long|too many tokens|content.filter|content management policy",
    re.IGNORECASE,
)

# Под MAX_FINDINGS находок ревью по абзацу.
MAX_OUTPUT_TOKENS = 4096


def _error_codes(body: str) -> set[str]:
    """`error.code` and `error.type` of an OpenAI-style error body; empty for any other body."""
    try:
        parsed = cast(object, json.loads(body))
    except ValueError:
        return set()
    if not isinstance(parsed, dict):
        return set()
    error = cast(dict[str, object], parsed).get("error")
    if not isinstance(error, dict):
        return set()
    fields = cast(dict[str, object], error)
    return {str(fields[key]) for key in ("code", "type") if fields.get(key)}


def _is_prompt_rejection(code: int, body: str) -> bool:
    if code == 413:
        return True
    if code not in (400, 422):
        return False
    return (
        bool(_error_codes(body) & _PROMPT_REJECTION_CODES)
        or bool(_GATEWAY_REJECTION.search(body))
        or bool(_PROMPT_REJECTION.search(body))
    )


def _log_call(
    prompt: str, started: float, outcome: str, usage: _Usage | None = None
) -> None:
    """One log line per call: cost, how close the prompt is to the context limit, gateway latency."""
    tokens = (
        f"{usage.get('prompt_tokens', '?')} prompt + {usage.get('completion_tokens', '?')} completion tokens"
        if usage
        else "no usage reported"
    )
    print(
        f"LLM call: {time.monotonic() - started:.1f}s, prompt {len(prompt)} chars, {tokens}, {outcome}"
    )


def _record_usage(usage: _Usage | None, choice: _Choice | None) -> None:
    """GenAI semantic-convention attributes of the call on its span."""
    attrs: dict[str, telemetry.Attribute] = {}
    if usage and "prompt_tokens" in usage:
        attrs["gen_ai.usage.input_tokens"] = usage["prompt_tokens"]
    if usage and "completion_tokens" in usage:
        attrs["gen_ai.usage.output_tokens"] = usage["completion_tokens"]
    finish_reason = choice.get("finish_reason") if choice else None
    if finish_reason:
        attrs["gen_ai.response.finish_reasons"] = finish_reason
    telemetry.set_attributes(attrs)


def chat(prompt: str, *, json_object: bool = False) -> str:
    """One completion; `json_object` asks the gateway for a bare JSON object (the prompt must say "JSON")."""
    model = env("LLM_MODEL")
    attrs: dict[str, telemetry.Attribute] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": model,
        "mr_bot.prompt_chars": len(prompt),
    }
    with telemetry.span(f"chat {model}", attrs, client=True):
        return _chat(prompt, json_object=json_object)


def _chat(prompt: str, *, json_object: bool) -> str:
    payload: dict[str, object] = {
        "model": env("LLM_MODEL"),
        "messages": [{"role": "user", "content": prompt}],
        # Один и тот же MR не должен получать разное ревью от запуска к запуску.
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    if json_object:
        payload["response_format"] = {"type": "json_object"}
    started = time.monotonic()
    try:
        completion = cast(
            _Completion,
            request(
                "POST",
                f"{env('LLM_BASE_URL').rstrip('/')}/chat/completions",
                {"Authorization": f"Bearer {env('LLM_API_KEY')}"},
                payload,
            ),
        )
    except urllib.error.HTTPError as error:
        _log_call(prompt, started, f"HTTP {error.code}")
        telemetry.set_attributes({"http.response.status_code": error.code})
        raw = error.read().decode("utf-8", errors="replace")
        # Тело отказа может цитировать промпт, а текст отказа уходит в MR.
        body = markers.strip(raw[:500])
        # Отказ из-за самого промпта повторять бессмысленно. Прочие ошибки не про этот MR:
        # он не помечается, и следующий запуск его повторит.
        if _is_prompt_rejection(error.code, raw):
            raise LLMRejected(
                f"LLM rejected the prompt: HTTP {error.code}: {body}"
            ) from error
        raise RuntimeError(f"LLM request failed: HTTP {error.code}: {body}") from error
    except OSError as error:
        # Таймаут или обрыв: по времени видно, висел ли шлюз до таймаута запроса.
        _log_call(prompt, started, type(error).__name__)
        raise
    choices = completion.get("choices")
    _record_usage(completion.get("usage"), choices[0] if choices else None)
    _log_call(
        prompt,
        started,
        f"finish_reason {choices[0].get('finish_reason')}" if choices else "no choices",
        completion.get("usage"),
    )
    if not choices:
        # Пустой choices шлюз отдаёт на отфильтрованный промпт; без маркера MR гонялся бы снова.
        raise LLMRejected("LLM returned no choices")
    choice = choices[0]
    content = choice["message"]["content"]
    if content is None:
        # Шлюз отдаёт content: null при срабатывании контент-фильтра или отказе модели.
        raise LLMRejected(
            f"LLM returned no content (finish_reason: {choice.get('finish_reason')})"
        )
    if choice.get("finish_reason") == "length":
        # При temperature 0 повтор обрежется так же.
        raise LLMRejected(f"LLM answer exceeded {MAX_OUTPUT_TOKENS} tokens")
    # Модель может повторить маркер из диффа и сдвинуть состояние джобы.
    return markers.strip(content).strip()
