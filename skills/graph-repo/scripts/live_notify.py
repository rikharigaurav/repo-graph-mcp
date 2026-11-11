#!/usr/bin/env python3
"""Claude Code hooks for the repo-graph live view.

Without arguments (PostToolUse, after Edit, Write, MultiEdit and NotebookEdit; see hooks/hooks.json): read
the hook's JSON from stdin, look for a live view watching a folder that contains the edited file
(`graph_repo.py live` leaves a lock file in the repo-graph cache), and post the path to it, so the change
shows up at once, labelled with the tool that made it.

With `--context` (UserPromptSubmit): ask the live view watching this project which changes the user
undid in its UI tab since Claude Code last asked, and print that, which Claude Code adds to the prompt's
context, so Claude doesn't redo them.

With no live view running both do nothing. They never fail and print nothing else, so they can't get in
Claude's way. Standard library only: they start in a few milliseconds.
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


def _locks() -> list[dict]:
    out = []
    try:
        for lf in sorted((cache_dir() / "live").glob("*.json")):
            try:
                out.append(json.loads(lf.read_text("utf8")))
            except Exception:
                continue
    except OSError:
        pass
    return out


def _post(lock: dict, path: str, body: dict, timeout: float = 1.0) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost never goes via a proxy
    req = urllib.request.Request(f"http://127.0.0.1:{int(lock['port'])}{path}", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "X-Repo-Graph-Token": str(lock.get("token", ""))})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def context(data: dict) -> None:
    """UserPromptSubmit: print what the user undid in the live view's UI tab, for Claude to read."""
    cwd = os.path.normcase(os.path.realpath(data.get("cwd") or os.getcwd()))
    for lock in _locks():
        try:
            root = os.path.normcase(os.path.realpath(lock["root"]))
            inside = cwd == root or cwd.startswith(root.rstrip("\\/") + os.sep) or root.startswith(cwd.rstrip("\\/") + os.sep)
            if not inside:
                continue
            message = json.loads(_post(lock, "/api/feedback", {}, timeout=2.0) or b"{}").get("message")
            if message:
                print(message)
        except Exception:
            continue


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    if "--context" in sys.argv[1:]:
        context(data)
        return
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return
    full = os.path.realpath(os.path.join(data.get("cwd") or os.getcwd(), path))
    key = os.path.normcase(full)
    body = {"file": full, "tool": data.get("tool_name") or "", "session": data.get("session_id") or ""}
    for lock in _locks():
        try:
            root = os.path.normcase(os.path.realpath(lock["root"]))
            if not (key == root or key.startswith(root.rstrip("\\/") + os.sep)):
                continue
            _post(lock, "/api/notify", body)
        except Exception:
            continue


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
