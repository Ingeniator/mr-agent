"""The `mr-review-stats` job: what became of the AI review's inline findings.

How to read the numbers: README.md next to this file.

Left out on purpose until the report regularly shows `n` in the hundreds per prompt
revision; at today's volume their intervals would be 30-90% wide for months and could not
tell two revisions apart. Add them then, in this order:

- Wilson 95% interval on precision: judge a prompt change only once the intervals of the
  two revisions stop overlapping.
- Control lines and lift. Sample up to ~20 random lines per reviewed head that the MR
  added and that have no finding within `NEAR_LINES`, seeded per MR and head, and run them
  through the same `changed_near` check against the final head. Lift is the "changed near"
  rate of findings minus that of control lines; on an MR that keeps rewriting a file a lift
  near zero means the "useful" verdicts measure churn, not the findings.
- Misses: human inline threads on reviewed MRs with no bot finding (inline or in the
  summary) within `NEAR_LINES`, a proxy for what the review did not catch.

Waiting loses nothing: votes, threads and commits are read back from GitLab and git on
every run, so a later version can compute all three over past MRs too.
"""

import functools
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

import markers
from gitlab_api import (
    OPT_IN_LABEL,
    Discussion,
    MergeRequest,
    bot_user_id,
    merge_requests_updated_since,
)
from mr_review import SEVERITIES
from repo import fetch_commit, git

LOOKBACK = timedelta(days=30)
OUTPUT = "review-stats.json"
# Правка рядом с прокомментированной строкой — тоже реакция на находку: проверку обычно
# добавляют строкой выше, а не меняют саму строку.
NEAR_LINES = 3
# Находки до появления маркера ai-finding: только серьёзность в тексте.
_LEGACY = re.compile(rf"🤖 \*\*({'|'.join(SEVERITIES)})\*\*: ")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+", re.MULTILINE)
# Строка находки в итоговой заметке (`summary_note` в mr_review.py).
_SUMMARY_FINDING = re.compile(
    rf"^- `(.+):(\d+)` \*\*({'|'.join(SEVERITIES)})\*\*: ", re.MULTILINE
)


class Verdict(Enum):
    USEFUL = "useful"  # 👍, or the code near the line changed after the comment
    DISMISSED = (
        "dismissed"  # 👎, resolved with no change, or the MR ended with no change
    )
    PENDING = "pending"  # the MR is open and nobody reacted yet


@dataclass
class FindingStats:
    mr: int
    mr_state: str
    path: str
    line: int
    head: str
    severity: str
    prompt: str
    model: str
    resolved: bool
    replies: int
    up: int
    down: int
    # None: the commits could not be fetched, so whether the code changed is unknown.
    changed: bool | None
    verdict: Verdict


@dataclass
class SummaryFinding:
    """A finding the review put into its summary note: no thread, no votes, no verdict."""

    mr: int
    path: str
    line: int
    severity: str


@dataclass
class MrReport:
    findings: list[FindingStats] = field(default_factory=list[FindingStats])
    in_summary: list[SummaryFinding] = field(default_factory=list[SummaryFinding])


@dataclass
class _Raw:
    discussion: Discussion
    head: str
    path: str
    line: int
    severity: str
    prompt: str
    model: str


def _finding(discussion: Discussion, bot: int) -> _Raw | None:
    """The bot's inline finding that opens `discussion`, or None for any other thread."""
    first = discussion["notes"][0]
    position = first.get("position")
    if first["author"]["id"] != bot or position is None:
        return None
    line = position.get("new_line")
    if line is None:
        return None
    path = position.get("new_path", "")
    if match := markers.FINDING.match(first["body"]):
        head, severity, prompt, model = match.groups()
        return _Raw(discussion, head, path, line, severity, prompt, model)
    if match := _LEGACY.match(first["body"]):
        head = position.get("head_sha", "")
        return _Raw(discussion, head, path, line, match.group(1), "legacy", "unknown")
    return None


