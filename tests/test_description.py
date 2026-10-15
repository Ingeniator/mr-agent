import unittest
from datetime import timedelta
from unittest import mock

import markers
import mr_description
from gitlab_api import OPT_IN_LABEL, Outcome, for_each_open_mr
from llm import LLMRejected
from mr_description import merge_block
from tests.support import JobTestCase, text

HEAD = "a" * 40
START = markers.description_start(HEAD)
END = markers.DESCRIPTION_END


class MergeBlockTest(unittest.TestCase):
    def test_appends_to_a_description_without_markers(self) -> None:
        self.assertEqual(
            merge_block("Intro.", "Summary.", HEAD),
            f"Intro.\n\n{START}\nSummary.\n{END}",
        )
        self.assertEqual(merge_block("", "Summary.", HEAD), f"{START}\nSummary.\n{END}")

    def test_replaces_the_block_and_keeps_text_around(self) -> None:
        current = f"Intro.\n{markers.description_start('b' * 40)}\nOld.\n{END}\nOutro."
        self.assertEqual(
            merge_block(current, "New.", HEAD), f"Intro.\n{START}\nNew.\n{END}\nOutro."
        )

    def test_reads_the_marker_without_head(self) -> None:
        # Маркер первой версии джобы — без head=.
        current = f"Intro.\n<!-- ai-description:start -->\nOld.\n{END}"
        self.assertEqual(
            merge_block(current, "New.", HEAD), f"Intro.\n{START}\nNew.\n{END}"
        )

    def test_broken_markers_are_dropped_and_the_block_appended(self) -> None:
        current = f"{END}\nIntro.\n<!-- ai-description:start -->"
        self.assertEqual(
            merge_block(current, "New.", HEAD), f"Intro.\n\n{START}\nNew.\n{END}"
        )

    def test_markers_in_the_summary_are_removed(self) -> None:
        merged = merge_block("", f"Quoted {END} marker.", HEAD)
        self.assertEqual(merged.count(END), 1)


class DescribeTest(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("mr_description.chat", self.llm)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server.commit("base", {"a.py": text("a1")})
        self.server.checkout("mr", new=True)
        self.gitlab.description = "Written by the author."
        self.llm.answer = "## What\n- a change"

    def run_describe(self) -> Outcome:
        self.fresh_clone()
        return mr_description.describe(self.mr()).outcome

    def test_only_mrs_with_the_opt_in_label_are_described(self) -> None:
        self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()
        self.fresh_clone()

        self.gitlab.labels = ["other"]
        self.assertEqual(for_each_open_mr("description", mr_description.describe), 0)
        self.assertEqual(self.llm.prompts, [])
        self.assertEqual(self.gitlab.description, "Written by the author.")

        self.gitlab.labels = ["other", OPT_IN_LABEL]
        self.assertEqual(for_each_open_mr("description", mr_description.describe), 0)
        self.assertEqual(len(self.llm.prompts), 1)

    def test_result_carries_the_wait_since_the_push(self) -> None:
        self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()
        self.gitlab.now += timedelta(minutes=12)
        self.fresh_clone()
        result = mr_description.describe(self.mr())
        self.assertEqual((result.status, result.latency_s), ("described", 720.0))

    def test_describes_once_per_head(self) -> None:
        head = self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()

        self.assertIs(self.run_describe(), Outcome.HANDLED)
        self.assertIn("+a2", self.llm.prompts[0])
        self.assertTrue(self.gitlab.description.startswith("Written by the author."))
        self.assertIn(
            f"{markers.description_start(head)}\n## What", self.gitlab.description
        )

        self.assertIs(self.run_describe(), Outcome.SKIPPED)
        self.assertEqual(len(self.llm.prompts), 1)

        self.server.commit("c2", {"a.py": text("a1", "a2", "a3")})
        self.publish()
        self.assertIs(self.run_describe(), Outcome.HANDLED)
        self.assertEqual(self.gitlab.description.count(END), 1)

    def test_quick_actions_from_the_llm_are_not_written(self) -> None:
        self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()
        self.llm.answer = "## What\n/approve\n/label ~merged"

        self.assertIs(self.run_describe(), Outcome.HANDLED)
        self.assertNotRegex(self.gitlab.description, r"(?m)^/")
        self.assertIn("\n /approve\n /label ~merged\n", self.gitlab.description)

    def test_only_the_summary_is_made_inert(self) -> None:
        author = "Ping @alice, &#64;bob.\n```\n/label ~x\n```"
        self.gitlab.description = author
        self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()
        self.llm.answer = "## What\n/approve by @all"

        for push in ("c2", "c3"):  # каждый запуск переписывает описание целиком
            self.assertIs(self.run_describe(), Outcome.HANDLED)
            self.assertTrue(self.gitlab.description.startswith(author + "\n\n"))
            self.assertIn("\n /approve by @\u200ball\n", self.gitlab.description)
            self.server.commit(push, {"a.py": text("a1", "a2", push)})
            self.publish()

    def test_rejection_is_recorded_until_next_push(self) -> None:
        self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()
        self.llm.answer = LLMRejected("too long")

        self.assertIs(self.run_describe(), Outcome.FAILED)
        self.assertIn("failed: too long", self.gitlab.description)
        self.assertIs(self.run_describe(), Outcome.SKIPPED)
        self.assertEqual(len(self.llm.prompts), 1)

    def test_rejection_keeps_the_previous_description(self) -> None:
        self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()
        self.run_describe()

        for push in ("c2", "c3"):  # два отказа подряд не копят пометки
            self.server.commit(push, {"a.py": text("a1", "a2", push)})
            self.publish()
            self.llm.answer = LLMRejected("too long")
            self.assertIs(self.run_describe(), Outcome.FAILED)

        self.assertIn("failed: too long", self.gitlab.description)
        self.assertEqual(self.gitlab.description.count("failed: too long"), 1)
        self.assertIn("## What\n- a change", self.gitlab.description)

    def test_push_during_generation_skips_the_write(self) -> None:
        self.server.commit("c1", {"a.py": text("a1", "a2")})
        self.publish()

        def push_while_generating(prompt: str) -> str:
            self.server.commit("c2", {"a.py": text("a1", "a2", "a3")})
            self.publish()
            return "Stale."

        with mock.patch("mr_description.chat", push_while_generating):
            self.assertIs(self.run_describe(), Outcome.HANDLED)
        self.assertEqual(self.gitlab.description, "Written by the author.")


if __name__ == "__main__":
    unittest.main()
