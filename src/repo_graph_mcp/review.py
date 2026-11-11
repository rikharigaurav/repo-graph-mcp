"""Review: accept or undo the changes made to a watched folder, one file at a time.

When the live view starts, it keeps a compressed copy of every reviewable file (source, templates,
styles, JSON/YAML config: the files a code change touches; not lock files, `.env` files or anything
over 1 MB). That copy is the **baseline**. Every file that now differs from it is a change to review:

- **Accept** keeps the file as it is now: the baseline moves forward to it.
- **Undo** puts the baseline back on disk (deleting a file that didn't exist, recreating one that was
  deleted). The version that was undone is kept, so **Bring back** can restore it.

Undos are remembered until Claude Code next asks (scripts/live_notify.py --context, run by the
plugin's UserPromptSubmit hook), so Claude learns which of its changes the user rejected.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import threading
import time
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from .classify import classify

MAX_BYTES = 1_000_000
MAX_DIFF_LINES = 600
EXTRA_EXT = {"css", "scss", "sass", "less", "styl", "pcss", "json", "jsonc", "json5", "yaml", "yml", "toml",
             "svg", "graphql", "gql", "prisma", "sql", "ini", "cfg", "conf", "txt", "csv"}
SKIP_NAME = re.compile(r"(-lock\.|\.lock$|\.lockb$|\.min\.|\.map$|\.log$|\.timestamp-|~$|\.sw[po]$|\.tmp$|\.bak$)")


def reviewable(rel: str) -> bool:
    """Files a code change touches, minus secrets, lock files and transient files."""
    name = PurePosixPath(rel).name
    lower = name.lower()
    if lower.startswith(".") or SKIP_NAME.search(lower):  # .env, .DS_Store, editor swap files…
        return False
    if classify(rel) is not None:
        return True
    return "." in lower and lower.rsplit(".", 1)[1] in EXTRA_EXT


def transient(rel: str) -> bool:
    """Files tools write and delete again within a second (Vite's config bundles, editor swaps)."""
    return bool(re.search(r"\.timestamp-\d+|~$|\.sw[po]$|\.tmp$", PurePosixPath(rel).name.lower()))


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha1(data).hexdigest()


def _text(data: bytes | None) -> list[str] | None:
    """The file as lines, or None for binary content."""
    if data is None:
        return []
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf8").splitlines(keepends=True)
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1").splitlines(keepends=True)
        except Exception:
            return None


class Review:
    def __init__(self, root: Path, stats: dict[str, tuple[int, int]]):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.base: dict[str, tuple[str | None, bytes | None]] = {}  # rel -> (sha1 or None if absent, zlib content)
        self.base_stat: dict[str, tuple[int, int] | None] = {}
        self.cur_sha: dict[str, tuple[tuple[int, int] | None, str | None]] = {}  # rel -> (stat, sha) cache
        self.undone: list[dict[str, Any]] = []  # newest last; kept for "Bring back"
        self.unreported: list[dict[str, Any]] = []  # undos Claude Code hasn't been told about
        self.writes: dict[str, tuple[float, str]] = {}  # rel -> (time, "undo" | "bring back") of our own writes
        for rel, st in stats.items():
            if reviewable(rel) and st[1] <= MAX_BYTES:
                data = self._read(rel)
                self.base[rel] = (_sha(data), zlib.compress(data, 3) if data is not None else None)
                self.base_stat[rel] = st

    # ── files ──
    def _path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root.resolve() and self.root.resolve() not in p.parents:
            raise ValueError(f"{rel} is outside the watched folder")
        return p

    def _read(self, rel: str) -> bytes | None:
        try:
            p = self._path(rel)
            if p.stat().st_size > MAX_BYTES:
                return None
            return p.read_bytes()
        except (OSError, ValueError):
            return None

    def _base_bytes(self, rel: str) -> bytes | None:
        h, z = self.base.get(rel, (None, None))
        return zlib.decompress(z) if h is not None and z is not None else None

    def _current(self, rel: str, st: tuple[int, int] | None) -> tuple[bytes | None, str | None]:
        data = self._read(rel) if st is not None else None
        return data, _sha(data)

    # ── what changed ──
    def pending(self, stats: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
        out = []
        with self.lock:
            for rel in sorted(set(stats) | set(self.base)):
                if not reviewable(rel):
                    continue
                st = stats.get(rel)
                if st is not None and st[1] > MAX_BYTES:
                    continue
                if st == self.base_stat.get(rel) and (rel in self.base or st is None):
                    continue
                bh = self.base.get(rel, (None, None))[0]
                cached = self.cur_sha.get(rel)
                if cached and cached[0] == st and cached[1] == bh:
                    h, adds, dels, binary = cached[2:]
                else:
                    data, h = self._current(rel, st)
                    adds = dels = 0
                    binary = False
                    if h != bh:
                        old, new = _text(self._base_bytes(rel)), _text(data)
                        binary = old is None or new is None
                        if not binary:
                            for line in difflib.unified_diff(old, new, n=0):
                                if line.startswith("+") and not line.startswith("+++"):
                                    adds += 1
                                elif line.startswith("-") and not line.startswith("---"):
                                    dels += 1
                    self.cur_sha[rel] = (st, bh, h, adds, dels, binary)
                if h == bh:
                    continue
                out.append({"path": rel, "status": "added" if bh is None else "deleted" if h is None else "modified",
                            "adds": adds, "dels": dels, "binary": binary})
        return out

    def diff(self, rel: str) -> dict[str, Any]:
        with self.lock:
            old_b = self._base_bytes(rel)
            new_b = self._read(rel)
            old, new = _text(old_b), _text(new_b)
            if old is None or new is None:
                return {"path": rel, "binary": True, "lines": []}
            lines = list(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))[2:]
            cut = len(lines) > MAX_DIFF_LINES
            return {"path": rel, "binary": False, "lines": [ln.rstrip("\n") for ln in lines[:MAX_DIFF_LINES]],
                    "truncated": cut}

    # ── decisions ──
    def accept(self, rels: list[str], stats: dict[str, tuple[int, int]]) -> list[str]:
        done = []
        with self.lock:
            for rel in rels:
                if not reviewable(rel):
                    continue
                data = self._read(rel) if stats.get(rel) is not None else None
                self.base[rel] = (_sha(data), zlib.compress(data, 3) if data is not None else None)
                self.base_stat[rel] = stats.get(rel)
                self.cur_sha.pop(rel, None)
                done.append(rel)
        return done

    def undo(self, rels: list[str], who: dict[str, dict[str, Any]] | None = None) -> list[str]:
        """Put the baseline back for these files. `who` says who made each change (for Claude's notes)."""
        done = []
        with self.lock:
            for rel in rels:
                if not reviewable(rel):
                    continue
                p = self._path(rel)
                rejected = self._read(rel) if p.exists() else None
                original = self._base_bytes(rel)
                if original is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(original)
                self.writes[rel] = (time.time(), "undo")
                self.cur_sha.pop(rel, None)
                entry = {"path": rel, "at": time.time(), "by": (who or {}).get(rel, {}).get("source", "disk"),
                         "tools": (who or {}).get(rel, {}).get("tools", []),
                         "rejected": zlib.compress(rejected, 3) if rejected is not None else None,
                         "was": "added" if original is None else "deleted" if rejected is None else "modified"}
                self.undone = [u for u in self.undone if u["path"] != rel][-19:] + [entry]
                self.unreported = [u for u in self.unreported if u["path"] != rel] + [entry]
                done.append(rel)
        return done

    def bring_back(self, rel: str) -> bool:
        """Restore the version that was undone (the user changed their mind)."""
        with self.lock:
            entry = next((u for u in self.undone if u["path"] == rel), None)
            if not entry:
                return False
            p = self._path(rel)
            if entry["rejected"] is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(zlib.decompress(entry["rejected"]))
            self.writes[rel] = (time.time(), "bring back")
            self.cur_sha.pop(rel, None)
            self.undone = [u for u in self.undone if u["path"] != rel]
            self.unreported = [u for u in self.unreported if u["path"] != rel]
            return True

    def ours(self, rel: str, within: float = 10.0) -> str | None:
        """"undo" or "bring back" when the review itself just wrote this file."""
        w = self.writes.get(rel)
        return w[1] if w and time.time() - w[0] < within else None

    def undone_list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [{"path": u["path"], "at": u["at"], "by": u["by"], "tools": u["tools"], "was": u["was"]}
                    for u in reversed(self.undone)]

    def take_unreported(self) -> list[dict[str, Any]]:
        with self.lock:
            out = [{"path": u["path"], "by": u["by"], "tools": u["tools"], "was": u["was"]} for u in self.unreported]
            self.unreported = []
            return out


def message_for_claude(undone: list[dict[str, Any]]) -> str:
    """What Claude Code is told when the user undid changes in the live view."""
    if not undone:
        return ""
    lines = ["In the repo-graph live view, the user reviewed recent changes and undid these, so the files are",
             "back as they were before:"]
    for u in undone:
        who = "your change" if u["by"] == "claude" else "a change"
        what = {"added": "creating", "deleted": "deleting"}.get(u["was"], "editing")
        lines.append(f"- {u['path']}: {who} ({what} it) was undone")
    lines.append("Re-read those files before editing them again, and don't redo the undone changes unless the "
                 "user asks for them.")
    return "\n".join(lines)


__all__ = ["Review", "reviewable", "transient", "message_for_claude"]
