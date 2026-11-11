"""Graph cache: in-memory LRU backed by gzipped JSON on disk, keyed by commit."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

from .graph import CodeGraph

log = logging.getLogger("repo_graph_mcp")


class GraphStore:
    def __init__(self, max_items: int = 12, disk_dir: str | None = None):
        self.max_items = max_items
        self._mem: OrderedDict[str, CodeGraph] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        base = disk_dir or os.getenv("REPO_GRAPH_CACHE_DIR")
        self.disk = Path(base) / "graphs" if base else None
        if self.disk:
            self.disk.mkdir(parents=True, exist_ok=True)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def get(self, key: str) -> CodeGraph | None:
        g = self._mem.get(key)
        if g is not None:
            self._mem.move_to_end(key)
            return g
        path = self._path(key)
        if path and path.exists():
            try:
                with gzip.open(path, "rt", encoding="utf8") as fh:
                    g = CodeGraph.from_json(json.load(fh))
                self._remember(key, g)
                return g
            except Exception as e:
                log.warning("couldn't read cached graph %s: %s", path, e)
        return None

    def put(self, key: str, g: CodeGraph, persist: bool = True) -> None:
        self._remember(key, g)
        path = self._path(key)
        if persist and path:
            try:
                with gzip.open(path, "wt", encoding="utf8") as fh:
                    json.dump(g.to_json(), fh, separators=(",", ":"))
            except Exception as e:
                log.warning("couldn't persist graph %s: %s", path, e)

    def find(self, ref: str) -> tuple[str, CodeGraph] | None:
        """Most recent graph whose id or repo display name matches `ref`."""
        if ref in self._mem:
            return ref, self.get(ref)  # type: ignore[return-value]
        r = ref.lower().removesuffix(".git").rstrip("/")
        r = re.sub(r"^(https?://)?(www\.)?github\.com/", "", r)
        for key in reversed(self._mem):
            g = self._mem[key]
            names = {key.lower(), str(g.repo.get("display", "")).lower(), str(g.repo.get("url", "")).lower(),
                     str(g.repo.get("name", "")).lower()}
            if r in names or key.lower().startswith(r + "@"):
                return key, g
        g = self.get(ref)
        return (ref, g) if g else None

    def keys(self) -> list[str]:
        return list(self._mem)

    def _remember(self, key: str, g: CodeGraph) -> None:
        g.repo.setdefault("cached_at", time.time())
        self._mem[key] = g
        self._mem.move_to_end(key)
        while len(self._mem) > self.max_items:
            self._mem.popitem(last=False)

    def _path(self, key: str) -> Path | None:
        if not self.disk:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.@-]+", "_", key)
        return self.disk / f"{safe}.json.gz"
