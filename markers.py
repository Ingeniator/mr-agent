"""Hidden HTML-comment markers the jobs leave in MRs: their only state between runs.

The markers live in MR descriptions and notes, so they outlive any version of these
scripts. Change them expand/contract, like any shared state (root CLAUDE.md): a new format
must still read the old ones, or every opted-in MR is described and reviewed again from scratch.
"""

import re

_SHA = r"[0-9a-f]{40}"

# Блок описания между началом и концом. Начало без head= — от первой версии джобы.
DESCRIPTION_START = re.compile(rf"<!-- ai-description:start(?: head=({_SHA}))? -->")
DESCRIPTION_END = "<!-- ai-description:end -->"
# Итоговая заметка ревью: отревьюенный HEAD — база следующего инкрементального ревью.
REVIEWED = re.compile(rf"<!-- ai-review head=({_SHA}) -->")
# Отказ LLM по этому HEAD: не повторять до следующего пуша. Базу ревью не сдвигает.
REVIEW_FAILED = re.compile(rf"<!-- ai-review-failed head=({_SHA}) -->")
# Прочие сбои джобы на этом HEAD (не отказ LLM): сколько запусков подряд упали — от этого
# пауза до следующей попытки. Одна заметка на джобу, правится на месте.
JOB_ERROR = re.compile(
    rf"<!-- ai-(description|review)-error head=({_SHA}) attempts=([1-9][0-9]*) -->"
)
# Inline-находка ревью: версия промпта и модель для mr_review_stats.py. Не состояние джобы.
FINDING = re.compile(
    rf"<!-- ai-finding head=({_SHA}) severity=(\w+) prompt=(\w+) model=([\w./:@-]+) -->"
)


_ANY = re.compile(
    "|".join(
        (
            DESCRIPTION_START.pattern,
            re.escape(DESCRIPTION_END),
            REVIEWED.pattern,
            REVIEW_FAILED.pattern,
            JOB_ERROR.pattern,
            FINDING.pattern,
        )
    )
)


def strip(text: str) -> str:
    """`text` without any marker, so text the LLM echoes from a diff cannot pose as job state."""
    # До неподвижной точки: вырезанный маркер мог склеить из остатков новый.
    while (stripped := _ANY.sub("", text)) != text:
        text = stripped
    return text


def description_start(head: str) -> str:
    return f"<!-- ai-description:start head={head} -->"


def reviewed(head: str) -> str:
    return f"<!-- ai-review head={head} -->"


def review_failed(head: str) -> str:
    return f"<!-- ai-review-failed head={head} -->"


def job_error(job: str, head: str, attempts: int) -> str:
    return f"<!-- ai-{job}-error head={head} attempts={attempts} -->"


def finding(head: str, severity: str, prompt: str, model: str) -> str:
    return f"<!-- ai-finding head={head} severity={severity} prompt={prompt} model={model} -->"
