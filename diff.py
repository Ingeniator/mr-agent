"""Unified diff parsing: line numbers for the prompt and commentable lines for GitLab."""

import re
from dataclasses import dataclass, field


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    # Строки, к которым GitLab принимает inline-комментарий: новый номер → старый номер
    # (None для добавленной строки). У неизменённой строки нужны оба номера, иначе 400.
    lines: dict[int, int | None] = field(default_factory=dict)
    # Старый номер удалённой строки → её текст.
    removed: dict[int, str] = field(default_factory=dict)
    # (новый номер, старый номер) сразу после каждого ханка: сдвиг для строк между ханками.
    hunk_ends: list[tuple[int, int]] = field(default_factory=list)

    def added(self) -> set[int]:
        return {new for new, old in self.lines.items() if old is None}

    def old_line(self, new: int) -> int | None:
        """The old-side number of new-side line `new`; None if the diff added it."""
        if new in self.lines:
            return self.lines[new]
        shift = 0
        for new_end, old_end in self.hunk_ends:
            if new_end > new:
                break
            shift = new_end - old_end
        return new - shift


@dataclass
class Fresh:
    """What changed since the previous review, in terms of the MR diff."""

    # (path, номер строки в новом файле)
    added: set[tuple[str, int]] = field(default_factory=set)
    # path → старые номера (по базе диффа MR) строк, удалённых после прошлого ревью.
    removed: dict[str, set[int]] = field(default_factory=dict)
    # Строки, которые MR добавил, а после прошлого ревью удалил: в диффе MR их нет совсем.
    dropped: dict[str, list[str]] = field(default_factory=dict)

    def paths(self) -> set[str]:
        return {path for path, _ in self.added} | set(self.removed) | set(self.dropped)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _first_line(start: str, count: str | None) -> int:
    # У пустой стороны ханка (`-5,0`) номер — строка перед ним, а не первая строка ханка.
    return int(start) + 1 if count == "0" else int(start)


def diff_path(raw: str, prefix: str) -> str:
    """The path from a `--- ` / `+++ ` diff header value, `/dev/null` kept as is."""
    # Git дописывает таб к именам с пробелами. C-экранированные имена не разбираем.
    return raw.rstrip("\t").removeprefix(prefix)


def parse_diff(
    diff: str, fresh: Fresh | None = None
) -> tuple[str, dict[str, FileDiff]]:
    """Number the new-side lines of a unified diff and collect the commentable lines per file.

    Returns the annotated diff for the prompt (`  42 + code` for added, `  42   code` for
    context, `     - code` for removed lines) and the files keyed by their new path (old
    path for deleted files). With `fresh`, its added lines are marked `*` instead of `+`,
    its removed lines `~` instead of `-`, and files outside `fresh.paths()` are left out of
    the annotated diff; the returned files stay complete.
    """
    # Блок на файл: (путь, строки). Пустой первый блок — на случай текста до первого заголовка.
    blocks: list[tuple[str, list[str]]] = [("", [])]
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    in_header = False
    old_path = ""
    old_no = new_no = 0
    # Не splitlines(): он режет и по \x0c, \x85, U+2028 и т.п., и номера строк уезжают.
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            current, in_header = None, True
            blocks.append(("", [line]))
            continue
        out = blocks[-1][1]
        if in_header and line.startswith("--- "):
            old_path = diff_path(line[4:], "a/")
            out.append(line)
            continue
        if in_header and line.startswith("+++ "):
            new_path = diff_path(line[4:], "b/")
            # Удалённый файл — по старому пути: у всех удалённых новый путь /dev/null.
            path = old_path if new_path == "/dev/null" else new_path
            current = FileDiff(
                old_path=new_path if old_path == "/dev/null" else old_path,
                new_path=new_path,
            )
            files[path] = current
            blocks[-1] = (path, out)
            out.append(line)
            continue
        hunk = _HUNK.match(line)
        if hunk:
            in_header = False
            old_no = _first_line(hunk.group(1), hunk.group(2))
            new_no = _first_line(hunk.group(3), hunk.group(4))
            if current:
                current.hunk_ends.append((new_no, old_no))
            out.append(line)
            continue
        if in_header or not line:
            out.append(line)
            continue
        tag, text = line[0], line[1:]
        if tag == "+":
            if current:
                current.lines[new_no] = None
            mark = (
                "*"
                if fresh is not None and (blocks[-1][0], new_no) in fresh.added
                else "+"
            )
            out.append(f"{new_no:>5} {mark} {text}")
            new_no += 1
        elif tag == " ":
            if current:
                current.lines[new_no] = old_no
            out.append(f"{new_no:>5}   {text}")
            old_no += 1
            new_no += 1
        elif tag == "-":
            if current:
                current.removed[old_no] = text
            mark = (
                "~"
                if fresh is not None
                and old_no in fresh.removed.get(blocks[-1][0], set())
                else "-"
            )
            out.append(f"{'':>5} {mark} {text}")
            old_no += 1
        else:
            out.append(line)
        if current and current.hunk_ends:
            current.hunk_ends[-1] = (new_no, old_no)
    fresh_paths = None if fresh is None else fresh.paths()
    kept = [
        line
        for path, lines in blocks
        if fresh_paths is None or path in fresh_paths
        for line in lines
    ]
    return "\n".join(kept), files
