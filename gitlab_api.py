"""The GitLab REST API client and the loop over open MRs that both jobs run."""

import functools
import json
import os
import re
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Literal, NotRequired, TypedDict, cast

import markers
import telemetry
from common import dry_run, env, request

MAX_MRS_PER_RUN = 3
OPT_IN_LABEL = "ai-review"
# Без паузы MR, на котором джоба падает всегда, занимал бы слот и красил бы каждый запуск.
ERROR_BACKOFF = timedelta(minutes=10)
MAX_ERROR_BACKOFF = timedelta(hours=6)

Job = Literal["description", "review"]

# Текст бота пишет LLM по диффу, так что ветка MR может подсунуть quick action (/approve …),
# выполненный от имени бота. Пробел перед «/» отключает команду, а Markdown не меняет.
_QUICK_ACTION = re.compile(r"^/", re.MULTILINE)
# Так же и @all: бот никого не упоминает, поэтому zero-width space после «@» гасит все
# упоминания. «@» в виде HTML-сущности GitLab тоже распознаёт.
_AT_ENTITY = re.compile(r"&(?:#0*64|#x0*40|commat);", re.IGNORECASE)
_MENTION = re.compile(r"(?<!\w)@(?=[\w.-])")


def inert(text: str) -> str:
    """`text` that GitLab neither runs as quick actions nor treats as @mentions."""
    text = _MENTION.sub("@\u200b", _AT_ENTITY.sub("@", text))
    return _QUICK_ACTION.sub(" /", text)


class DiffRefs(TypedDict):
    base_sha: str
    start_sha: str
    head_sha: str


class MergeRequestData(TypedDict):
    description: str | None
    diff_refs: DiffRefs | None


class Version(TypedDict):
    head_commit_sha: str
    base_commit_sha: str
    start_commit_sha: str
    created_at: NotRequired[str]


class Commit(TypedDict):
    id: str
    short_id: str
    title: str
    parent_ids: NotRequired[list[str]]


class User(TypedDict):
    id: int


class Note(TypedDict):
    id: int
    body: str
    system: bool
    author: User
    updated_at: str


class Position(TypedDict, total=False):
    position_type: str
    base_sha: str
    start_sha: str
    head_sha: str
    old_path: str
    new_path: str
    new_line: int
    old_line: int


class _MergeRequestRef(TypedDict):
    iid: int


class MergeRequestRef(TypedDict):
    iid: int
    state: str  # opened, merged, closed, locked


class DiscussionNote(TypedDict):
    id: int
    body: str
    system: bool
    author: User
    resolved: NotRequired[bool]
    position: NotRequired[Position]


class Discussion(TypedDict):
    id: str
    notes: list[DiscussionNote]


class AwardEmoji(TypedDict):
    name: str
    user: User


class _Token(TypedDict):
    expires_at: str | None


def _auth() -> dict[str, str]:
    return {"PRIVATE-TOKEN": env("MR_BOT_TOKEN")}


def _project_api() -> str:
    return f"{env('CI_API_V4_URL')}/projects/{env('CI_PROJECT_ID')}"


def _pages(url: str) -> Iterator[object]:
    """Items of a paginated GitLab list endpoint, fetched page by page as they are consumed."""
    page = 1
    while True:
        batch = cast(
            list[object], request("GET", f"{url}&per_page=100&page={page}", _auth())
        )
        yield from batch
        if len(batch) < 100:
            return
        page += 1


@functools.cache
def bot_user_id() -> int:
    """The bot user: `MR_BOT_USER_ID` if set, else the user `MR_BOT_TOKEN` acts as."""
    # Локальный запуск с личным токеном: без этого ботом считается сам разработчик, и ни
    # маркеров прошлых ревью, ни находок бота не находится.
    if override := os.environ.get("MR_BOT_USER_ID"):
        return int(override)
    return cast(User, request("GET", f"{env('CI_API_V4_URL')}/user", _auth()))["id"]


def token_expires_on() -> date | None:
    """The day `MR_BOT_TOKEN` expires; None if it never does."""
    url = f"{env('CI_API_V4_URL')}/personal_access_tokens/self"
    expires = cast(_Token, request("GET", url, _auth()))["expires_at"]
    return date.fromisoformat(expires) if expires else None


def open_merge_requests(label: str) -> list[int]:
    """IIDs of the project's open MRs carrying `label`."""
    url = f"{_project_api()}/merge_requests?state=opened&order_by=updated_at&sort=desc"
    url += f"&labels={urllib.parse.quote(label)}"
    return [cast(_MergeRequestRef, item)["iid"] for item in _pages(url)]