@functools.cache
def _changed_ranges(path: str, at: str, head: str) -> tuple[tuple[int, int], ...]:
    """Old-side line ranges of `path` that `head` changed since `at`."""
    diff = git("diff", "-U0", "--no-color", "--no-ext-diff", at, head, "--", path)
    ranges: list[tuple[int, int]] = []
    for match in _HUNK.finditer(diff):
        start = int(match.group(1))
        count = int(match.group(2) or 1)
        # Чистая вставка (count 0) идёт после строки start.
        ranges.append((start, start + max(count, 1) - 1))
    return tuple(ranges)


def changed_near(path: str, line: int, at: str, head: str) -> bool:
    """Whether `head` changed `path` within `NEAR_LINES` of `line` as it was at `at`."""
    if at == head:
        return False
    return any(
        start - NEAR_LINES <= line <= last + NEAR_LINES
        for start, last in _changed_ranges(path, at, head)
    )


def _summary_findings(
    iid: int, discussions: list[Discussion], bot: int
) -> list[SummaryFinding]:
    found: list[SummaryFinding] = []
    for discussion in discussions:
        first = discussion["notes"][0]
        if first["author"]["id"] != bot or not markers.REVIEWED.match(first["body"]):
            continue
        for match in _SUMMARY_FINDING.finditer(first["body"]):
            path, line, severity = match.groups()
            found.append(SummaryFinding(iid, path, int(line), severity))
    return found


def verdict(
    state: str, resolved: bool, up: int, down: int, changed: bool | None
) -> Verdict:
    # Явная оценка человека важнее косвенных признаков.
    if up != down:
        return Verdict.USEFUL if up > down else Verdict.DISMISSED
    if changed:
        return Verdict.USEFUL
    if changed is None:
        return Verdict.PENDING
    # Тред закрыли без правки — отклонили. MR закрыт или влит, а кода не трогали — проигнорировали.
    if resolved or state != "opened":
        return Verdict.DISMISSED
    return Verdict.PENDING


def mr_findings(iid: int, state: str) -> list[FindingStats]:
    return mr_report(iid, state).findings


def mr_report(iid: int, state: str) -> MrReport:
    bot = bot_user_id()
    mr = MergeRequest(iid)
    discussions = list(mr.discussions())
    report = MrReport()
    reviewed = any(
        d["notes"][0]["author"]["id"] == bot
        and markers.REVIEWED.match(d["notes"][0]["body"])
        for d in discussions
    )
    raws = [r for d in discussions if (r := _finding(d, bot)) is not None]
    if not reviewed and not raws:
        return report
    report.in_summary = _summary_findings(iid, discussions, bot)
    if not raws:
        return report
    refs = mr.get()["diff_refs"]
    head = refs["head_sha"] if refs and fetch_commit(refs["head_sha"]) else None
    # После force-push HEAD, на котором оставлена находка, сервер может уже не отдать.
    available = {r.head for r in raws if head is not None and fetch_commit(r.head)}
    for raw in raws:
        notes = raw.discussion["notes"]
        first = notes[0]
        votes = [
            e["name"] for e in mr.award_emoji(first["id"]) if e["user"]["id"] != bot
        ]
        changed = (
            changed_near(raw.path, raw.line, raw.head, head)
            if head is not None and raw.head in available
            else None
        )
        resolved = first.get("resolved", False)
        up, down = votes.count("thumbsup"), votes.count("thumbsdown")
        report.findings.append(
            FindingStats(
                mr=iid,
                mr_state=state,
                path=raw.path,
                line=raw.line,
                head=raw.head,
                severity=raw.severity,
                prompt=raw.prompt,
                model=raw.model,
                resolved=resolved,
                replies=sum(1 for n in notes[1:] if n["author"]["id"] != bot),
                up=up,
                down=down,
                changed=changed,
                verdict=verdict(state, resolved, up, down, changed),
            )
        )
    return report


