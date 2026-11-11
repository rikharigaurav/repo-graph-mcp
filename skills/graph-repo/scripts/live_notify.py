#!/usr/bin/env python3
"""Claude Code hook: tell a running repo-graph live view which file Claude just changed.

Runs after Edit, Write, MultiEdit and NotebookEdit (see hooks/hooks.json). It reads the hook's JSON from
stdin, looks for a live view watching a folder that contains the file (`graph_repo.py live` leaves a
lock file in the repo-graph cache), and posts the path to it, so the change shows up at once, labelled
with the tool that made it. With no live view running it does nothing. It never fails and never
prints, so it can't get in Claude's way. Standard library only: it starts in a few milliseconds.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


def cache_dir() -> Path:  # same rules as graph_repo.py
    base = os.getenv("REPO_GRAPH_HOME")
    if base:
        return Path(base)
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "repo-graph"
    return Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "repo-graph"


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return
    full = os.path.realpath(os.path.join(data.get("cwd") or os.getcwd(), path))
    key = os.path.normcase(full)
    try:
        locks = sorted((cache_dir() / "live").glob("*.json"))
    except OSError:
        return
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost never goes via a proxy
    body = json.dumps({"file": full, "tool": data.get("tool_name") or "", "session": data.get("session_id") or ""}).encode()
    for lf in locks:
        try:
            lock = json.loads(lf.read_text("utf8"))
            root = os.path.normcase(os.path.realpath(lock["root"]))
            if not (key == root or key.startswith(root.rstrip("\\/") + os.sep)):
                continue
            req = urllib.request.Request(f"http://127.0.0.1:{int(lock['port'])}/api/notify", data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "X-Repo-Graph-Token": str(lock.get("token", ""))})
            opener.open(req, timeout=1.0).close()
        except Exception:
            continue


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