def merge_requests_updated_since(label: str, since: datetime) -> list[MergeRequestRef]:
    """The project's MRs in any state carrying `label`, updated after `since`."""
    url = f"{_project_api()}/merge_requests?state=all&order_by=updated_at&sort=desc"
    url += f"&labels={urllib.parse.quote(label)}"
    url += f"&updated_after={urllib.parse.quote(since.isoformat())}"
    return [cast(MergeRequestRef, item) for item in _pages(url)]


class MergeRequest:
    """One merge request of the project, via the GitLab REST API.

    With `DRY_RUN=1` every write is printed instead of sent; reads still go to GitLab.
    """

    def __init__(self, iid: int) -> None:
        self.iid: int = iid
        self.api: str = f"{_project_api()}/merge_requests/{iid}"
        self._auth: dict[str, str] = _auth()

    def _write(self, method: str, path: str, body: Mapping[str, object]) -> object:
        if not dry_run():
            return request(method, f"{self.api}{path}", self._auth, body)
        print(f"[dry run] {method} !{self.iid}{path}")
        for key, value in body.items():
            print(value if isinstance(value, str) else f"{key}: {json.dumps(value)}")
        return {"id": 0}

    def get(self) -> MergeRequestData:
        return cast(MergeRequestData, request("GET", self.api, self._auth))

    def commits(self) -> list[Commit]:
        """Commits of the MR's latest version, newest first, as GitLab lists them."""
        return [cast(Commit, item) for item in _pages(f"{self.api}/commits?")]

    @functools.cached_property
    def versions(self) -> list[Version]:
        """The MR's versions, newest first, fetched once per instance: a run reads them
        several times, and a push during the run is caught by the head check anyway."""
        return [cast(Version, item) for item in _pages(f"{self.api}/versions?")]

    def base_at(self, head: str) -> str | None:
        """The MR base when `head` was its head; None if no version of the MR had that head."""
        # В shallow-клоне базу не вычислить, а у версии она точная и после rebase.
        for version in self.versions:
            if version["head_commit_sha"] == head:
                return version["base_commit_sha"]
        return None

    def waiting_since(self, handled: str | None) -> datetime | None:
        """When the oldest push not handled yet was made: the first version after the one
        whose head is `handled`.

        Without `handled`, or when no version had it, the current version counts: earlier
        pushes may predate the opt-in label, and their wait is not the bot's.
        """
        versions = self.versions
        newer: list[Version] = []
        for version in versions:
            if version["head_commit_sha"] == handled:
                break
            newer.append(version)
        else:
            newer = versions[:1]
        created = newer[-1].get("created_at") if newer else None
        return datetime.fromisoformat(created) if created else None

    def head_pushed_at(self) -> datetime | None:
        """When the MR's current version was created, i.e. its head was pushed."""
        if not self.versions:
            return None
        created = self.versions[0].get("created_at")
        return datetime.fromisoformat(created) if created else None

    def update_description(self, description: str) -> None:
        """Write `description` as is: it carries the author's text, so only the caller can
        make the bot's own part inert."""
        _ = self._write("PUT", "", {"description": description})

    def notes(self) -> Iterator[Note]:
        """All notes of the MR, newest first, fetched page by page as they are consumed."""
        for item in _pages(f"{self.api}/notes?sort=desc&order_by=created_at"):
            yield cast(Note, item)

    def discussions(self) -> Iterator[Discussion]:
        for item in _pages(f"{self.api}/discussions?"):
            yield cast(Discussion, item)

    def award_emoji(self, note_id: int) -> list[AwardEmoji]:
        url = f"{self.api}/notes/{note_id}/award_emoji?"
        return [cast(AwardEmoji, item) for item in _pages(url)]

    def create_note(self, body: str) -> Note:
        return cast(Note, self._write("POST", "/notes", {"body": inert(body)}))

    def update_note(self, note_id: int, body: str) -> None:
        _ = self._write("PUT", f"/notes/{note_id}", {"body": inert(body)})

    def create_discussion(self, body: str, position: Position) -> None:
        _ = self._write(
            "POST", "/discussions", {"body": inert(body), "position": position}
        )


