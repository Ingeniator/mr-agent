"""The `mr-bot-health` job: fails when the review has silently stopped.

A red scheduled pipeline notifies only the schedule's owner, and a schedule that stopped
running is not red at all. This job, on its own daily schedule, turns both into a failure
anyone can see: labelled MRs left unreviewed, and `MR_BOT_TOKEN` close to expiry.

Behaviour and setup: README.md next to this file.
"""

import sys
import urllib.error
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from gitlab_api import OPT_IN_LABEL, MergeRequest, open_merge_requests, token_expires_on
from mr_review import review_markers

# Обычно ревью приходит через один-два интервала расписания; столько ждать — уже поломка.
STALE_AFTER = timedelta(hours=4)
# Продлить project access token может только Maintainer — нужно время, чтобы его найти.
TOKEN_WARN_DAYS = 14


@dataclass
class Waiting:
    iid: int
    head: str
    since: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def waiting(mr: MergeRequest) -> Waiting | None:
    """`mr` if its current head has neither a review nor a recorded LLM rejection."""
    refs = mr.get()["diff_refs"]
    if refs is None:
        return None
    head = refs["head_sha"]
    found = review_markers(mr)
    if head in (found.reviewed, found.failed):
        return None
    # Без даты версии считаем, что HEAD запушен только что: лучше пропустить, чем ложная тревога.
    return Waiting(mr.iid, head, mr.head_pushed_at() or _now())


def token_problem(today: date) -> str | None:
    try:
        expires = token_expires_on()
    except urllib.error.HTTPError as error:
        # GitLab older than 15.5 has no /personal_access_tokens/self.
        if error.code == 404:
            print("Cannot read the token's expiry date on this GitLab, not checked.")
            return None
        raise
    if expires is None:
        print("MR_BOT_TOKEN does not expire.")
        return None
    left = (expires - today).days
    print(f"MR_BOT_TOKEN expires on {expires} ({left} days left).")
    if left <= TOKEN_WARN_DAYS:
        return f"MR_BOT_TOKEN expires on {expires}: rotate it (README.md, 'Rotating MR_BOT_TOKEN')."
    return None


def main() -> int:
    now = _now()
    problems: list[str] = []
    if problem := token_problem(now.date()):
        problems.append(problem)

    iids = open_merge_requests(OPT_IN_LABEL)
    print(f"{len(iids)} open MR(s) labelled {OPT_IN_LABEL}.")
    stale: list[Waiting] = []
    for iid in iids:
        try:
            found = waiting(MergeRequest(iid))
        except Exception as error:
            problems.append(
                f"!{iid} could not be checked: {type(error).__name__}: {error}"
            )
            continue
        if found is None:
            print(f"!{iid}: current head reviewed.")
            continue
        age = now - found.since
        print(
            f"!{iid}: {found.head[:8]} waits for review for {age.total_seconds() / 3600:.1f} h."
        )
        if age > STALE_AFTER:
            stale.append(found)
    if stale:
        listed = ", ".join(f"!{w.iid}" for w in stale)
        problems.append(
            f"{len(stale)} labelled MR(s) unreviewed for over {STALE_AFTER.total_seconds() / 3600:.0f} h: "
            + f"{listed}. Follow README.md, 'When mr-bot-health fails'."
        )

    for problem in problems:
        print(f"PROBLEM: {problem}")
    if not problems:
        print("The MR bots are healthy.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
