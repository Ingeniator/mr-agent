"""The loop over open MRs (`gitlab_api.for_each_open_mr`) and the LLM client (`llm.chat`)."""

# Атрибуты TestCase задаёт setUp, а не __init__.
# pyright: reportUninitializedInstanceVariable=false

import contextlib
import io
import os
import re
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import cast
from unittest import mock

import llm
import markers
from gitlab_api import Job, MergeRequest, Outcome, Result, for_each_open_mr, inert
from llm import LLMRejected, chat
from tests.support import IID, JobTestCase, recorded_spans, text


class ForEachOpenMrTest(JobTestCase):
    def run_loop(
        self, outcomes: dict[int, Outcome | Exception]
    ) -> tuple[int, list[int]]:
        seen: list[int] = []

        def handle(mr: MergeRequest) -> Result:
            seen.append(mr.iid)
            outcome = outcomes.get(mr.iid, Outcome.HANDLED)
            if isinstance(outcome, Exception):
                raise outcome
            return Result(
                outcome, "skipped" if outcome is Outcome.SKIPPED else "reviewed"
            )

        # Fake GitLab знает только !7: прочие MR не читаются ни паузой, ни записью сбоя.
        with mock.patch(
            "gitlab_api.open_merge_requests", lambda label=None: list(outcomes)
        ), mock.patch("gitlab_api._error_note", lambda mr, job: None), mock.patch(
            "gitlab_api._record_error", lambda mr, job, error: None
        ):
            status = for_each_open_mr("review", handle, limit=2)
        return status, seen

    def test_skipped_mrs_do_not_count_toward_the_limit(self) -> None:
        status, seen = self.run_loop(
            {
                1: Outcome.SKIPPED,
                2: Outcome.HANDLED,
                3: Outcome.SKIPPED,
                4: Outcome.HANDLED,
                5: Outcome.HANDLED,
            }
        )
        self.assertEqual((status, seen), (0, [1, 2, 3, 4]))

    def test_a_failure_is_reported_and_the_rest_handled(self) -> None:
        status, seen = self.run_loop(
            {1: RuntimeError("boom"), 2: Outcome.SKIPPED, 3: Outcome.FAILED}
        )
        self.assertEqual((status, seen), (1, [1, 2, 3]))

    def test_every_mr_gets_a_span_and_the_rest_are_counted(self) -> None:
        _ = self.run_loop(
            {
                1: Outcome.SKIPPED,
                2: RuntimeError("boom"),
                3: Outcome.HANDLED,
                4: Outcome.HANDLED,
                5: Outcome.HANDLED,
            }
        )
        spans = recorded_spans()
        self.assertEqual(
            [
                (name, a["mr_bot.status"], a["mr_bot.outcome"])
                for name, a in spans
                if name != "mr-review"
            ],
            [
                ("mr-review !1", "skipped", "skipped"),
                ("mr-review !2", "error", "failed"),
                ("mr-review !3", "reviewed", "handled"),
            ],
        )
        self.assertEqual(spans[1][1]["mr_bot.reason"], "RuntimeError: boom")
        self.assertEqual(spans[-1], ("mr-review", {"mr_bot.job": "review", "mr_bot.deferred": "2"}))

    def test_mr_iid_selects_one_mr(self) -> None:
        os.environ["MR_IID"] = "42"
        status, seen = self.run_loop({1: Outcome.HANDLED, 2: Outcome.HANDLED})
        self.assertEqual((status, seen), (0, [42]))


START = datetime(2026, 1, 1, tzinfo=UTC)