def commit_log(commits: list[Commit], since: str | None = None) -> str:
    """`git log --oneline --no-merges` of `commits` (newest first), cut at `since` if present.

    Without `since` among them (no previous review, or it was force-pushed away) all commits
    are listed. The list replaces `git log` because the job's clone has no MR history.
    """
    lines: list[str] = []
    for commit in commits:
        if commit["id"] == since:
            break
        if len(commit.get("parent_ids", [])) <= 1:
            lines.append(f"{commit['short_id']} {commit['title']}\n")
    return "".join(lines)


def _now() -> datetime:
    return datetime.now(UTC)


def _error_note(mr: MergeRequest, job: Job) -> tuple[Note, str, int] | None:
    """The bot's newest error note of `job` on `mr`, with the head and attempts it records."""
    bot = bot_user_id()
    for note in mr.notes():
        if note["system"] or note["author"]["id"] != bot:
            continue
        # Как и у маркеров ревью, состояние — только маркер в первой строке.
        match = markers.JOB_ERROR.match(note["body"])
        if match and match.group(1) == job:
            return note, match.group(2), int(match.group(3))
    return None


def _retry_delay(attempts: int) -> timedelta:
    return min(ERROR_BACKOFF * (1 << min(attempts - 1, 32)), MAX_ERROR_BACKOFF)


def _backing_off(
    mr: MergeRequest, found: tuple[Note, str, int] | None
) -> tuple[str, int, datetime] | None:
    """(head, attempts, retry time) if the job failed on the current head of `mr` (its
    error note `found`) and its retry delay has not passed; None otherwise."""
    if found is None:
        return None
    refs = mr.get()["diff_refs"]
    if refs is None:
        return None
    note, head, attempts = found
    if head != refs["head_sha"]:
        return None
    retry_at = datetime.fromisoformat(note["updated_at"]) + _retry_delay(attempts)
    if _now() >= retry_at:
        return None
    print(
        f"Failed {attempts} time(s) on {head[:8]}, next attempt after {retry_at:%H:%M} UTC."
    )
    return head, attempts, retry_at


def _record_error(mr: MergeRequest, job: Job, error: Exception) -> int | None:
    """Count a failure of `job` on the current head of `mr` in its error note; returns
    the failures in a row on that head."""
    refs = mr.get()["diff_refs"]
    if refs is None:
        return None
    head = refs["head_sha"]
    found = _error_note(mr, job)
    attempts = found[2] + 1 if found and found[1] == head else 1
    # Текст ошибки — только в лог джобы: вывод git и HTTP-клиента в заметке MR ни к чему.
    body = "\n".join(
        (
            markers.job_error(job, head, attempts),
            f"🤖 **AI {job}** of `{head[:8]}` failed ({type(error).__name__}), "
            + f"{attempts} run(s) in a row; see the job log. Next attempt in "
            + f"{_retry_delay(attempts).total_seconds() / 60:.0f} min or after the next push.",
        )
    )
    if found is None:
        _ = mr.create_note(body)
    else:
        mr.update_note(found[0]["id"], body)
    return attempts


def _clear_error(mr: MergeRequest, job: Job, found: tuple[Note, str, int]) -> None:
    """Close the error note `found` of `job` on `mr` after a run that did not fail."""
    note, head, attempts = found
    # Без маркера заметка больше не состояние: пауза снята, следующий сбой заведёт новую.
    body = (
        f"🤖 **AI {job}** of `{head[:8]}` failed {attempts} run(s) in a row; "
        + "a later run succeeded."
    )
    mr.update_note(note["id"], body)


class Outcome(Enum):
    SKIPPED = "skipped"  # nothing new, no LLM call; does not count toward the run limit
    HANDLED = "handled"
    FAILED = "failed"


# Что случилось с MR в запуске: атрибут его спана. Outcome — только для цикла.
Status = Literal[
    "reviewed",
    "described",
    "skipped",  # nothing new for the LLM; `reason` says why
    "head_moved",  # a push came in while the LLM was answering; nothing written
    "rejected",  # the LLM rejected the prompt; not retried until the next push
    "error",  # any other failure; retried with a growing delay
    "backing_off",  # an earlier error on this head, its retry delay has not passed
]


@dataclass
class Result:
    """What a job did with one MR: `outcome` drives the loop, the rest goes to its span."""

    outcome: Outcome
    status: Status
    reason: str | None = None
    head: str | None = None
    # Только ревью: находки inline и ушедшие в итоговую заметку (вне позиции, 400, сбой).
    inline: int | None = None
    in_summary: int | None = None
    # От самого раннего ещё не обработанного пуша до записи результата в MR.
    latency_s: float | None = None
    # Сбоев подряд на этом HEAD.
    attempt: int | None = None


