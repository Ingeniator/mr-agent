"""The `mr-review` job: incremental LLM review of open MRs labelled `ai-review`.

The rules in the prompt are the root CLAUDE.md and the CLAUDE.md of every touched
component, read from the job's checkout of the default branch, never from the MR head:
an MR cannot rewrite the rules it is reviewed against.

Incremental review. The summary note's marker records the reviewed head. The next review
sends the full MR diff of the files touched since then, with the lines added since then
marked `*` and the lines removed since then marked `~` (`since_last_review`), and posts
inline comments only on `*` lines; other findings go into the summary note. Lines the MR
had added and has removed since the last review are no longer in the MR diff, so the
prompt lists them separately (`dropped_section`). Changes that came in by merging or
rebasing onto the target branch drop out; when nothing else changed, the marker moves to
the new head without a new note (`record_no_changes`). After a force-push the old head is
fetched by SHA (`repo.fetch_commit`); if it cannot be fetched, or no MR version had it as
its head (`previous_base`), the whole MR is reviewed again.

Setup, failure handling and local runs: README.md next to this file.
"""

import hashlib
import json
import sys
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import markers
from common import env, truncate
from diff import FileDiff, Fresh, parse_diff
from gitlab_api import (
    DiffRefs,
    MergeRequest,
    Note,
    Outcome,
    Position,
    Result,
    bot_user_id,
    commit_log,
    for_each_open_mr,
    latency,
    skipped,
)
from llm import LLMRejected, chat
from project import PROJECT
from repo import fetch_commit, fetch_commits, git

MAX_FINDINGS = 15
# Вмещает корневой CLAUDE.md и CLAUDE.md всех компонентов.
MAX_RULES_CHARS = 80_000
SEVERITIES = ("blocker", "major", "minor")
# Джоба запускается из корня клона проекта (ветка по умолчанию), правила читаются оттуда.
REPO_ROOT = Path.cwd()

PROMPT = """You are a senior reviewer of merge requests.
{project}
Review the diff below against the project rules.

Report only real problems: bugs, unhandled errors, security issues, changes that break
the previous release running side by side (expand/contract rule), behaviour changes
without tests, and violations of the project rules. No praise, no style nits a formatter
would fix, no speculation about code you cannot see. Fewer, well-founded findings are
better than many weak ones; an empty list is a fine answer.

Each diff line has its new-file line number on the left ("  42 + added", "  42   context");
removed lines have no number. A finding must point at a numbered line of the same file.

{focus}Answer with a single JSON object and nothing else:
{{"summary": "<2-5 sentences, Markdown: overall assessment and the main risk>",
  "findings": [{{"path": "<new file path>", "line": <number>,
                 "severity": "blocker|major|minor", "body": "<problem and suggested fix, Markdown>"}}]}}

Project rules:
{rules}

Commits under review:
{commits}

Diff:
{diff}
"""

FOLLOW_UP = """This is a follow-up review. Lines marked "*" instead of "+" ("  42 * added") were
added and lines marked "~" instead of "-" ("     ~ removed") were removed since the previous
review. Review only these changes — a removed check or guard is a finding too; the other lines
are already reviewed and shown as context. Point a finding about added code at its "*" line,
and a finding about removed code at the nearest numbered line.

"""


def prompt_version() -> str:
    """Short id of the prompt templates, so findings can be measured per prompt revision."""
    return hashlib.sha256((PROJECT + PROMPT + FOLLOW_UP).encode()).hexdigest()[:8]


@dataclass
class Finding:
    path: str
    line: int
    severity: str
    body: str


@dataclass
class Review:
    summary: str
    findings: list[Finding]