class ErrorBackoffTest(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server.commit("base", {"a.py": text("a1")})
        self.server.checkout("mr", new=True)
        self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()
        self.calls = 0
        self.error: Exception | None = RuntimeError("fetch refused")

    def handle(self, mr: MergeRequest) -> Result:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return Result(Outcome.HANDLED, "reviewed")

    def run_at(self, minutes: int, job: Job = "review") -> int:
        self.gitlab.now = START + timedelta(minutes=minutes)
        return for_each_open_mr(job, self.handle)

    def error_notes(self) -> list[str]:
        return [
            n["body"] for n in self.gitlab.notes if markers.JOB_ERROR.match(n["body"])
        ]

    def test_a_recurring_failure_is_retried_with_a_growing_delay(self) -> None:
        # Сбои в 0, 10, 30 минут: паузы 10, 20, 40 минут, между ними MR пропускается.
        ran = [(m, self.run_at(m), self.calls) for m in (0, 5, 10, 25, 30, 65, 70)]
        self.assertEqual(
            ran,
            [
                (0, 1, 1),
                (5, 0, 1),
                (10, 1, 2),
                (25, 0, 2),
                (30, 1, 3),
                (65, 0, 3),
                (70, 1, 4),
            ],
        )
        head = self.gitlab.refs and self.gitlab.refs["head_sha"]
        notes = self.error_notes()
        self.assertEqual(len(notes), 1)  # одна заметка, правится на месте
        self.assertTrue(notes[0].startswith(markers.job_error("review", str(head), 4)))
        self.assertNotIn("fetch refused", notes[0])  # текст ошибки — только в лог

    def test_the_span_counts_attempts(self) -> None:
        _ = self.run_at(0)
        _ = self.run_at(5)
        mrs = [a for name, a in recorded_spans() if name == f"mr-review !{IID}"]
        self.assertEqual(
            [(a["mr_bot.status"], a.get("mr_bot.attempt")) for a in mrs],
            [("error", "1"), ("backing_off", "1")],
        )
        self.assertEqual(
            mrs[1]["mr_bot.head"], self.gitlab.refs and self.gitlab.refs["head_sha"]
        )

    def test_the_delay_is_capped(self) -> None:
        for minutes in range(0, 3 * 24 * 60, 10):
            _ = self.run_at(minutes)
        # Попытки в 0, 10, 30, 70, 150, 310 минут, с 630-й — раз в 6 часов: ещё 11 за 72 часа.
        self.assertEqual(self.calls, 17)

    def test_a_push_retries_at_once(self) -> None:
        _ = self.run_at(0)
        self.server.commit("c2", {"a.py": text("a1", "a2", "a3")})
        self.publish()
        self.assertEqual(self.run_at(1), 1)
        self.assertEqual(self.calls, 2)
        self.assertIn("attempts=1 ", self.error_notes()[0])

    def test_success_after_the_delay(self) -> None:
        _ = self.run_at(0)
        self.error = None
        self.assertEqual((self.run_at(5), self.calls), (0, 1))
        self.assertEqual((self.run_at(10), self.calls), (0, 2))

    def test_success_closes_the_error_note(self) -> None:
        _ = self.run_at(0)
        _ = self.run_at(5)  # пауза: заметка не тронута
        self.assertEqual(len(self.error_notes()), 1)
        self.error = None
        self.assertEqual(self.run_at(10), 0)
        self.assertEqual(self.error_notes(), [])
        self.assertIn("a later run succeeded", self.gitlab.notes[-1]["body"])
        # Новый сбой после успеха — с первой попытки и в новой заметке.
        self.error = RuntimeError("again")
        self.assertEqual(self.run_at(11), 1)
        self.assertIn("attempts=1 ", self.error_notes()[0])
        self.assertEqual(len(self.gitlab.notes), 2)

    def test_a_failed_outcome_keeps_the_error_note(self) -> None:
        _ = self.run_at(0)
        self.error = None
        with mock.patch.object(
            self, "handle", lambda mr: Result(Outcome.FAILED, "rejected")
        ):
            self.assertEqual(self.run_at(10), 1)
        self.assertEqual(len(self.error_notes()), 1)
        self.assertNotIn("succeeded", self.gitlab.notes[-1]["body"])

    def test_mr_iid_success_closes_the_error_note(self) -> None:
        _ = self.run_at(0)
        os.environ["MR_IID"] = str(IID)
        self.error = None
        self.assertEqual(self.run_at(1), 0)
        self.assertEqual(self.error_notes(), [])

    def test_jobs_back_off_separately(self) -> None:
        _ = self.run_at(0, "review")
        self.assertEqual(self.run_at(1, "description"), 1)
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(self.error_notes()), 2)

    def test_mr_iid_ignores_the_delay(self) -> None:
        _ = self.run_at(0)
        os.environ["MR_IID"] = str(IID)
        self.assertEqual(self.run_at(1), 1)
        self.assertEqual(self.calls, 2)

    def test_marker_from_another_user_is_ignored(self) -> None:
        head = str(self.gitlab.refs and self.gitlab.refs["head_sha"])
        _ = self.gitlab.add_note(markers.job_error("review", head, 30), author=2)
        self.error = None
        self.assertEqual((self.run_at(0), self.calls), (0, 1))


