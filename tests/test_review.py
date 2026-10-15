import json
import os
from email.message import Message
import unittest
import urllib.error
from datetime import timedelta
from unittest import mock

import markers
import mr_review
from diff import FileDiff
from gitlab_api import Outcome
from llm import LLMRejected
from mr_review import parse_review
from repo import git, has_commit
from tests.support import JobTestCase, text

BASE = text(*(f"a{n}" for n in range(1, 11)))  # a1 … a10


def answer(*findings: tuple[str, int, str]) -> str:
    return json.dumps(
        {
            "summary": "Summary.",
            "findings": [
                {"path": path, "line": line, "severity": "major", "body": body}
                for path, line, body in findings
            ],
        }
    )


class ReviewTest(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("mr_review.chat", self.llm)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server.commit("base", {"a.py": BASE})
        self.server.checkout("mr", new=True)

    def run_review(self) -> Outcome:
        self.fresh_clone()
        return mr_review.review(self.mr()).outcome

    def summary_note(self) -> str:
        return self.gitlab.notes[-1]["body"]

    def test_result_counts_findings_and_the_wait_since_the_oldest_unreviewed_push(
        self,
    ) -> None:
        self.server.commit("c1", {"a.py": BASE.replace("a5", "A5")})
        self.publish()
        self.gitlab.now += timedelta(minutes=30)
        self.llm.answer = answer(("a.py", 5, "inline"), ("a.py", 100, "outside"))
        self.fresh_clone()
        first = mr_review.review(self.mr())
        self.assertEqual(
            (first.status, first.inline, first.in_summary, first.latency_s),
            ("reviewed", 1, 1, 1800.0),
        )
        # Два пуша до следующего ревью: ждали с первого из них.
        self.server.commit("c2", {"a.py": BASE.replace("a5", "A5").replace("a7", "A7")})
        self.publish()
        self.gitlab.now += timedelta(minutes=10)
        self.server.commit("c3", {"a.py": BASE.replace("a5", "A5").replace("a7", "B7")})
        self.publish()
        self.gitlab.now += timedelta(minutes=5)
        self.llm.answer = answer()
        self.fresh_clone()
        second = mr_review.review(self.mr())
        self.assertEqual((second.status, second.latency_s), ("reviewed", 900.0))
        self.fresh_clone()
        again = mr_review.review(self.mr())
        self.assertEqual(
            (again.status, again.reason),
            ("skipped", f"{second.head and second.head[:8]} is already reviewed."),
        )

    def test_first_review_posts_inline_and_summary(self) -> None:
        self.server.commit("c1", {"a.py": BASE.replace("a5", "A5") + "a11\n"})
        refs = self.publish()
        self.llm.answer = answer(
            ("a.py", 5, "on an added line"),
            ("a.py", 4, "on a context line"),
            ("a.py", 100, "outside the diff"),
        )

        self.assertIs(self.run_review(), Outcome.HANDLED)

        self.assertIn("    5 + A5", self.llm.prompts[0])
        self.assertEqual(self.llm.json_object, [True])
        self.assertNotIn("follow-up review", self.llm.prompts[0])
        positions = [position for _, position in self.gitlab.discussions]
        self.assertEqual(positions[0].get("new_line"), 5)
        self.assertNotIn("old_line", positions[0])
        # У неизменённой строки GitLab требует оба номера.
        self.assertEqual(
            (positions[1].get("new_line"), positions[1].get("old_line")), (4, 4)
        )
        self.assertEqual(positions[0].get("head_sha"), refs["head_sha"])
        note = self.summary_note()
        self.assertIn(markers.reviewed(refs["head_sha"]), note)
        self.assertIn("`a.py:100`", note)
        self.assertIn("2 inline comment(s)", note)

    def test_reviewed_head_is_skipped(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "a11\n"})
        self.publish()
        self.run_review()

        self.assertIs(self.run_review(), Outcome.SKIPPED)
        self.assertEqual(len(self.llm.prompts), 1)

    def test_marker_quoted_by_another_user_is_ignored(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "a11\n"})
        refs = self.publish()
        self.gitlab.add_note(f"> {markers.reviewed(refs['head_sha'])}", author=2)

        self.assertIs(self.run_review(), Outcome.HANDLED)

    def test_marker_echoed_in_a_bot_note_does_not_move_the_base(self) -> None:
        base = self.server.git("rev-parse", "main").strip()
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        first_head = self.publish()["head_sha"]
        self.run_review()
        self.server.commit("c2", {"a.py": BASE + "x1\nx2\n"})
        head = self.publish()["head_sha"]
        # Inline-заметка бота, где находка процитировала маркеры из диффа.
        self.gitlab.add_note(
            f"🤖 **major**: {markers.reviewed(base)} {markers.review_failed(head)}"
        )

        self.assertIs(self.run_review(), Outcome.HANDLED)
        self.assertIn("follow-up review", self.llm.prompts[-1])
        self.assertIn(f"`{first_head[:8]}..{head[:8]}`", self.summary_note())

    def test_follow_up_after_target_merge_reviews_only_new_changes(self) -> None:
        self.server.git("checkout", "-q", "main")
        self.server.commit(
            "base files",
            {
                "b.py": text("b1"),
                "gone.py": text("keep"),
                "c.py": text("c1", "check()"),
            },
        )
        self.server.git("checkout", "-q", "mr")
        self.server.git("merge", "-q", "--no-edit", "main")
        self.server.commit(
            "c1", {"a.py": BASE.replace("a2", "A2"), "b.py": text("b1", "mr line")}
        )
        self.publish()
        self.run_review()

        # Целевая ветка удаляет строку и добавляет файл, MR её вмерживает.
        self.server.git("checkout", "-q", "main")
        self.server.commit("target", {"c.py": text("c1"), "t.py": text("target")})
        self.server.git("checkout", "-q", "mr")
        self.server.git("merge", "-q", "--no-edit", "main")
        # Новый коммит MR только удаляет: строку базы и целый файл.
        self.server.commit(
            "c2",
            {"a.py": BASE.replace("a2", "A2").replace("a9\n", ""), "gone.py": None},
        )
        self.publish()

        self.assertIs(self.run_review(), Outcome.HANDLED)

        prompt = self.llm.prompts[-1]
        self.assertIn("follow-up review", prompt)
        self.assertIn("      ~ a9", prompt)
        self.assertIn("      ~ keep", prompt)
        self.assertIn("    2 + A2", prompt)  # уже отревьюено — контекст, без «*»
        self.assertNotIn("check()", prompt)  # удаление целевой ветки
        self.assertNotIn("t.py", prompt)
        self.assertNotIn("mr line", prompt)  # файл без новых изменений
        # Только коммиты после прошлого ревью, без merge-коммитов.
        commits = prompt.split("Commits under review:\n", 1)[1].split("\n\nDiff:", 1)[0]
        self.assertRegex(commits, r"^[0-9a-f]+ c2\n$")
        # Клон остаётся shallow: история MR не скачивается.
        self.assertEqual(git("rev-parse", "--is-shallow-repository").strip(), "true")
        self.assertFalse(has_commit(self.server.git("rev-parse", "mr~1")))
        # base_at и waiting_since — один обход versions на ревью.
        self.assertEqual(self.gitlab.version_reads, 2)

    def test_force_push_stays_incremental(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        first_head = self.publish()["head_sha"]
        self.run_review()

        self.server.git("checkout", "-q", "main")
        self.server.commit("target", {"t.py": text("target")})
        self.server.git("rebase", "-q", "main", "mr")
        self.server.commit("c2", {"a.py": BASE + "x1\nx2\n"})
        refs = self.publish()
        self.llm.answer = answer(("a.py", 11, "old line"), ("a.py", 12, "new line"))

        self.assertIs(self.run_review(), Outcome.HANDLED)

        prompt = self.llm.prompts[-1]
        self.assertIn("   12 * x2", prompt)
        self.assertIn("   11 + x1", prompt)
        self.assertIn(
            f"`{first_head[:8]}..{refs['head_sha'][:8]}`", self.summary_note()
        )
        # Inline — только на новую строку; находка по уже отревьюенной — в итоговую заметку.
        self.assertEqual([p.get("new_line") for _, p in self.gitlab.discussions], [12])
        self.assertIn("`a.py:11`", self.summary_note())

    def test_force_push_with_old_head_gone_reviews_the_whole_mr(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.publish()
        self.run_review()

        self.server.git("checkout", "-q", "main")
        self.server.commit("target", {"t.py": text("target")})
        self.server.git("rebase", "-q", "main", "mr")
        self.server.commit("c2", {"a.py": BASE + "x1\nx2\n"})
        self.publish()
        # Сервер больше не отдаёт старый HEAD: на него не ссылается ничего, и он вычищен.
        self.server.git("reflog", "expire", "--expire=now", "--all")
        self.server.git("gc", "--quiet", "--prune=now")

        self.assertIs(self.run_review(), Outcome.HANDLED)

        prompt = self.llm.prompts[-1]
        self.assertNotIn("follow-up review", prompt)
        self.assertIn("   11 + x1", prompt)
        self.assertIn("   12 + x2", prompt)
        self.assertIn("of the whole MR", self.summary_note())

    def test_reviewed_head_missing_from_versions_reviews_the_whole_mr(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.publish()
        self.run_review()
        self.server.commit("c2", {"a.py": BASE + "x1\nx2\n"})
        self.publish()
        self.gitlab.versions = self.gitlab.versions[:1]

        self.assertIs(self.run_review(), Outcome.HANDLED)

        self.assertNotIn("follow-up review", self.llm.prompts[-1])
        self.assertIn("of the whole MR", self.summary_note())

    def test_llm_rejection_is_not_retried_until_next_push(self) -> None:
        reviewed = self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.publish()
        self.run_review()

        rejected = self.server.commit("c2", {"a.py": BASE + "x1\nx2\n"})
        self.publish()
        self.llm.answer = LLMRejected("too long")
        self.assertIs(self.run_review(), Outcome.FAILED)
        self.assertIn(markers.review_failed(rejected), self.summary_note())
        self.assertIs(self.run_review(), Outcome.SKIPPED)
        self.assertEqual(len(self.llm.prompts), 2)

        head = self.server.commit("c3", {"a.py": BASE + "x1\nx2\nx3\n"})
        self.publish()
        self.llm.answer = answer()
        self.assertIs(self.run_review(), Outcome.HANDLED)
        # База — последний успешный ревью, а не отвергнутый HEAD: x2 не теряется.
        self.assertIn(f"`{reviewed[:8]}..{head[:8]}`", self.summary_note())
        self.assertIn("   12 * x2", self.llm.prompts[-1])

    def test_failing_inline_posts_go_to_summary(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\nx2\nx3\n"})
        self.publish()
        self.llm.answer = answer(
            ("a.py", 11, "one"), ("a.py", 12, "two"), ("a.py", 13, "three")
        )
        rejected = urllib.error.HTTPError("u", 400, "bad position", Message(), None)
        self.gitlab.discussion_errors = [rejected, None, urllib.error.URLError("down")]

        self.assertIs(self.run_review(), Outcome.FAILED)

        self.assertEqual([p.get("new_line") for _, p in self.gitlab.discussions], [12])
        note = self.summary_note()
        self.assertIn("`a.py:11`", note)
        self.assertIn("`a.py:13`", note)
        self.assertIn("Posting inline comments stopped", note)
        self.assertNotIn("Review in progress", note)

    def test_quick_actions_from_the_llm_are_not_posted(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.publish()
        self.llm.answer = json.dumps(
            {
                "summary": "Fine.\n/approve",
                "findings": [
                    {
                        "path": "a.py",
                        "line": 11,
                        "severity": "major",
                        "body": "x\n/close",
                    },
                    {"path": "a.py", "line": 99, "severity": "minor", "body": "/merge"},
                ],
            }
        )

        self.assertIs(self.run_review(), Outcome.HANDLED)

        bodies = [body for body, _ in self.gitlab.discussions]
        bodies += [note["body"] for note in self.gitlab.notes]
        for body in bodies:
            self.assertNotRegex(body, r"(?m)^/")
        self.assertIn("\n /approve", self.summary_note())
        self.assertIn("\n /close", bodies[0])

    def test_dry_run_writes_nothing(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.publish()
        self.llm.answer = answer(("a.py", 11, "finding"))
        os.environ["DRY_RUN"] = "1"

        self.assertIs(self.run_review(), Outcome.HANDLED)
        self.assertEqual(self.gitlab.notes, [])

    def test_target_merge_only_moves_the_marker(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.publish()
        self.run_review()
        notes = len(self.gitlab.notes)

        self.server.git("checkout", "-q", "main")
        self.server.commit("target", {"t.py": text("target")})
        self.server.git("checkout", "-q", "mr")
        self.server.git("merge", "-q", "--no-edit", "main")
        head = self.publish()["head_sha"]

        self.assertIs(self.run_review(), Outcome.SKIPPED)
        self.assertEqual(len(self.gitlab.notes), notes)
        self.assertIn(markers.reviewed(head), self.summary_note())
        self.assertIs(self.run_review(), Outcome.SKIPPED)
        self.assertEqual(len(self.llm.prompts), 1)

    def test_empty_mr_is_recorded_once(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.server.commit("revert", {"a.py": BASE})
        head = self.publish()["head_sha"]

        self.assertIs(self.run_review(), Outcome.SKIPPED)
        self.assertEqual(len(self.gitlab.notes), 1)
        self.assertIn(markers.reviewed(head), self.summary_note())
        self.assertIs(self.run_review(), Outcome.SKIPPED)
        self.assertEqual(len(self.gitlab.notes), 1)
        self.assertEqual(self.llm.prompts, [])

    def test_empty_diff_after_unavailable_head_moves_the_marker(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.publish()
        self.run_review()
        self.server.commit("revert", {"a.py": BASE})
        head = self.publish()["head_sha"]
        self.gitlab.versions = self.gitlab.versions[:1]

        self.assertIs(self.run_review(), Outcome.SKIPPED)
        self.assertEqual(len(self.gitlab.notes), 1)
        self.assertIn(markers.reviewed(head), self.summary_note())

    def test_dropping_a_line_the_mr_added_is_reviewed(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "check_access()\nx1\n"})
        self.publish()
        self.run_review()

        self.server.commit("c2", {"a.py": BASE + "x1\n"})
        self.publish()

        self.assertIs(self.run_review(), Outcome.HANDLED)
        prompt = self.llm.prompts[-1]
        self.assertIn("removed since the previous review", prompt)
        self.assertIn("--- a.py\n      ~ check_access()", prompt)

    def test_removal_of_a_repeated_line_marks_only_that_line(self) -> None:
        start = BASE.replace("a3", "pass").replace("a7", "pass")
        self.server.git("checkout", "-q", "main")
        self.server.commit("repeated", {"a.py": start})
        self.server.git("checkout", "-q", "mr")
        self.server.git("merge", "-q", "--no-edit", "main")
        # Строка перед удалениями сдвигает номера: `since` и база нумеруются по-разному.
        self.server.commit("c1", {"a.py": "head\n" + start.replace("pass\n", "", 1)})
        self.publish()
        self.run_review()

        self.server.commit("c2", {"a.py": "head\n" + start.replace("pass\n", "")})
        self.publish()

        self.assertIs(self.run_review(), Outcome.HANDLED)
        prompt = self.llm.prompts[-1]
        # По тексту были бы помечены обе; свежая только вторая.
        self.assertRegex(prompt, r"      - pass\n(.*\n)*?      ~ pass\n")
        self.assertEqual(prompt.count("      ~ pass"), 1)

    def test_push_during_review_posts_nothing(self) -> None:
        self.server.commit("c1", {"a.py": BASE + "x1\n"})
        self.publish()
        self.llm.answer = answer(("a.py", 11, "finding"))

        def push_while_reviewing(prompt: str, *, json_object: bool = False) -> str:
            self.server.commit("c2", {"a.py": BASE + "x1\nx2\n"})
            self.publish()
            return answer(("a.py", 11, "finding"))

        with mock.patch("mr_review.chat", push_while_reviewing):
            self.assertIs(self.run_review(), Outcome.HANDLED)
        self.assertEqual(self.gitlab.notes, [])
        self.assertEqual(self.gitlab.discussions, [])

    def test_rules_budget_is_shared_between_files(self) -> None:
        root = self.tmp / "repo"
        for name, size in (("", 30), ("eval", 30), ("frontend", 30)):
            (root / name).mkdir(parents=True, exist_ok=True)
            (root / name / "CLAUDE.md").write_text(name[:1] * size or "r" * size)
        files = {p: FileDiff(p, p) for p in ("eval/x.py", "frontend/y.ts")}

        with mock.patch.object(mr_review, "REPO_ROOT", root), mock.patch.object(
            mr_review, "MAX_RULES_CHARS", 60
        ):
            rules = mr_review.project_rules(files)

        # Каждому файлу — своя доля, последний компонент не выпадает целиком.
        self.assertIn("### frontend/CLAUDE.md\n" + "f" * 20, rules)
        self.assertEqual(rules.count("[rules truncated: 10 more characters]"), 3)
        self.assertNotIn("diff truncated", rules)


class ParseReviewTest(unittest.TestCase):
    def test_json_in_a_code_fence(self) -> None:
        review = parse_review('```json\n{"summary": "S", "findings": []}\n```')
        self.assertEqual((review.summary, review.findings), ("S", []))

    def test_findings_are_validated_and_sorted(self) -> None:
        review = parse_review(
            json.dumps(
                {
                    "summary": "S",
                    "findings": [
                        {
                            "path": "a.py",
                            "line": "3",
                            "severity": "odd",
                            "body": "string line",
                        },
                        {
                            "path": "a.py",
                            "line": True,
                            "severity": "major",
                            "body": "bool line",
                        },
                        {
                            "path": "a.py",
                            "line": 1,
                            "severity": "blocker",
                            "body": "blocker",
                        },
                        {"path": "a.py", "body": "no line"},
                        "not an object",
                    ],
                }
            )
        )
        self.assertEqual(
            [(f.line, f.severity, f.body) for f in review.findings],
            [(1, "blocker", "blocker"), (3, "minor", "string line")],
        )

    def test_not_json_becomes_the_summary(self) -> None:
        review = parse_review("I could not review this.")
        self.assertEqual(
            (review.summary, review.findings), ("I could not review this.", [])
        )


if __name__ == "__main__":
    unittest.main()