def project_rules(files: dict[str, FileDiff]) -> str:
    components = sorted({path.split("/", 1)[0] for path in files if "/" in path})
    docs = [REPO_ROOT / "CLAUDE.md"] + [REPO_ROOT / c / "CLAUDE.md" for c in components]
    texts = {doc: doc.read_text() for doc in docs if doc.is_file()}
    # Бюджет делится поровну, а недобор коротких файлов отдаётся длинным: общий обрез с конца
    # молча выкидывал бы правила последних компонентов целиком.
    budget = MAX_RULES_CHARS
    limits: dict[Path, int] = {}
    for left, doc in enumerate(sorted(texts, key=lambda d: len(texts[d])), start=0):
        limits[doc] = min(len(texts[doc]), budget // (len(texts) - left))
        budget -= limits[doc]
    return "\n\n".join(
        f"### {doc.relative_to(REPO_ROOT)}\n{truncate(text, limits[doc], 'rules')}"
        for doc, text in texts.items()
    )


def parse_review(answer: str) -> Review:
    # Шлюз может проигнорировать response_format, и модель обернёт JSON в ```json или текст.
    start, end = answer.find("{"), answer.rfind("}")
    try:
        data: object = json.loads(answer[start : end + 1]) if start != -1 else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return Review(summary=answer, findings=[])
    fields = cast(dict[str, object], data)
    raw_findings = fields.get("findings")
    items = cast(list[object], raw_findings) if isinstance(raw_findings, list) else []
    findings: list[Finding] = []
    for item in (cast(dict[str, object], i) for i in items if isinstance(i, dict)):
        path, line, body = item.get("path"), item.get("line"), item.get("body")
        severity = item.get("severity")
        # Модель иногда пишет номер строкой ("42"); bool — подкласс int, его не принимаем.
        if isinstance(line, str) and line.strip().isdigit():
            line = int(line)
        if not (isinstance(path, str) and isinstance(body, str)):
            continue
        if isinstance(line, int) and not isinstance(line, bool):
            if not (isinstance(severity, str) and severity in SEVERITIES):
                severity = "minor"
            findings.append(Finding(path, line, severity, body))
    findings.sort(key=lambda f: SEVERITIES.index(f.severity))
    return Review(
        summary=str(fields.get("summary") or ""), findings=findings[:MAX_FINDINGS]
    )


@dataclass
class ReviewMarkers:
    # Последний отревьюенный HEAD — база инкрементального ревью.
    reviewed: str | None = None
    reviewed_note: Note | None = None
    # HEAD, ревью которого LLM отвергла уже после `reviewed`.
    failed: str | None = None


def review_markers(mr: MergeRequest) -> ReviewMarkers:
    """The last reviewed head with its note, and the last head the LLM rejected after it."""
    # Состояние — только маркер первой строкой заметки бота; всё прочее может быть цитатой.
    bot = bot_user_id()
    found = ReviewMarkers()
    for note in mr.notes():
        if note["system"] or note["author"]["id"] != bot:
            continue
        if found.failed is None and (
            match := markers.REVIEW_FAILED.match(note["body"])
        ):
            found.failed = match.group(1)
        if match := markers.REVIEWED.match(note["body"]):
            found.reviewed, found.reviewed_note = match.group(1), note
            return found
    return found


def since_last_review(
    since_diff: str,
    mr_files: dict[str, FileDiff],
    own_at_since: dict[str, FileDiff],
    base_to_since: dict[str, FileDiff],
) -> Fresh:
    """The changes since the last review that are still changes of the MR.

    Added lines are matched by number: both diffs number lines of `head`. Removed lines are
    numbered in `since`; `base_to_since` (the MR base against `since`) maps them to the
    base the MR diff removes them from. After the target branch is merged into the MR,
    `since..head` also carries the target's commits; their lines are not changes of the MR
    diff and drop out here. `own_at_since` is the MR's own diff at the last review: lines
    it added at `since` and that are removed since then go to `Fresh.dropped`.
    """
    _, since_files = parse_diff(since_diff)
    fresh = Fresh()
    for path, file in since_files.items():
        # Ключи own_at_since и base_to_since — пути на `since`, то есть старый путь здесь.
        own = own_at_since.get(file.old_path)
        if own:
            # Сверка с собственным диффом MR отсекает удаления целевой ветки.
            added = own.added()
            dropped = [
                text
                for line, text in sorted(file.removed.items())
                if line in added and text.strip()
            ]
            if dropped:
                fresh.dropped[path] = dropped
        mr_file = mr_files.get(path)
        if mr_file is None:
            continue
        fresh.added |= {(path, line) for line in file.added() & mr_file.added()}
        # Файла нет в диффе base..since — на `since` он такой же, как в базе.
        to_base = base_to_since.get(file.old_path)
        removed = {
            at_base
            for line in file.removed
            if (at_base := to_base.old_line(line) if to_base else line) is not None
            and at_base in mr_file.removed
        }
        if removed:
            fresh.removed[path] = removed
    return fresh


def dropped_section(fresh: Fresh) -> str:
    if not fresh.dropped:
        return ""
    lines = [
        "Lines the MR had added earlier and removed since the previous review. They are no",
        "longer in the diff at all; review their removal too (a dropped check or guard is a",
        "finding). Point such a finding at the nearest numbered line of the same file.",
    ]
    for path in sorted(fresh.dropped):
        lines.append(f"--- {path}")
        lines += [f"      ~ {text}" for text in fresh.dropped[path]]
    return "\n".join(lines) + "\n\n"


def finding_text(finding: Finding) -> str:
    return f"**{finding.severity}**: {finding.body}"


def post_inline(
    mr: MergeRequest, refs: DiffRefs, file: FileDiff, finding: Finding
) -> None:
    position = Position(
        position_type="text",
        base_sha=refs["base_sha"],
        start_sha=refs["start_sha"],
        head_sha=refs["head_sha"],
        old_path=file.old_path,
        new_path=file.new_path,
        new_line=finding.line,
    )
    old_line = file.lines[finding.line]
    if old_line is not None:
        position["old_line"] = old_line
    marker = markers.finding(
        refs["head_sha"], finding.severity, prompt_version(), env("LLM_MODEL")
    )
    mr.create_discussion(f"{marker}\n🤖 {finding_text(finding)}", position)


def git_diff(*revisions: str) -> str:
    return git("diff", "--no-color", "--no-ext-diff", *revisions)


def skip_reason(found: ReviewMarkers, head: str) -> str | None:
    if found.reviewed == head:
        return f"{head[:8]} is already reviewed."
    if found.failed == head:
        return (
            f"The LLM rejected the review of {head[:8]}; retried after the next push."
        )
    return None


@dataclass
class Scope:
    """What this run reviews: the whole MR, or with `since` only the changes after that head.

    `mr_files` and the inline positions always come from the full MR diff, even in an
    incremental review: GitLab accepts inline comments only on lines of the MR diff.
    """

    head: str
    since: str | None
    mr_files: dict[str, FileDiff]
    annotated: str
    fresh: Fresh | None

    @property
    def label(self) -> str:
        return (
            "the whole MR"
            if self.since is None
            else f"`{self.since[:8]}..{self.head[:8]}`"
        )

    def is_empty(self) -> bool:
        return not self.annotated.strip() and not (self.fresh and self.fresh.dropped)

    def inline_file(self, finding: Finding) -> FileDiff | None:
        """The file to attach `finding` to, or None to put it into the summary note."""
        file = self.mr_files.get(finding.path)
        if file is None or finding.line not in file.lines:
            return None
        # На уже отревьюенных строках inline копились бы повторы от ревью к ревью.
        if (
            self.fresh is not None
            and (finding.path, finding.line) not in self.fresh.added
        ):
            return None
        return file


def previous_base(mr: MergeRequest, since: str | None) -> str | None:
    """The MR base at the last review, fetched along with `since`; None to review the whole MR."""
    if since is None:
        return None
    # После force-push `since` не предок HEAD, но since_last_review всё равно отсеет лишнее.
    old_base = mr.base_at(since)
    if old_base is None or not fetch_commit(since):
        print(f"Last reviewed head {since[:8]} is unavailable, reviewing the whole MR.")
        return None
    fetch_commits(old_base)
    return old_base


def collect_scope(mr: MergeRequest, refs: DiffRefs, since: str | None) -> Scope:
    head = refs["head_sha"]
    fetch_commits(refs["base_sha"], head)
    mr_diff = git_diff(refs["base_sha"], head)
    _, mr_files = parse_diff(mr_diff)
    old_base = previous_base(mr, since)
    fresh: Fresh | None = None
    if since is not None and old_base is not None:
        _, own_at_since = parse_diff(git_diff(old_base, since))
        _, base_to_since = parse_diff(git_diff(refs["base_sha"], since))
        fresh = since_last_review(
            git_diff(since, head), mr_files, own_at_since, base_to_since
        )
    annotated, _ = parse_diff(mr_diff, fresh)
    return Scope(
        head=head,
        since=since if fresh is not None else None,
        mr_files=mr_files,
        annotated=annotated,
        fresh=fresh,
    )


def record_no_changes(mr: MergeRequest, found: ReviewMarkers, head: str) -> None:
    """Mark `head` reviewed without asking the LLM, so later runs do not diff it again.

    Typical case: the target branch was merged into the MR. An existing marker is moved,
    including one on an unavailable earlier head, rather than posting a new note.
    """
    if found.reviewed is not None and found.reviewed_note is not None:
        note = found.reviewed_note
        body = note["body"].replace(
            markers.reviewed(found.reviewed), markers.reviewed(head)
        )
        mr.update_note(note["id"], body)
    else:
        empty = "🤖 **AI review**: the MR has no changes to review."
        _ = mr.create_note(f"{markers.reviewed(head)}\n{empty}")


def build_prompt(mr: MergeRequest, scope: Scope) -> str:
    return PROMPT.format(
        project=PROJECT,
        rules=project_rules(scope.mr_files),
        focus="" if scope.fresh is None else FOLLOW_UP + dropped_section(scope.fresh),
        commits=commit_log(mr.commits(), scope.since),
        diff=truncate(scope.annotated),
    )


def record_rejection(mr: MergeRequest, scope: Scope, error: LLMRejected) -> None:
    """Mark `head` as rejected so the same prompt is not resent every run.

    A separate marker keeps the base of the next review where it was, so the commits
    the LLM never saw are reviewed after the next push.
    """
    print(f"!{mr.iid}: {error}")
    failed = f"🤖 **AI review** of {scope.label} failed: {error}. It is retried after the next push."
    _ = mr.create_note(f"{markers.review_failed(scope.head)}\n{failed}")


def head_moved(mr: MergeRequest, head: str) -> bool:
    current = mr.get()["diff_refs"]
    return current is None or current["head_sha"] != head


@dataclass
class Posting:
    placed: int = 0
    unplaced: list[Finding] = field(default_factory=list[Finding])
    # Почему inline-комментарии прекращены; None — дошли до конца.
    failure: str | None = None


def post_findings(
    mr: MergeRequest, refs: DiffRefs, scope: Scope, findings: list[Finding]
) -> Posting:
    """Post findings as inline comments; what cannot be placed goes to `Posting.unplaced`.

    A rejected position (400) moves one finding to the summary. Any other error stops
    inline posting and moves the rest there: the marker note is already published, so a
    crash here would lose the findings for good.
    """
    posting = Posting()
    for index, finding in enumerate(findings):
        file = scope.inline_file(finding)
        if file is None:
            posting.unplaced.append(finding)
            continue
        where = f"{finding.path}:{finding.line}"
        try:
            post_inline(mr, refs, file, finding)
            posting.placed += 1
        except urllib.error.HTTPError as error:
            if error.code != 400:
                posting.failure = f"HTTP {error.code}: {error.reason}"
            else:
                print(f"Inline comment on {where} rejected (400), moved to summary.")
                posting.unplaced.append(finding)
        except Exception as error:
            posting.failure = f"{type(error).__name__}: {error}"
        if posting.failure:
            print(
                f"Inline comment on {where} failed ({posting.failure}), the rest goes to summary."
            )
            posting.unplaced += findings[index:]
            break
    return posting


def note_header(scope: Scope) -> list[str]:
    return [markers.reviewed(scope.head), f"🤖 **AI review** of {scope.label}", ""]


def summary_note(scope: Scope, result: Review, posting: Posting) -> str:
    lines = note_header(scope) + [result.summary or "—"]
    if posting.unplaced:
        lines += ["", "Findings not attached to a new diff line:"]
        lines += [f"- `{f.path}:{f.line}` {finding_text(f)}" for f in posting.unplaced]
    if posting.failure:
        lines += ["", f"⚠️ Posting inline comments stopped: `{posting.failure}`."]
    lines += [
        "",
        f"<sub>{posting.placed} inline comment(s). Generated by an LLM — verify before acting. "
        + "React 👍 or 👎 on an inline comment to rate it: the ratings measure the reviewer.</sub>",
    ]
    return "\n".join(lines)


def review(mr: MergeRequest) -> Result:
    refs = mr.get()["diff_refs"]
    if refs is None:
        return skipped("No diff refs yet, skipping.")
    head = refs["head_sha"]
    found = review_markers(mr)
    if reason := skip_reason(found, head):
        return skipped(reason)

    scope = collect_scope(mr, refs, found.reviewed)
    if scope.is_empty():
        record_no_changes(mr, found, head)
        return skipped("No changes to review.")

    waiting = mr.waiting_since(found.reviewed)
    try:
        answer = chat(build_prompt(mr, scope), json_object=True)
    except LLMRejected as error:
        record_rejection(mr, scope, error)
        return Result(
            Outcome.FAILED, "rejected", str(error), head, latency_s=latency(waiting)
        )
    result = parse_review(answer)

    # Комментарии легли бы на устаревшую версию диффа, а маркер — на старый HEAD.
    # Следующий запуск отревьюит новый HEAD от прежней базы.
    if head_moved(mr, head):
        print("MR head moved on during the review, skipping: the next run reviews it.")
        return Result(Outcome.HANDLED, "head_moved", head=head)

    # Маркер публикуется до inline-комментариев: если дальше что-то упадёт, следующий
    # пайплайн увидит этот HEAD отревьюенным и не запостит комментарии повторно.
    in_progress = "\n".join(note_header(scope) + ["Review in progress…"])
    note_id = mr.create_note(in_progress)["id"]
    reviewed_after = latency(waiting)
    posting = post_findings(mr, refs, scope, result.findings)
    note = summary_note(scope, result, posting)
    try:
        mr.update_note(note_id, note)
    except Exception:
        print(note)  # заметка осталась «in progress» — пусть ревью будет хотя бы в логе
        raise
    print(
        f"Posted review of !{mr.iid}: {posting.placed} inline, {len(posting.unplaced)} in summary."
    )
    return Result(
        Outcome.FAILED if posting.failure else Outcome.HANDLED,
        "reviewed",
        posting.failure,
        head,
        inline=posting.placed,
        in_summary=len(posting.unplaced),
        latency_s=reviewed_after,
    )


if __name__ == "__main__":
    sys.exit(for_each_open_mr("review", review))
