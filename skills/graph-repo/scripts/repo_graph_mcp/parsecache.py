"""Parse results remembered by file content, so a file is parsed once per version of it.

Rebuilding after an edit, building the commit before, or switching branches re-parses only the files
whose bytes changed; everything else comes back from here. Entries are keyed by language and a hash of
the file's bytes, and the whole cache is dropped when the parser version changes.

Stored as gzipped JSON (never pickle), in the repo-graph cache folder, one file per repository.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .parsers.base import Call, Endpoint, FileParse, HttpCall, Import, Symbol

log = logging.getLogger("repo_graph_mcp")

FORMAT = 1
MAX_ENTRIES = int(os.getenv("REPO_GRAPH_PARSE_CACHE_ENTRIES", "30000"))


def content_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def body_hash(lines: list[bytes], start: int, end: int) -> str:
    """Fingerprint of a symbol's source lines (1-based, inclusive), ignoring trailing whitespace."""
    chunk = b"\n".join(line.rstrip() for line in lines[max(start - 1, 0):max(end, start)])
    return hashlib.sha1(chunk).hexdigest()[:12]


def to_dict(p: FileParse) -> dict:
    return asdict(p)


def from_dict(d: dict) -> FileParse:
    return FileParse(
        language=d["language"],
        symbols=[Symbol(**s) for s in d.get("symbols", [])],
        calls=[Call(**c) for c in d.get("calls", [])],
        imports=[Import(**i) for i in d.get("imports", [])],
        endpoints=[Endpoint(**e) for e in d.get("endpoints", [])],
        http_calls=[HttpCall(**h) for h in d.get("http_calls", [])],
        instances=d.get("instances", {}),
        route_prefixes=d.get("route_prefixes", {}),
        module_doc=d.get("module_doc", ""),
        default_export=d.get("default_export"),
        framework=d.get("framework", ""),
        loc=d.get("loc", 0),
        error=d.get("error"),
    )


class ParseCache:
    def __init__(self, path: Path | None):
        self.path = path
        self.entries: dict[str, dict] = {}
        self.used: set[str] = set()
        self.dirty = False
        self.hits = 0
        self.misses = 0
        if path and path.exists():
            try:
                with gzip.open(path, "rt", encoding="utf8") as fh:
                    data = json.load(fh)
                if data.get("format") == FORMAT and data.get("version") == __version__:
                    self.entries = data.get("entries", {})
            except Exception as e:  # a broken cache is just an empty one
                log.warning("ignoring unreadable parse cache %s: %s", path, e)

    @staticmethod
    def key(language: str, digest: str) -> str:
        return f"{language}:{digest}"

    def get(self, language: str, digest: str) -> FileParse | None:
        k = self.key(language, digest)
        d = self.entries.get(k)
        if d is None:
            self.misses += 1
            return None
        try:
            p = from_dict(d)
        except Exception:
            self.misses += 1
            return None
        self.used.add(k)
        self.hits += 1
        return p

    def put(self, language: str, digest: str, p: FileParse) -> None:
        k = self.key(language, digest)
        self.entries[k] = to_dict(p)
        self.used.add(k)
        self.dirty = True

    def save(self) -> None:
        if not (self.path and self.dirty):
            return
        keep = self.entries
        if len(keep) > MAX_ENTRIES:  # keep what this run used, then whatever else fits
            order = [k for k in keep if k in self.used] + [k for k in keep if k not in self.used]
            keep = {k: keep[k] for k in order[:MAX_ENTRIES]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".parses-", dir=self.path.parent)
            with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf8", compresslevel=5) as fh:
                json.dump({"format": FORMAT, "version": __version__, "entries": keep}, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
            self.dirty = False
        except Exception as e:
            log.warning("couldn't save parse cache %s: %s", self.path, e)