def http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "error", Message(), io.BytesIO(body.encode()))


# Тела ошибок litellm-прокси 1.82.6 (ProxyException.to_dict поверх exception_type) на
# настоящие ответы OpenAI/Azure. Перегенерировать при обновлении litellm на шлюзе: тест
# держит формат, по которому `llm` узнаёт отказ из-за промпта.
LITELLM_PROXY_ERRORS: dict[str, tuple[int, str, bool]] = {
    "context, openai": (
        400,
        '{"error": {"message": "litellm.ContextWindowExceededError: litellm.BadRequestError: ContextWindowExceededError: OpenAIException - This model\'s maximum context length is 128000 tokens. However, your messages resulted in 200000 tokens.", "type": null, "param": null, "code": "400"}}',
        True,
    ),
    "context, azure": (
        400,
        '{"error": {"message": "litellm.ContextWindowExceededError: litellm.BadRequestError: AzureException ContextWindowExceededError - This model\'s maximum context length is 128000 tokens. However, your messages resulted in 200000 tokens.", "type": null, "param": null, "code": "400"}}',
        True,
    ),
    "content policy, openai": (
        400,
        '{"error": {"message": "litellm.BadRequestError: litellm.ContentPolicyViolationError: ContentPolicyViolationError: OpenAIException - Your request was rejected as a result of our safety system.", "type": null, "param": null, "code": "400"}}',
        True,
    ),
    "content filter, azure": (
        400,
        '{"error": {"message": "litellm.BadRequestError: litellm.ContentPolicyViolationError: The response was filtered due to the prompt triggering Azure OpenAI\'s content management policy.", "type": "invalid_request_error", "param": null, "code": "400"}}',
        True,
    ),
    # content_filter провайдера `openai` litellm не классифицирует: узнаётся только по тексту.
    "content filter, openai": (
        400,
        '{"error": {"message": "litellm.BadRequestError: OpenAIException - The response was filtered due to the prompt triggering Azure OpenAI\'s content management policy.", "type": null, "param": null, "code": "400"}}',
        True,
    ),
    "unknown param": (
        400,
        '{"error": {"message": "litellm.BadRequestError: OpenAIException - Unrecognized request argument supplied: foo", "type": "invalid_request_error", "param": null, "code": "400"}}',
        False,
    ),
    "unknown model": (
        404,
        '{"error": {"message": "litellm.NotFoundError: OpenAIException - The model `gpt-9` does not exist", "type": null, "param": null, "code": "404"}}',
        False,
    ),
}


