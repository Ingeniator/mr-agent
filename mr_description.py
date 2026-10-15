"""The `mr-description` job: an LLM summary block in open MRs labelled `ai-review`.

Behaviour, setup and local runs: README.md next to this file.
"""

import sys

import markers
from common import truncate
from gitlab_api import (
    MergeRequest,
    Outcome,
    Result,
    Status,
    commit_log,
    for_each_open_mr,
    inert,
    latency,
    skipped,
)
from llm import LLMRejected, chat
from markers import DESCRIPTION_END as END
from markers import DESCRIPTION_START as START
from markers import description_start
from project import PROJECT
from repo import fetch_commits, git

PROMPT = """You write GitLab merge request descriptions.
{project}

Write the description in Markdown with exactly these sections:
## What — what changed, grouped by component; a short bullet list.
## Why — the motivation, only as far as commits and diff show it.
## Compatibility — which shared state the change touches (the state named above, if any;
APIs, data formats, config keys, CLI contracts) and whether the previous release still works
with it. Write "None" if nothing.
## How to test — commands or steps; mention tests added or changed in the diff.

Be factual and concise. Do not invent anything the diff does not show.
Answer with the description only, no preamble.

Commits:
{commits}

Diff stat:
{stat}

Diff:
{diff}
"""


def described_head(description: str) -> str | None:
    match = START.search(description)
    return match.group(1) if match else None


FAILED_PREFIX = "_AI description of `"


def _block_span(description: str) -> tuple[int, int, int] | None:
    """(block start, text start, end marker start) of the ai-description block, if paired."""
    start = START.search(description)
    end = description.find(END, start.end()) if start else -1
    return (start.start(), start.end(), end) if start and end != -1 else None


def block_text(description: str) -> str:
    """The summary inside the block without earlier failure notes; empty when there is none."""
    span = _block_span(description)
    if span is None:
        return ""
    lines = description[span[1] : span[2]].strip().split("\n")
    return "\n".join(l for l in lines if not l.startswith(FAILED_PREFIX)).strip()


def failed_summary(head: str, error: Exception, previous: str) -> str:
    note = f"{FAILED_PREFIX}{head[:8]}` failed: {error}. It is retried after the next push._"
    if not previous:
        return note
    # Прежнее описание полезнее пустого места: оставляем его, пометив, что оно не про этот HEAD.
    return f"{note}\n_The text below describes an earlier version of the MR._\n\n{previous}"


def merge_block(current: str, summary: str, head: str) -> str:
    # Прежний текст блока (failed_summary) мог править человек. inert — только для блока:
    # иначе каждый запуск ломал бы @упоминания и строки на «/» в тексте автора.
    summary = inert(markers.strip(summary))
    block = f"{description_start(head)}\n{summary}\n{END}"
    span = _block_span(current)
    if span is not None:
        return current[: span[0]] + block + current[span[2] + len(END) :]
    # Без пары маркеров убираем остатки, иначе следующий запуск снова не найдёт пару.
    rest = START.sub("", current).replace(END, "")
    return f"{rest.rstrip()}\n\n{block}".strip()


def describe(mr: MergeRequest) -> Result:
    data = mr.get()
    refs = data["diff_refs"]
    if refs is None:
        return skipped("No diff refs yet, skipping.")
    head = refs["head_sha"]
    described = described_head(data["description"] or "")
    if described == head:
        return skipped(f"{head[:8]} is already described.")
    base = refs["base_sha"]
    fetch_commits(base, head)
    diff = git("diff", "--no-color", "--no-ext-diff", base, head)
    if not diff.strip():
        return skipped("Empty diff, nothing to describe.")
    waiting = mr.waiting_since(described)

    summary = ""
    rejected: LLMRejected | None = None
    try:
        summary = chat(
            PROMPT.format(
                project=PROJECT,
                commits=commit_log(mr.commits()),
                stat=git("diff", "--stat", base, head),
                diff=truncate(diff),
            )
        )
    except LLMRejected as error:
        # Блок с этим HEAD пишется и при отказе, чтобы не слать тот же промпт каждый запуск.
        print(f"!{mr.iid}: {error}")
        rejected = error
    outcome = Outcome.HANDLED if rejected is None else Outcome.FAILED

    # Пока шёл запрос к LLM, в MR могли запушить или поправить описание.
    current = mr.get()
    refs = current["diff_refs"]
    if refs is None or refs["head_sha"] != head:
        print(
            "MR head moved on during generation, skipping: the next run describes it."
        )
        return Result(outcome, "head_moved", head=head)
    description = current["description"] or ""
    if rejected is not None:
        summary = failed_summary(head, rejected, block_text(description))
    mr.update_description(merge_block(description, summary, head))
    print(f"Updated description of !{mr.iid}:\n\n{summary}")
    status: Status = "described" if rejected is None else "rejected"
    reason = None if rejected is None else str(rejected)
    return Result(outcome, status, reason, head, latency_s=latency(waiting))


if __name__ == "__main__":
    sys.exit(for_each_open_mr("description", describe))
