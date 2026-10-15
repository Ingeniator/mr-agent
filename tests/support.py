"""Test fixtures: a git server shaped like GitLab, a fake GitLab REST API and a fake LLM."""

# Атрибуты TestCase задаёт setUp, а не __init__.
# pyright: reportUninitializedInstanceVariable=false

import os
import subprocess
import tempfile
import unittest
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

import gitlab_api
import telemetry
from gitlab_api import (
    OPT_IN_LABEL,
    AwardEmoji,
    Commit,
    DiffRefs,
    Discussion,
    DiscussionNote,
    MergeRequest,
    Note,
    Position,
    User,
    Version,
)

BOT = 1
IID = 7


def sh(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def recorded_spans() -> list[tuple[str, dict[str, object]]]:
    """Names and attributes of the spans recorded so far, in the order they ended."""
    return [
        (
            span["name"],
            {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]},
        )
        for span in telemetry._spans  # pyright: ignore[reportPrivateUsage]
    ]


def text(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


class Server:
    """The project repository as GitLab holds it: branches plus `refs/merge-requests/<iid>/head`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir()
        sh(path, "init", "-q", "-b", "main")
        # GitLab отдаёт коммиты старых версий MR по SHA (keep-around) — сервер тоже.
        sh(path, "config", "uploadpack.allowAnySHA1InWant", "true")

    def git(self, *args: str) -> str:
        return sh(self.path, *args)

    def commit(self, message: str, files: dict[str, str | None]) -> str:
        """Commit on the current branch; a `None` content deletes the file."""
        for name, content in files.items():
            if content is None:
                self.git("rm", "-q", name)
                continue
            file = self.path / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
            self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def checkout(self, branch: str, new: bool = False) -> None:
        self.git("checkout", "-q", *(["-b"] if new else []), branch)

    def publish_mr(self, branch: str = "mr") -> DiffRefs:
        """Point the MR ref at `branch` and return the diff refs GitLab would report."""
        head = self.git("rev-parse", branch)
        self.git("update-ref", f"refs/merge-requests/{IID}/head", head)
        return DiffRefs(
            base_sha=self.git("merge-base", "main", branch),
            start_sha=self.git("rev-parse", "main"),
            head_sha=head,
        )

    def commits(self, refs: DiffRefs) -> list[Commit]:
        """The MR commits GitLab lists for `refs`: `base..head`, newest first."""
        log = self.git(
            "log",
            "--format=%H%x00%h%x00%s%x00%P",
            f"{refs['base_sha']}..{refs['head_sha']}",
        )
        commits: list[Commit] = []
        for line in log.splitlines():
            sha, short, title, parents = line.split("\0")
            commits.append(
                Commit(id=sha, short_id=short, title=title, parent_ids=parents.split())
            )
        return commits


class FakeGitLab:
    """The REST API of project 1 with the single open MR !7, as much as the jobs use."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.description: str = ""
        self.labels: list[str] = [OPT_IN_LABEL]
        self.refs: DiffRefs | None = None
        self.commits: list[Commit] = []  # newest first
        self.versions: list[Version] = []  # newest first
        self.version_reads: int = 0  # первые страницы GET /versions
        self.notes: list[Note] = []  # oldest first
        self.discussions: list[tuple[str, Position]] = []
        # Те же inline-треды, как их отдаёт GET /discussions: с ответами и статусом.
        self.threads: list[Discussion] = []
        self.emoji: dict[int, list[AwardEmoji]] = {}
        self.state: str = "opened"
        # Ошибки, которыми ответят следующие POST /discussions (None — принять).
        self.discussion_errors: list[Exception | None] = []
        # Часы GitLab: updated_at заметок и `gitlab_api._now` джобы.
        self.now: datetime = datetime(2026, 1, 1, tzinfo=UTC)
        # expires_at токена бота (GET /personal_access_tokens/self); None — бессрочный.
        self.token_expires: str | None = None

    def add_note(self, body: str, author: int = BOT) -> Note:
        note = Note(
            id=len(self.notes) + 1,
            body=body,
            system=False,
            author=User(id=author),
            updated_at=self.now.isoformat().replace("+00:00", "Z"),
        )
        self.notes.append(note)
        return note

    def reply(self, discussion: Discussion, body: str, author: int) -> None:
        discussion["notes"].append(
            DiscussionNote(
                id=10_000 + sum(len(d["notes"]) for d in self.threads),
                body=body,
                system=False,
                author=User(id=author),
            )
        )

    def request(
        self, method: str, url: str, headers: dict[str, str], body: object = None
    ) -> object:
        if os.environ.get("DRY_RUN") == "1" and method != "GET":
            raise AssertionError(f"write in dry run: {method} {url}")
        parts = urllib.parse.urlsplit(url)
        path = parts.path.removeprefix("/api/v4")
        query = urllib.parse.parse_qs(parts.query)
        payload = cast(dict[str, object], body or {})
        mr = f"/projects/1/merge_requests/{IID}"
        if (method, path) == ("GET", "/user"):
            return {"id": BOT}
        if (method, path) == ("GET", "/personal_access_tokens/self"):
            return {"expires_at": self.token_expires}
        if (method, path) == ("GET", "/projects/1/merge_requests"):
            wanted = set(query.get("labels", [""])[0].split(",")) - {""}
            listed = query["page"] == ["1"] and wanted <= set(self.labels)
            state = query.get("state", ["opened"])[0]
            listed = listed and state in ("all", self.state)
            return [{"iid": IID, "state": self.state}] if listed else []
        if (method, path) == ("GET", mr):
            return {"description": self.description, "diff_refs": self.refs}
        if (method, path) == ("PUT", mr):
            self.description = str(payload["description"])
            return {}
        if (method, path) == ("GET", f"{mr}/versions"):
            if query["page"] != ["1"]:
                return []
            self.version_reads += 1
            return self.versions
        if (method, path) == ("GET", f"{mr}/commits"):
            return self.commits if query["page"] == ["1"] else []
        if (method, path) == ("GET", f"{mr}/notes"):
            return list(reversed(self.notes)) if query["page"] == ["1"] else []
        if (method, path) == ("POST", f"{mr}/notes"):
            return self.add_note(str(payload["body"]))
        if method == "PUT" and path.startswith(f"{mr}/notes/"):
            note_id = int(path.rsplit("/", 1)[1])
            note = next(n for n in self.notes if n["id"] == note_id)
            note["body"] = str(payload["body"])
            note["updated_at"] = self.now.isoformat().replace("+00:00", "Z")
            return {}
        if (method, path) == ("POST", f"{mr}/discussions"):
            error = self.discussion_errors.pop(0) if self.discussion_errors else None
            if error is not None:
                raise error
            position = cast(Position, payload["position"])
            self.discussions.append((str(payload["body"]), position))
            discussion = Discussion(id=str(len(self.threads) + 1), notes=[])
            self.threads.append(discussion)
            self.reply(discussion, str(payload["body"]), BOT)
            discussion["notes"][0]["resolved"] = False
            discussion["notes"][0]["position"] = position
            return {}
        if (method, path) == ("GET", f"{mr}/discussions"):
            # Как в GitLab: обычные заметки идут отдельными обсуждениями без позиции.
            single = [
                Discussion(
                    id=f"note-{n['id']}",
                    notes=[
                        DiscussionNote(
                            id=n["id"],
                            body=n["body"],
                            system=n["system"],
                            author=n["author"],
                        )
                    ],
                )
                for n in self.notes
            ]
            return self.threads + single if query["page"] == ["1"] else []
        if method == "GET" and path.startswith(f"{mr}/notes/"):
            note_id = int(path.split("/")[-2])
            return self.emoji.get(note_id, []) if query["page"] == ["1"] else []
        raise AssertionError(f"unexpected request: {method} {url}")


class FakeLLM:
    """Records prompts; answers with `answer`, or raises it when it is an exception."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.json_object: list[bool] = []
        self.answer: str | Exception = '{"summary": "Looks fine.", "findings": []}'

    def __call__(self, prompt: str, *, json_object: bool = False) -> str:
        self.prompts.append(prompt)
        self.json_object.append(json_object)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class JobTestCase(unittest.TestCase):
    """A scheduled run against `FakeGitLab`, with a git server and a fresh clone per run."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # Без коллектора спаны копятся до конца процесса: каждый тест видит только свои.
        telemetry._spans.clear()  # pyright: ignore[reportPrivateUsage]
        environ = mock.patch.dict(
            os.environ,
            {
                "CI_API_V4_URL": "https://gitlab.test/api/v4",
                "CI_PROJECT_ID": "1",
                "MR_BOT_TOKEN": "token",
                "LLM_MODEL": "gpt-4o",
                # Глобальный и системный git config разработчика (gpgsign, hooks) не влияют.
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
        )
        environ.start()
        self.addCleanup(environ.stop)
        for name in ("DRY_RUN", "MR_IID", "GIT_REMOTE", "MR_BOT_USER_ID", "CI_JOB_ID"):
            os.environ.pop(name, None)
        self.addCleanup(os.chdir, os.getcwd())

        self.server = Server(self.tmp / "server")
        self.gitlab = FakeGitLab(self.server)
        for patcher in (
            mock.patch("gitlab_api.request", self.gitlab.request),
            mock.patch("gitlab_api._now", lambda: self.gitlab.now),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        gitlab_api.bot_user_id.cache_clear()

        self.llm = FakeLLM()
        self._clones = 0

    def fresh_clone(self) -> None:
        """Clone the tip of the default branch into a new directory, as every scheduled job starts."""
        self._clones += 1
        path = self.tmp / f"job{self._clones}"
        # file:// — иначе локальный clone игнорирует --depth; GIT_DEPTH=1 в CI.
        url = self.server.path.as_uri()
        sh(self.tmp, "clone", "-q", "--depth=1", "-b", "main", url, str(path))
        os.chdir(path)

    def publish(self, branch: str = "mr") -> DiffRefs:
        refs = self.server.publish_mr(branch)
        self.gitlab.refs = refs
        self.gitlab.commits = self.server.commits(refs)
        self.gitlab.versions.insert(
            0,
            Version(
                head_commit_sha=refs["head_sha"],
                base_commit_sha=refs["base_sha"],
                start_commit_sha=refs["start_sha"],
                created_at=self.gitlab.now.isoformat().replace("+00:00", "Z"),
            ),
        )
        return refs

    def mr(self) -> MergeRequest:
        return MergeRequest(IID)