class ChatTest(unittest.TestCase):
    def setUp(self) -> None:
        environ = mock.patch.dict(
            os.environ,
            {
                "LLM_BASE_URL": "https://llm.test/v1",
                "LLM_API_KEY": "k",
                "LLM_MODEL": "m",
            },
        )
        environ.start()
        self.addCleanup(environ.stop)

    def chat_with(self, response: object) -> str:
        def request(*args: object) -> object:
            if isinstance(response, Exception):
                raise response
            return response

        with mock.patch.object(llm, "request", request):
            return chat("prompt")

    def test_returns_stripped_content(self) -> None:
        self.assertEqual(
            self.chat_with({"choices": [{"message": {"content": " ok \n"}}]}), "ok"
        )

    def test_request_is_bounded_and_deterministic(self) -> None:
        sent: list[object] = []

        def request(*args: object) -> object:
            sent.append(args[3])
            return {"choices": [{"message": {"content": "{}"}}]}

        with mock.patch.object(llm, "request", request):
            _ = chat("prompt")
            _ = chat("prompt", json_object=True)
        plain, json_mode = cast(list[dict[str, object]], sent)
        self.assertEqual(plain["temperature"], 0)
        self.assertEqual(plain["max_tokens"], llm.MAX_OUTPUT_TOKENS)
        self.assertNotIn("response_format", plain)
        self.assertEqual(json_mode["response_format"], {"type": "json_object"})

    def test_markers_echoed_by_the_llm_are_removed(self) -> None:
        marker = markers.reviewed("a" * 40)
        glued = marker[:10] + marker + marker[10:]
        self.assertEqual(
            self.chat_with({"choices": [{"message": {"content": f"x {glued} y"}}]}),
            "x  y",
        )
        with self.assertRaises(LLMRejected) as raised:
            self.chat_with(http_error(413, f"prompt: {marker}"))
        self.assertNotIn(marker, str(raised.exception))

    def test_each_call_logs_usage_latency_and_prompt_size(self) -> None:
        for response, expected in (
            (
                {
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                },
                "prompt 6 chars, 12 prompt + 3 completion tokens, finish_reason stop",
            ),
            (
                {"choices": [{"message": {"content": "ok"}}]},
                "prompt 6 chars, no usage reported, finish_reason None",
            ),
            (
                http_error(413, "too large"),
                "prompt 6 chars, no usage reported, HTTP 413",
            ),
            (TimeoutError("timed out"), "no usage reported, TimeoutError"),
        ):
            out = io.StringIO()
            with self.subTest(expected=expected), contextlib.redirect_stdout(out):
                with contextlib.suppress(RuntimeError, OSError):
                    _ = self.chat_with(response)
                self.assertIn(expected, out.getvalue())
                self.assertRegex(out.getvalue(), r"^LLM call: \d+\.\ds, ")

    def test_prompt_rejections(self) -> None:
        for response in (
            http_error(413, "payload too large"),
            http_error(400, "This model's maximum context length is 128000 tokens"),
            http_error(422, "blocked by content_filter"),
            http_error(
                400,
                '{"error": {"message": "Request rejected.", "code": "context_length_exceeded"}}',
            ),
            http_error(
                400, '{"error": {"message": "Flagged.", "code": "content_filter"}}'
            ),
            {"choices": []},
            {
                "choices": [
                    {"message": {"content": None}, "finish_reason": "content_filter"}
                ]
            },
            {
                "choices": [
                    {"message": {"content": '{"summ'}, "finish_reason": "length"}
                ]
            },
        ):
            with self.subTest(response=response), self.assertRaises(LLMRejected):
                self.chat_with(response)

    def test_litellm_proxy_errors(self) -> None:
        for name, (code, body, rejected) in LITELLM_PROXY_ERRORS.items():
            with self.subTest(name), self.assertRaises(RuntimeError) as raised:
                self.chat_with(http_error(code, body))
            self.assertEqual(isinstance(raised.exception, LLMRejected), rejected)

    def test_litellm_rejections_do_not_depend_on_wording(self) -> None:
        # Классифицированные litellm отказы узнаются по имени класса, а не по тексту провайдера.
        never = re.compile(r"(?!)")
        classified = [
            (code, body)
            for code, body, rejected in LITELLM_PROXY_ERRORS.values()
            if rejected and "litellm.Co" in body
        ]
        self.assertTrue(classified)
        with mock.patch.object(llm, "_PROMPT_REJECTION", never):
            for code, body in classified:
                with self.subTest(body=body), self.assertRaises(LLMRejected):
                    self.chat_with(http_error(code, body))

    def test_other_errors_are_not_rejections(self) -> None:
        for response in (
            http_error(400, "unknown model"),
            http_error(
                400,
                '{"error": {"message": "Unknown model.", "type": "invalid_request_error"}}',
            ),
            # Код отказа на 5xx — сбой шлюза, а не этого промпта.
            http_error(503, '{"error": {"code": "context_length_exceeded"}}'),
            http_error(401, "bad key"),
            http_error(429, "slow down"),
            http_error(503, "unavailable"),
        ):
            with self.subTest(response=response), self.assertRaises(
                RuntimeError
            ) as raised:
                self.chat_with(response)
            self.assertNotIsInstance(raised.exception, LLMRejected)


class InertTest(unittest.TestCase):
    def test_mentions_do_not_notify(self) -> None:
        self.assertEqual(
            inert("@all, (@group/sub) &#64;here &#x40;dev &commat;x"),
            "@\u200ball, (@\u200bgroup/sub) @\u200bhere @\u200bdev @\u200bx",
        )
        self.assertEqual(inert("mail a@b.com, @ alone"), "mail a@b.com, @ alone")


if __name__ == "__main__":
    unittest.main()
