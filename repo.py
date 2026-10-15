"""Git operations on the job's clone: running git and fetching MR commits by SHA.

CI clones only the tip of the default branch (`GIT_DEPTH: "1"` in `.gitlab-ci.yml`), so a
run that skips every MR costs one shallow checkout however long the history grows. For an
MR it handles, the job fetches just the commits it diffs, by SHA and without history: the
MR base and head, plus the last reviewed head and its base for a follow-up review. What a
shallow clone cannot compute comes from the GitLab API instead: the commit list in the
prompt (`gitlab_api.commit_log`) and the base of the last review (`mr_review.previous_base`).
A developer's full clone is fetched into without `--depth` and stays unshallow.
"""

import os
import subprocess


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        capture_output=True,
        # Дифф файла не в UTF-8 иначе падал бы на каждом запуске; битые байты в промпте безвредны.
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def remote() -> str:
    """The GitLab remote: `origin` in CI, `GIT_REMOTE` (name or URL) for local runs."""
    return os.environ.get("GIT_REMOTE") or "origin"


def has_commit(sha: str) -> bool:
    check = ["git", "cat-file", "-e", f"{sha}^{{commit}}"]
    return subprocess.run(check, capture_output=True).returncode == 0


def _depth() -> list[str]:
    # Diff двух коммитов история не нужна, но полный клон разработчика не делаем shallow.
    shallow = git("rev-parse", "--is-shallow-repository").strip() == "true"
    return ["--depth=1"] if shallow else []


def fetch_commits(*shas: str) -> None:
    """Fetch commits `shas` by SHA, without history in a shallow clone: enough to diff them."""
    missing = [sha for sha in shas if not has_commit(sha)]
    if missing:
        _ = git("fetch", "--quiet", *_depth(), remote(), *missing)


def fetch_commit(sha: str) -> bool:
    """Whether commit `sha` is in the clone, fetching it from the remote by SHA if needed."""
    # После force-push старый HEAD недостижим из веток, но GitLab отдаёт его по SHA (keep-around).
    if not has_commit(sha):
        fetch = ["git", "fetch", "--quiet", *_depth(), remote(), sha]
        _ = subprocess.run(fetch, capture_output=True)
    return has_commit(sha)