def skipped(reason: str) -> Result:
    print(reason)
    return Result(Outcome.SKIPPED, "skipped", reason)


def latency(since: datetime | None) -> float | None:
    """Seconds from `since` (a push, `MergeRequest.waiting_since`) until now."""
    return round((_now() - since).total_seconds(), 1) if since else None


def _span_attributes(result: Result) -> dict[str, telemetry.Attribute]:
    attrs: dict[str, telemetry.Attribute | None] = {
        "mr_bot.outcome": result.outcome.value,
        "mr_bot.status": result.status,
        "mr_bot.reason": result.reason,
        "mr_bot.head": result.head,
        "mr_bot.inline": result.inline,
        "mr_bot.in_summary": result.in_summary,
        "mr_bot.latency_s": result.latency_s,
        "mr_bot.attempt": result.attempt,
    }
    return {key: value for key, value in attrs.items() if value is not None}


def for_each_open_mr(
    job: Job, handle: Callable[[MergeRequest], Result], limit: int = MAX_MRS_PER_RUN
) -> int:
    """Run `handle` on the open MRs labelled `OPT_IN_LABEL`, most recently updated first.

    Returns the job's exit code.

    Stops after `limit` MRs that were not skipped, so one run stays within the job timeout
    and a backlog is worked off over several runs. A failure on one MR is reported, counted
    in an error note and retried with a growing delay (skipped until then); the rest are
    still handled. The first run on the MR that does not fail closes the note. `MR_IID`
    handles that one MR only, labelled or not (local runs), and ignores the delay. Every
    handled or skipped MR gets a span; the MRs left for the next run are counted in
    `mr_bot.deferred` on the run's span.
    """
    with telemetry.span(f"mr-{job}", {"mr_bot.job": job}):
        status = _for_each_open_mr(job, handle, limit)
        if status:
            telemetry.fail("an MR failed")
        return status


def _for_each_open_mr(
    job: Job, handle: Callable[[MergeRequest], Result], limit: int
) -> int:
    only = os.environ.get("MR_IID")
    iids = [int(only)] if only else open_merge_requests(OPT_IN_LABEL)
    status = 0
    handled = 0
    for index, iid in enumerate(iids):
        if handled >= limit:
            print(f"Run limit of {limit} MRs reached, the rest wait for the next run.")
            telemetry.set_attributes({"mr_bot.deferred": len(iids) - index})
            break
        print(f"=== !{iid}")
        with telemetry.span(f"mr-{job} !{iid}", {"gitlab.mr.iid": iid}):
            result = _handle_one(MergeRequest(iid), job, handle, bool(only))
            telemetry.set_attributes(_span_attributes(result))
            if result.outcome is Outcome.FAILED:
                telemetry.fail("handling the MR failed")
        if result.outcome is not Outcome.SKIPPED:
            handled += 1
        if result.outcome is Outcome.FAILED:
            status = 1
    return status


def _handle_one(
    mr: MergeRequest, job: Job, handle: Callable[[MergeRequest], Result], only: bool
) -> Result:
    try:
        found = _error_note(mr, job)
        if not only and (waiting := _backing_off(mr, found)):
            head, attempts, retry_at = waiting
            reason = f"next attempt after {retry_at.isoformat()}"
            return Result(
                Outcome.SKIPPED, "backing_off", reason, head, attempt=attempts
            )
        result = handle(mr)
        # FAILED без исключения (отказ LLM, оборванные inline) — тоже сбой: «успех» в заметке
        # был бы неправдой. Свою паузу такой исход ведёт сам, через маркер отказа.
        if found is not None and result.outcome is not Outcome.FAILED:
            try:
                _clear_error(mr, job, found)
            except Exception as clear_error:
                print(f"!{mr.iid}: could not clear the failure: {clear_error}")
        return result
    except Exception as error:
        print(f"!{mr.iid} failed: {type(error).__name__}: {error}")
        attempt: int | None = None
        try:
            attempt = _record_error(mr, job, error)
        except Exception as record_error:
            print(f"!{mr.iid}: could not record the failure: {record_error}")
        # Как и в логе джобы; в заметку MR текст ошибки не попадает.
        reason = f"{type(error).__name__}: {error}"[:300]
        return Result(Outcome.FAILED, "error", reason, attempt=attempt)
