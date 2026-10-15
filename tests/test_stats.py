# Атрибуты TestCase задаёт setUp, а не __init__.
# pyright: reportUninitializedInstanceVariable=false

import json
import os
from unittest import mock

import gitlab_api
import markers
import mr_review
import mr_review_stats
from gitlab_api import AwardEmoji, Position, User
from mr_review_stats import FindingStats, Verdict, report
from tests.support import BOT, IID, JobTestCase, text

BASE = text(*(f"a{n}" for n in range(1, 21)))  # a1 … a20
HUMAN = 2


def answer(*lines: int) -> str:
    findings = [
        {"path": "a.py", "line": line, "severity": "major", "body": f"line {line}"}
        for line in lines
    ]
    return json.dumps({"summary": "Summary.", "findings": findings})


class StatsTest(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("mr_review.chat", self.llm)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server.commit("base", {"a.py": BASE})
        self.server.checkout("mr", new=True)
        # Находки на строках 2 и 15: между ними больше NEAR_LINES.
        self.server.commit(
            "c1", {"a.py": BASE.replace("a2\n", "A2\n").replace("a15\n", "A15\n")}
        )
        self.reviewed_head = self.publish()["head_sha"]
        self.llm.answer = answer(2, 15)
        self.fresh_clone()
        _ = mr_review.review(self.mr())

    def push(self, content: str) -> None:
        self.server.commit("fix", {"a.py": content})
        self.publish()

    def stats(self, state: str = "opened") -> dict[int, FindingStats]:
        self.gitlab.state = state
        self.fresh_clone()
        return {f.line: f for f in mr_review_stats.mr_findings(IID, state)}

    def vote(self, thread: int, name: str, user: int = HUMAN) -> None:
        note_id = self.gitlab.threads[thread]["notes"][0]["id"]
        emoji = AwardEmoji(name=name, user=User(id=user))
        self.gitlab.emoji.setdefault(note_id, []).append(emoji)

    def test_inline_findings_carry_the_prompt_and_model(self) -> None:
        body = self.gitlab.discussions[0][0]
        match = markers.FINDING.match(body)
        assert match is not None
        self.assertEqual(
            match.groups(),
            (self.reviewed_head, "major", mr_review.prompt_version(), "gpt-4o"),
        )
        self.assertIn("👍 or 👎", self.gitlab.notes[-1]["body"])

    def test_a_change_near_the_line_is_useful(self) -> None:
        # Проверку добавили строкой выше, сама строка не менялась.
        fixed = BASE.replace("a2\n", "A2\n").replace("a15\n", "A15\n")
        self.push(fixed.replace("a1\n", "a1\nguard()\n"))

        found = self.stats()

        self.assertIs(found[2].verdict, Verdict.USEFUL)
        self.assertIs(found[15].verdict, Verdict.PENDING)
        self.assertEqual(found[2].prompt, mr_review.prompt_version())

    def test_an_mr_merged_without_a_change_dismisses_the_finding(self) -> None:
        found = self.stats(state="merged")
        self.assertIs(found[2].verdict, Verdict.DISMISSED)

    def test_resolved_without_a_change_is_dismissed(self) -> None:
        self.gitlab.threads[0]["notes"][0]["resolved"] = True
        self.gitlab.reply(self.gitlab.threads[0], "Not a problem here.", HUMAN)

        found = self.stats()

        self.assertIs(found[2].verdict, Verdict.DISMISSED)
        self.assertEqual((found[2].resolved, found[2].replies), (True, 1))

    def test_votes_outweigh_code_changes(self) -> None:
        self.push(BASE)  # обе строки изменены
        self.vote(0, "thumbsdown")
        self.vote(1, "thumbsup", user=BOT)  # голос самого бота не считается

        found = self.stats()

        self.assertIs(found[2].verdict, Verdict.DISMISSED)
        self.assertIs(found[15].verdict, Verdict.USEFUL)
        self.assertEqual((found[15].up, found[15].changed), (0, True))

    def test_an_upvote_on_a_merged_mr_is_useful(self) -> None:
        self.vote(1, "thumbsup")
        self.assertIs(self.stats(state="merged")[15].verdict, Verdict.USEFUL)

    def test_finding_on_an_unavailable_head_is_pending(self) -> None:
        self.gitlab.threads[0]["notes"][0]["body"] = markers.finding(
            "f" * 40, "major", "p", "m"
        )
        found = self.stats(state="merged")
        self.assertIsNone(found[2].changed)
        self.assertIs(found[2].verdict, Verdict.PENDING)

    def test_legacy_findings_without_a_marker_are_read(self) -> None:
        first = self.gitlab.threads[0]["notes"][0]
        first["body"] = "🤖 **minor**: old style"
        found = self.stats()
        self.assertEqual((found[2].severity, found[2].prompt), ("minor", "legacy"))

    def test_threads_not_opened_by_the_bot_are_ignored(self) -> None:
        self.gitlab.threads[0]["notes"][0]["author"] = User(id=HUMAN)
        position = Position(new_path="a.py", new_line=3)
        self.gitlab.threads[1]["notes"][0]["position"] = position
        self.gitlab.threads[1]["notes"][0]["body"] = "🤖 **AI review** summary"
        self.assertEqual(self.stats(), {})

    def test_bot_user_id_overrides_the_token_user(self) -> None:
        # Локальный запуск с личным токеном: находки бота ищутся по MR_BOT_USER_ID.
        for thread in self.gitlab.threads:
            thread["notes"][0]["author"] = User(id=HUMAN)
        os.environ["MR_BOT_USER_ID"] = str(HUMAN)
        gitlab_api.bot_user_id.cache_clear()
        self.assertEqual(sorted(self.stats()), [2, 15])

    def test_main_writes_the_report(self) -> None:
        self.vote(0, "thumbsup")
        output = self.tmp / "stats.json"
        self.fresh_clone()
        with mock.patch.dict(os.environ, {"REVIEW_STATS_JSON": str(output)}):
            self.assertEqual(mr_review_stats.main(), 0)
        data = json.loads(output.read_text())
        self.assertEqual(len(data["findings"]), 2)
        overall = data["groups"][0]
        self.assertEqual(
            (overall["key"], overall["useful"], overall["pending"]), ("all", 1, 1)
        )
        self.assertEqual(overall["precision"], 1.0)


class ReportTest(JobTestCase):
    def finding(self, verdict: Verdict, severity: str = "major") -> FindingStats:
        return FindingStats(
            mr=1,
            mr_state="merged",
            path="a.py",
            line=1,
            head="h",
            severity=severity,
            prompt="p1",
            model="m",
            resolved=False,
            replies=0,
            up=0,
            down=0,
            changed=False,
            verdict=verdict,
        )

    def test_precision_ignores_pending(self) -> None:
        groups = report(
            [
                self.finding(Verdict.USEFUL, "minor"),
                self.finding(Verdict.DISMISSED),
                self.finding(Verdict.DISMISSED),
                self.finding(Verdict.PENDING, "blocker"),
            ]
        )
        by_key = {g.key: g for g in groups}
        self.assertAlmostEqual(by_key["all"].precision or 0, 1 / 3)
        self.assertIsNone(by_key["severity=blocker"].precision)
        self.assertEqual(
            [g.key for g in groups if g.key.startswith("severity=")],
            ["severity=blocker", "severity=major", "severity=minor"],
        )

    def test_table_shows_the_decided_count_next_to_the_precision(self) -> None:
        groups = report([self.finding(Verdict.USEFUL), self.finding(Verdict.DISMISSED)])
        row = mr_review_stats.table(groups).splitlines()[1]
        self.assertIn("    2        50%", row)

    def test_echoed_finding_marker_is_stripped(self) -> None:
        marker = markers.finding("a" * 40, "major", "p", "m")
        self.assertEqual(markers.strip(f"x{marker}y"), "xy")


class SummaryNoteTest(JobTestCase):

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("mr_review.chat", self.llm)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server.commit("base", {"a.py": BASE})
        self.server.checkout("mr", new=True)
        # Находки на 2 и 15, а 100 — вне диффа, в итоговую заметку.
        self.server.commit("c1", {"a.py": BASE.upper()})
        self.publish()
        self.llm.answer = answer(2, 15, 100)
        self.fresh_clone()
        _ = mr_review.review(self.mr())

    def run_report(self) -> mr_review_stats.MrReport:
        self.fresh_clone()
        return mr_review_stats.mr_report(IID, "opened")

    def test_findings_in_the_summary_note_are_counted(self) -> None:
        found = self.run_report()
        self.assertEqual(
            [(f.path, f.line, f.severity) for f in found.in_summary],
            [("a.py", 100, "major")],
        )
        self.assertEqual(len(found.findings), 2)

    def test_unreviewed_mrs_report_nothing(self) -> None:
        self.gitlab.threads.clear()
        self.gitlab.notes.clear()
        self.assertEqual(self.run_report(), mr_review_stats.MrReport())
