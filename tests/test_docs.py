"""README.md and overview.html against the code: names that exist, links that resolve, no copied values.

The docs name constants, functions and files instead of restating them; this keeps the
names honest. ADRs are left out: they record a decision as it was and are not rewritten.
"""

import importlib
import re
import unittest
from pathlib import Path
from typing import cast

HERE = Path(__file__).resolve().parents[1]
DOCS = (HERE / "README.md", HERE / "overview.html")
MODULES = sorted(path for path in HERE.glob("*.py"))
CI = HERE / "gitlab-ci.example.yml"
# Файлы проекта, который ревьюят, а не этого репозитория.
PROJECT_FILES = frozenset({"CLAUDE.md", ".gitlab-ci.yml"})

# UPPER_CASE с подчёркиванием: константы модулей и переменные окружения/CI.
_CONSTANT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\(\)")
_FILE = re.compile(r"(?<![\w./-])([\w.][\w./-]*\.(?:py|md|html|yml))\b")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HREF = re.compile(r'href="([^"]+)"')
_HTML_ID = re.compile(r'\bid="([^"]+)"')
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)
_JS_STRING = re.compile(r"'((?:[^'\\\n]|\\.)*)'")


def _find(pattern: re.Pattern[str], text: str) -> list[str]:
    """`findall` of a pattern with at most one group: always strings."""
    return cast(list[str], pattern.findall(text))


def _code_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in MODULES)


def _prose(doc: Path, *, scripts: bool) -> str:
    """The doc's text; for HTML, the string literals of its scripts only when `scripts`.

    The page's own JS calls DOM methods (`remove()`), but its strings are the simulator's
    text and name the bots' functions and constants.
    """
    text = doc.read_text(encoding="utf-8")
    if doc.suffix != ".html":
        return text
    strings = [s for js in _find(_SCRIPT, text) for s in _find(_JS_STRING, js)]
    return "\n".join([_SCRIPT.sub("", text), *(strings if scripts else [])])


def _slug(heading: str) -> str:
    """GitLab's anchor for a Markdown heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return re.sub(r"-{2,}", "-", text.replace(" ", "-"))


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        return set(_find(_HTML_ID, text))
    text = _FENCE.sub("", text)
    return {_slug(line.lstrip("#")) for line in text.splitlines() if line.startswith("#")}


def _links(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        return _find(_HREF, text)
    return _find(_MD_LINK, _FENCE.sub("", text))


def _int_constants() -> dict[str, int]:
    values: dict[str, int] = {}
    for path in MODULES:
        module = importlib.import_module(path.stem)
        attributes: dict[str, object] = vars(module)
        for name, value in attributes.items():
            if _CONSTANT.fullmatch(name) and type(value) is int and value > 1:
                values[name] = value
    return values


def _spellings(value: int) -> set[str]:
    spellings = {str(value), f"{value:_}", f"{value:,}", f"{value:,}".replace(",", " ")}
    if value % 1000 == 0:
        spellings.add(f"{value // 1000}k")
    return spellings


class DocsMatchCode(unittest.TestCase):
    def test_constants_exist(self) -> None:
        known = set(_find(_CONSTANT, _code_text() + CI.read_text(encoding="utf-8")))
        for doc in DOCS:
            missing = set(_find(_CONSTANT, _prose(doc, scripts=True))) - known
            self.assertEqual(missing, set(), f"{doc.name} names what no script or CI job has")

    def test_functions_exist(self) -> None:
        defined = set(_find(re.compile(r"\bdef ([a-z_][a-z0-9_]*)\("), _code_text()))
        for doc in DOCS:
            missing = set(_find(_CALL, _prose(doc, scripts=True))) - defined
            self.assertEqual(missing, set(), f"{doc.name} names functions nobody defines")

    def test_files_exist(self) -> None:
        for doc in DOCS:
            # Строки скриптов не проверяются: демо-дифф правит выдуманный config.py.
            for name in set(_find(_FILE, _prose(doc, scripts=False))) - PROJECT_FILES:
                with self.subTest(doc=doc.name, file=name):
                    # Имя файла в тексте — от корня репозитория или его tests/.
                    bases = (HERE, HERE / "tests")
                    found = [base / name for base in bases if (base / name).exists()]
                    self.assertTrue(found, f"{doc.name} names a missing file")

    def test_links_resolve(self) -> None:
        for doc in DOCS:
            for link in _links(doc):
                if re.match(r"[a-z]+:", link):
                    continue
                with self.subTest(doc=doc.name, link=link):
                    target, _, anchor = link.partition("#")
                    path = (doc.parent / target).resolve() if target else doc
                    self.assertTrue(path.exists(), "missing file")
                    if anchor and path.suffix in (".md", ".html"):
                        self.assertIn(anchor, _anchors(path), "missing anchor")

    def test_values_are_not_copied(self) -> None:
        constants = _int_constants()
        for doc in DOCS:
            for number, line in enumerate(_prose(doc, scripts=True).splitlines(), 1):
                for name in set(_find(_CONSTANT, line)) & constants.keys():
                    copies = {
                        spelling
                        for spelling in _spellings(constants[name])
                        if re.search(rf"(?<![\w.]){re.escape(spelling)}(?![\w.])", line)
                    }
                    with self.subTest(doc=doc.name, line=number, constant=name):
                        self.assertEqual(copies, set(), "name the constant, not its value")


class Helpers(unittest.TestCase):
    def test_slug_matches_gitlab(self) -> None:
        self.assertEqual(_slug(" Where to read what"), "where-to-read-what")
        self.assertEqual(_slug("Why polling, not webhooks"), "why-polling-not-webhooks")

    def test_drift_is_caught(self) -> None:
        self.assertEqual(_find(_CONSTANT, "`MAX_MRS_PER_RUN` and `GIT_CONFIG_*`"), ["MAX_MRS_PER_RUN"])
        self.assertEqual(_find(_CALL, "`mr.get()`, `chat(PROMPT)`"), ["get"])
        self.assertEqual(_find(_FILE, "`.gitlab-ci.yml` and tests/test_docs.py."), [".gitlab-ci.yml", "tests/test_docs.py"])


if __name__ == "__main__":
    _ = unittest.main()
