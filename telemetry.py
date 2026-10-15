"""Spans of the bots' work, exported as OTLP/HTTP JSON when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.

One trace per run of `mr-review` / `mr-description`, sent when the job exits:

- `mr-review` / `mr-description`: the run; error status if any MR failed, and
  `mr_bot.deferred`, the MRs left for the next run, when the run limit was hit.
- `mr-review !<iid>`: one MR, with `gitlab.mr.iid` and what became of it: `mr_bot.outcome`,
  `mr_bot.status`, and when they apply `mr_bot.reason`, `mr_bot.head`, `mr_bot.inline`,
  `mr_bot.in_summary`, `mr_bot.latency_s` and `mr_bot.attempt` (fields of
  `gitlab_api.Result`).
- `chat <model>`: one LLM call (span kind client), with the GenAI semantic-convention
  attributes `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, plus
  `mr_bot.prompt_chars` and `http.response.status_code` on a gateway error. Prompts and
  answers are never exported: they carry MR code.

The resource carries `ci.pipeline.id` and `ci.job.id`, so a trace leads back to its job
log. The export is best effort: it gives up after `EXPORT_TIMEOUT` seconds, and a collector
that is down only prints a line to the job log, never turns the job red.
"""

import atexit
import os
import secrets
import time
import urllib.parse
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import NotRequired, TypedDict

from common import request


class _AnyValue(TypedDict, total=False):
    stringValue: str
    intValue: str
    doubleValue: float
    boolValue: bool


class _KeyValue(TypedDict):
    key: str
    value: _AnyValue


class _Span(TypedDict):
    traceId: str
    spanId: str
    parentSpanId: NotRequired[str]
    name: str
    kind: int
    startTimeUnixNano: str
    endTimeUnixNano: NotRequired[str]
    attributes: list[_KeyValue]
    status: NotRequired[dict[str, int | str]]


Attribute = str | int | float | bool

EXPORT_TIMEOUT = 10

_SPAN_KIND_INTERNAL = 1
_SPAN_KIND_CLIENT = 3
_STATUS_ERROR = 2

_spans: list[_Span] = []
_stack: list[_Span] = []
_trace_id = secrets.token_hex(16)


def _value(value: Attribute) -> _AnyValue:
    # bool проверяется раньше int: bool — подкласс int.
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": value}


def _attributes(attrs: Mapping[str, Attribute]) -> list[_KeyValue]:
    return [{"key": key, "value": _value(value)} for key, value in attrs.items()]


def _endpoint() -> str | None:
    traces = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if traces:
        return traces
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    return f"{base.rstrip('/')}/v1/traces" if base else None


def _headers() -> dict[str, str]:
    """`OTEL_EXPORTER_OTLP_HEADERS` as the spec defines it: `k1=v1,k2=v2`."""
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {
        urllib.parse.unquote(k).strip(): urllib.parse.unquote(v).strip()
        for k, v in pairs
    }


def set_attributes(attrs: Mapping[str, Attribute]) -> None:
    """Add attributes to the innermost open span, if any."""
    if _stack:
        _stack[-1]["attributes"].extend(_attributes(attrs))


def fail(message: str) -> None:
    """Mark the innermost open span as failed for an error its caller handled itself."""
    if _stack:
        _stack[-1]["status"] = {"code": _STATUS_ERROR, "message": message}


@contextmanager
def span(
    name: str, attrs: Mapping[str, Attribute] | None = None, *, client: bool = False
) -> Generator[None]:
    """Record one span; nested spans become its children. `client` marks an outgoing call."""
    current: _Span = {
        "traceId": _trace_id,
        "spanId": secrets.token_hex(8),
        "name": name,
        "kind": _SPAN_KIND_CLIENT if client else _SPAN_KIND_INTERNAL,
        "startTimeUnixNano": str(time.time_ns()),
        "attributes": _attributes(attrs or {}),
    }
    if _stack:
        current["parentSpanId"] = _stack[-1]["spanId"]
    _stack.append(current)
    try:
        yield
    except BaseException as error:
        current["status"] = {"code": _STATUS_ERROR, "message": type(error).__name__}
        raise
    finally:
        _ = _stack.pop()
        current["endTimeUnixNano"] = str(time.time_ns())
        _spans.append(current)


def flush() -> None:
    """Send the recorded spans; an unreachable collector never fails the job."""
    endpoint = _endpoint()
    if not endpoint or not _spans:
        return
    service = os.environ.get("OTEL_SERVICE_NAME") or os.environ.get("CI_JOB_NAME") or "mr-bot"
    resource: dict[str, Attribute] = {"service.name": service}
    for key, var in (("ci.pipeline.id", "CI_PIPELINE_ID"), ("ci.job.id", "CI_JOB_ID")):
        if os.environ.get(var):
            resource[key] = os.environ[var]
    body = {
        "resourceSpans": [
            {
                "resource": {"attributes": _attributes(resource)},
                "scopeSpans": [{"scope": {"name": "mr-bot"}, "spans": list(_spans)}],
            }
        ]
    }
    try:
        # Экспорт идёт на выходе джобы: зависший коллектор не должен держать её 5 минут.
        _ = request("POST", endpoint, _headers(), body, timeout=EXPORT_TIMEOUT)
    except (OSError, ValueError) as error:
        # Телеметрия вспомогательная: ревью уже записано, красить джобу из-за коллектора незачем.
        print(f"OTLP export to {endpoint} failed: {error}")
        return
    _spans.clear()


_ = atexit.register(flush)
