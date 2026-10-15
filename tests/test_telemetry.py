"""OTLP/HTTP JSON export of the bots' spans (`telemetry`)."""

# Атрибуты TestCase задаёт setUp, а не __init__.
# pyright: reportUninitializedInstanceVariable=false

import os
import unittest
import urllib.error
from typing import cast
from unittest import mock

import telemetry
from gitlab_api import MergeRequest, Outcome, Result, for_each_open_mr
from llm import chat
from tests.support import JobTestCase

Sent = list[tuple[str, dict[str, str], object, float]]


def _attrs(span: dict[str, object]) -> dict[str, object]:
    items = cast(list[dict[str, dict[str, object]]], span.get("attributes", []))
    return {
        str(item["key"]): next(iter(item["value"].values())) for item in items
    }


class TelemetryCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        telemetry._spans.clear()  # pyright: ignore[reportPrivateUsage]
        self.addCleanup(telemetry._spans.clear)  # pyright: ignore[reportPrivateUsage]
        environ = mock.patch.dict(
            os.environ,
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.test:4318/",
                "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20secret, x-tenant = eval",
                "OTEL_SERVICE_NAME": "mr-review",
                "CI_JOB_ID": "42",
            },
        )
        environ.start()
        self.addCleanup(environ.stop)
        self.sent: Sent = []

        def request(
            method: str,
            url: str,
            headers: dict[str, str],
            body: object = None,
            *,
            timeout: float = 300,
        ) -> object:
            self.assertEqual(method, "POST")
            self.sent.append((url, headers, body, timeout))
            return {}

        patcher = mock.patch("telemetry.request", request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def exported(self) -> list[dict[str, object]]:
        telemetry.flush()
        self.assertEqual(len(self.sent), 1)
        body = cast(dict[str, list[dict[str, object]]], self.sent[0][2])
        scope = cast(list[dict[str, object]], body["resourceSpans"][0]["scopeSpans"])
        return cast(list[dict[str, object]], scope[0]["spans"])


class ExportTest(TelemetryCase):
    def test_spans_nest_and_go_to_the_traces_endpoint(self) -> None:
        with telemetry.span("run", {"job": "review"}):
            with telemetry.span("call", {"n": 3, "ok": True}, client=True):
                telemetry.set_attributes({"tokens": 10, "latency_s": 1.5})
        inner, outer = self.exported()
        url, headers, body, timeout = self.sent[0]
        self.assertEqual(url, "https://collector.test:4318/v1/traces")
        self.assertEqual(
            headers, {"Authorization": "Bearer secret", "x-tenant": "eval"}
        )
        self.assertEqual(timeout, telemetry.EXPORT_TIMEOUT)
        resource = cast(
            dict[str, list[dict[str, dict[str, object]]]], body
        )["resourceSpans"][0]["resource"]
        self.assertEqual(
            _attrs(cast(dict[str, object], resource)),
            {"service.name": "mr-review", "ci.job.id": "42"},
        )
        self.assertEqual(inner["parentSpanId"], outer["spanId"])
        self.assertEqual(inner["traceId"], outer["traceId"])
        self.assertNotIn("parentSpanId", outer)
        self.assertEqual((inner["kind"], outer["kind"]), (3, 1))
        self.assertEqual(_attrs(inner), {"n": "3", "ok": True, "tokens": "10", "latency_s": 1.5})
        self.assertLessEqual(
            int(cast(str, inner["startTimeUnixNano"])),
            int(cast(str, inner["endTimeUnixNano"])),
        )

    def test_an_exception_marks_the_span_failed(self) -> None:
        with self.assertRaises(KeyError):
            with telemetry.span("run"):
                raise KeyError("x")
        (span,) = self.exported()
        self.assertEqual(span["status"], {"code": 2, "message": "KeyError"})

    def test_nothing_is_sent_without_an_endpoint(self) -> None:
        del os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]
        with telemetry.span("run"):
            pass
        telemetry.flush()
        self.assertEqual(self.sent, [])

    def test_traces_endpoint_is_used_as_is(self) -> None:
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "https://other.test/t"
        self.addCleanup(os.environ.pop, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        with telemetry.span("run"):
            pass
        _ = self.exported()
        self.assertEqual(self.sent[0][0], "https://other.test/t")

    def test_an_unreachable_collector_does_not_raise(self) -> None:
        def down(*args: object, **kwargs: object) -> object:
            raise urllib.error.URLError("refused")

        with telemetry.span("run"):
            pass
        with mock.patch("telemetry.request", down):
            telemetry.flush()


class InstrumentationTest(JobTestCase, TelemetryCase):
    def setUp(self) -> None:
        # JobTestCase.setUp не зовёт super(): без явного вызова TelemetryCase не настроится.
        JobTestCase.setUp(self)
        TelemetryCase.setUp(self)

    def test_llm_calls_nest_under_the_mr_and_the_run(self) -> None:
        os.environ.update(LLM_BASE_URL="https://llm.test/v1", LLM_API_KEY="k")
        completion = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }

        def handle(mr: MergeRequest) -> Result:
            with mock.patch("llm.request", lambda *a, **k: completion):
                _ = chat("prompt")
            if mr.iid == 2:
                raise RuntimeError("boom")
            return Result(Outcome.HANDLED, "reviewed")

        with mock.patch(
            "gitlab_api.open_merge_requests", lambda label=None: [1, 2]
        ), mock.patch("gitlab_api._error_note", lambda mr, job: None), mock.patch(
            "gitlab_api._record_error", lambda mr, job, error: None
        ):
            self.assertEqual(for_each_open_mr("review", handle), 1)

        spans = {cast(str, span["name"]): span for span in self.exported()}
        self.assertEqual(
            sorted(spans), ["chat gpt-4o", "mr-review", "mr-review !1", "mr-review !2"]
        )
        run, first, second = spans["mr-review"], spans["mr-review !1"], spans["mr-review !2"]
        self.assertEqual(first["parentSpanId"], run["spanId"])
        self.assertEqual(_attrs(first)["mr_bot.outcome"], "handled")
        self.assertNotIn("status", first)
        self.assertEqual(_attrs(second)["mr_bot.outcome"], "failed")
        self.assertEqual(cast(dict[str, object], second["status"])["code"], 2)
        self.assertEqual(cast(dict[str, object], run["status"])["code"], 2)
        # Последний chat — внутри !2: имена совпадают, в словаре остался он.
        llm_span = spans["chat gpt-4o"]
        self.assertEqual(llm_span["parentSpanId"], second["spanId"])
        self.assertEqual(
            _attrs(llm_span),
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "gpt-4o",
                "mr_bot.prompt_chars": "6",
                "gen_ai.usage.input_tokens": "12",
                "gen_ai.usage.output_tokens": "3",
                "gen_ai.response.finish_reasons": "stop",
            },
        )
