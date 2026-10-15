"""The `mr-bot-health` job: unreviewed labelled MRs and the bot token's expiry."""

import io
import urllib.error
from contextlib import redirect_stdout
from datetime import timedelta
from email.message import Message
from unittest import mock

import mr_bot_health
import mr_review
from llm import LLMRejected
from mr_bot_health import STALE_AFTER, TOKEN_WARN_DAYS
from tests.support import JobTestCase, text


class HealthTest(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        for patcher in (
            mock.patch("mr_review.chat", self.llm),
            mock.patch("mr_bot_health._now", lambda: self.gitlab.now),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server.commit("base", {"a.py": text("a")})
        self.server.checkout("mr", new=True)
        self.server.commit("c1", {"a.py": text("b")})
        _ = self.publish()

    def check(self) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            status = mr_bot_health.main()
        return status, out.getvalue()

    def review(self) -> None:
        self.fresh_clone()
        _ = mr_review.review(self.mr())

    def test_a_reviewed_head_is_healthy(self) -> None:
        self.review()
        self.gitlab.now += STALE_AFTER * 2
        self.assertEqual(self.check()[0], 0)

    def test_an_unreviewed_head_is_fine_for_a_while(self) -> None:
        self.gitlab.now += STALE_AFTER - timedelta(minutes=1)
        self.assertEqual(self.check()[0], 0)

    def test_an_unreviewed_head_goes_stale(self) -> None:
        self.gitlab.now += STALE_AFTER + timedelta(minutes=1)
        status, out = self.check()
        self.assertEqual(status, 1)
        self.assertIn("unreviewed for over 4 h: !7", out)

    def test_a_push_after_the_review_is_waited_for(self) -> None:
        self.review()
        self.server.commit("c2", {"a.py": text("c")})
        _ = self.publish()
        self.assertEqual(self.check()[0], 0)
        self.gitlab.now += STALE_AFTER + timedelta(minutes=1)
        self.assertEqual(self.check()[0], 1)

    def test_an_llm_rejection_counts_as_handled(self) -> None:
        self.llm.answer = LLMRejected("too long")
        self.review()
        self.gitlab.now += STALE_AFTER * 2
        self.assertEqual(self.check()[0], 0)

    def test_without_labelled_mrs_there_is_nothing_to_check(self) -> None:
        self.gitlab.labels = []
        self.gitlab.now += STALE_AFTER * 2
        self.assertEqual(self.check()[0], 0)

    def test_a_token_close_to_expiry_fails(self) -> None:
        self.review()
        for days, status in ((TOKEN_WARN_DAYS + 1, 0), (TOKEN_WARN_DAYS, 1)):
            with self.subTest(days=days):
                self.gitlab.token_expires = (
                    self.gitlab.now.date() + timedelta(days=days)
                ).isoformat()
                self.assertEqual(self.check()[0], status)

    def test_an_unknown_token_expiry_is_not_a_failure(self) -> None:
        self.review()
        not_found = urllib.error.HTTPError("url", 404, "Not Found", Message(), None)
        with mock.patch("mr_bot_health.token_expires_on", side_effect=not_found):
            self.assertEqual(self.check()[0], 0)

    def test_an_expired_token_fails_the_check(self) -> None:
        unauthorized = urllib.error.HTTPError(
            "url", 401, "Unauthorized", Message(), None
        )
        with mock.patch("mr_bot_health.token_expires_on", side_effect=unauthorized):
            with self.assertRaises(urllib.error.HTTPError):
                _ = self.check()
