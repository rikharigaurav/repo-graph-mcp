"""Rule-based file classification.

This replaces the per-folder Mistral call in Read-Code-Structure
(utils/llmCalls.get_file_type). The rules are the same ones that prompt spelled
out, applied deterministically — so classification costs microseconds instead
of an LLM round-trip per directory.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .graph import DOC_FILE, SOURCE_FILE, TEMPLATE_FILE, TEST_FILE

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "jspm_packages", "__pycache__",
    "venv", ".venv", "env", ".env", "virtualenv", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".ipynb_checkpoints", "site-packages", "dist", "build", "out", ".next", ".nuxt",
    ".svelte-kit", ".turbo", ".parcel-cache", ".cache", "coverage", ".nyc_output", "htmlcov",
    "target", "vendor", "Pods", ".gradle", ".idea", ".vscode", ".vs", ".terraform", ".serverless",
    ".expo", ".angular", ".docusaurus", "storybook-static", "_build", "deps", ".dart_tool",
    ".claude", ".github",
}

SOURCE_EXT = {
    "py", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "java", "kt", "kts", "scala",
    "go", "rs", "rb", "php", "cs", "fs", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "m", "mm", "swift",
    "dart", "vue", "svelte", "lua", "r", "jl", "ex", "exs", "erl", "clj", "hs", "ml", "sol", "zig",
    "sh", "bash", "ps1",
}
TEMPLATE_EXT = {
    "html", "htm", "xhtml", "xml", "jinja", "jinja2", "j2", "hbs", "handlebars", "mustache", "twig",
    "erb", "ejs", "pug", "jade", "njk", "liquid", "jsp", "cshtml", "razor", "astro",
}
DOC_EXT = {"md", "mdx", "rst", "adoc", "asciidoc", "org", "tex"}
DOC_NAMES = {
    "readme", "changelog", "changes", "history", "license", "licence", "contributing", "install",
    "guide", "manual", "authors", "notice", "code_of_conduct", "security", "faq", "todo",
}
PARSEABLE = {"py": "python", "pyi": "python", "js": "javascript", "jsx": "javascript",
             "mjs": "javascript", "cjs": "javascript", "ts": "typescript", "mts": "typescript",
             "cts": "typescript", "tsx": "tsx"}

_TEST_FILE_RE = re.compile(
    r"(^test_.*|.*_test\.[a-z]+$|.*_tests?\.py$|.*\.(test|spec|e2e|cy)\.[a-z]+$|^conftest\.py$|.*Test\.(java|kt|cs)$|.*_spec\.rb$)",
    re.IGNORECASE,
)
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "cypress", "testing", "__mocks__"}
_GENERATED_RE = re.compile(r"(\.min\.(js|css)$|\.bundle\.js$|\.chunk\.js$|-lock\.|\.lock$|\.map$|\.d\.ts$)")


def ext_of(path: str) -> str:
    name = PurePosixPath(path).name.lower()
    if name.endswith(".blade.php"):
        return "blade.php"
    return name.rsplit(".", 1)[-1] if "." in name else ""


def is_ignored_dir(name: str) -> bool:
    return name in IGNORED_DIRS or (name.startswith(".") and name not in {".", ".."})


def classify(rel_path: str) -> str | None:
    """Return a file label, or None when the file should be skipped
    (configs, lock files, images, binaries, generated code)."""
    p = PurePosixPath(rel_path)
    name = p.name
    lower = name.lower()
    ext = ext_of(rel_path)
    if _GENERATED_RE.search(lower):
        return None
    stem = lower.split(".", 1)[0]

    if ext in SOURCE_EXT:
        parts = {part.lower() for part in p.parts[:-1]}
        if _TEST_FILE_RE.match(name) or parts & _TEST_DIRS:
            return TEST_FILE
        return SOURCE_FILE
    if ext in TEMPLATE_EXT or ext == "blade.php":
        return TEMPLATE_FILE
    if ext in DOC_EXT or stem in DOC_NAMES:
        return DOC_FILE
    if ext == "txt" and ("docs" in {x.lower() for x in p.parts} or stem in DOC_NAMES):
        return DOC_FILE
    return None


def language_of(rel_path: str) -> str | None:
    return PARSEABLE.get(ext_of(rel_path))


def detect_test_framework(import_modules: list[str], source_head: str = "") -> str:
    mods = " ".join(import_modules).lower()
    found = []
    for needle, name in (
        ("pytest", "pytest"), ("unittest", "unittest"), ("vitest", "vitest"), ("@jest", "jest"),
        ("jest", "jest"), ("mocha", "mocha"), ("chai", "chai"), ("@playwright/test", "playwright"),
        ("cypress", "cypress"), ("@testing-library", "testing-library"), ("supertest", "supertest"),
        ("nose", "nose"), ("hypothesis", "hypothesis"),
    ):
        if needle in mods and name not in found:
            found.append(name)
    if not found:
        head = source_head[:4000]
        if re.search(r"\bdescribe\(|\bit\(|\btest\(", head):
            found.append("jest-style")
        elif re.search(r"^def test_", head, re.M):
            found.append("pytest")
    return ", ".join(found) or "None"