@dataclass
class Group:
    key: str
    total: int = 0
    useful: int = 0
    dismissed: int = 0
    pending: int = 0
    resolved: int = 0
    replied: int = 0
    up: int = 0
    down: int = 0

    def add(self, finding: FindingStats) -> None:
        self.total += 1
        self.useful += finding.verdict is Verdict.USEFUL
        self.dismissed += finding.verdict is Verdict.DISMISSED
        self.pending += finding.verdict is Verdict.PENDING
        self.resolved += finding.resolved
        self.replied += finding.replies > 0
        self.up += finding.up
        self.down += finding.down

    @property
    def decided(self) -> int:
        return self.useful + self.dismissed

    @property
    def precision(self) -> float | None:
        return self.useful / self.decided if self.decided else None

    def summary(self) -> dict[str, object]:
        data: dict[str, object] = asdict(self)
        return data | {"decided": self.decided, "precision": self.precision}


def group(findings: Iterable[tuple[str, FindingStats]]) -> list[Group]:
    groups: dict[str, Group] = {}
    for name, finding in findings:
        groups.setdefault(name, Group(name)).add(finding)
    return list(groups.values())


def report(findings: list[FindingStats]) -> list[Group]:
    """Totals, then per prompt revision, model and severity."""
    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    by_severity = sorted(findings, key=lambda f: order.get(f.severity, len(order)))
    return (
        group(("all", f) for f in findings)
        + group((f"prompt={f.prompt}", f) for f in findings)
        + group((f"model={f.model}", f) for f in findings)
        + group((f"severity={f.severity}", f) for f in by_severity)
    )


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def table(groups: list[Group]) -> str:
    header = (
        f"{'':<24}{'total':>7}{'useful':>8}{'dismiss':>8}{'pending':>8}{'n':>5}"
        + f"{'precision':>11}{'resolved':>10}{'replied':>9}{'👍':>4}{'👎':>4}"
    )
    rows = [header]
    for g in groups:
        rows.append(
            f"{g.key:<24}{g.total:>7}{g.useful:>8}{g.dismissed:>8}{g.pending:>8}{g.decided:>5}"
            + f"{_percent(g.precision):>11}"
            + f"{g.resolved:>10}{g.replied:>9}{g.up:>4}{g.down:>4}"
        )
    return "\n".join(rows)


def _now() -> datetime:
    return datetime.now(UTC)


def main() -> int:
    days = os.environ.get("REVIEW_STATS_DAYS")
    since = _now() - (timedelta(days=int(days)) if days else LOOKBACK)
    status = 0
    mrs: list[MrReport] = []
    for ref in merge_requests_updated_since(OPT_IN_LABEL, since):
        try:
            mrs.append(mr_report(ref["iid"], ref["state"]))
        except Exception as error:
            print(f"!{ref['iid']} failed: {type(error).__name__}: {error}")
            status = 1
    findings = [f for m in mrs for f in m.findings]
    in_summary = [x for m in mrs for x in m.in_summary]
    groups = report(findings)
    print(f"AI review findings on MRs updated since {since:%Y-%m-%d}:\n")
    print(table(groups))
    by_severity = ", ".join(
        f"{s} {sum(f.severity == s for f in in_summary)}" for s in SEVERITIES
    )
    print(
        f"\nFindings in summary notes, not in the table: {len(in_summary)} ({by_severity})."
    )
    output = os.environ.get("REVIEW_STATS_JSON") or OUTPUT
    data = {
        "since": since.isoformat(),
        "groups": [g.summary() for g in groups],
        "findings": [asdict(f) | {"verdict": f.verdict.value} for f in findings],
        "in_summary": [asdict(f) for f in in_summary],
    }
    with open(output, "w") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    return status


if __name__ == "__main__":
    sys.exit(main())
