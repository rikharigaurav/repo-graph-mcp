"""Version control: what changed in the working tree and in recent commits, as graph changes.

For a git checkout (a local folder, or a clone of a GitHub repo with some history) this reads:
- HEAD, the branch and its upstream (`origin/main`), and which commits aren't pushed yet;
- the uncommitted changes (`git status`), shown as changes on top of HEAD;
- the last N commits on the branch, each as changes against its parent.

Each commit's graph is built once and kept as a snapshot keyed by its SHA, so the next time it is
needed it loads instead of being rebuilt; building one reuses the parse of every file whose bytes
were seen before (parsecache.py), so only the files a commit touched are parsed. Old commits are
read with `git archive` into a temporary folder: the repository itself is never modified, and
nothing here talks to the network.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import graph as G
from .classify import classify
from .graph import CodeGraph

log = logging.getLogger("repo_graph_mcp")

DEFAULT_COMMITS = int(os.getenv("REPO_GRAPH_HISTORY", "8"))
MAX_LISTED = 300  # nodes per change list
TRACKED = {G.SOURCE_FILE, G.TEST_FILE, G.DOC_FILE, G.TEMPLATE_FILE, G.CLASS, G.FUNCTION, G.ENDPOINT, G.AGENT,
           G.AGENT_STEP, G.MCP_SERVER, G.SERVICE, G.ENV_VAR}


def git(root: Path, *args: str, check: bool = True, binary: bool = False, timeout: int = 60):
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf8", "replace").strip()[:300])
    return p.stdout if binary else p.stdout.decode("utf8", "replace")


def is_git(root: Path) -> bool:
    if not shutil.which("git"):
        return False
    try:
        return git(root, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true"
    except Exception:
        return False


# ── reading the repository ────────────────────────────────────────────────────

def repo_state(root: Path, commits: int = DEFAULT_COMMITS) -> dict[str, Any] | None:
    """HEAD, branch, upstream, uncommitted files and the last `commits` commits touching `root`."""
    if not is_git(root):
        return None
    top = Path(git(root, "rev-parse", "--show-toplevel").strip())
    prefix = git(root, "rev-parse", "--show-prefix").strip().rstrip("/")  # graphed folder within the repo
    head = git(root, "rev-parse", "HEAD", check=False).strip()
    if not head:
        return None
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    spec = ["--", prefix] if prefix else []

    # uncommitted: staged, unstaged and untracked files under the graphed folder
    working: list[dict[str, str]] = []
    raw = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", *(["--", "."] if prefix else []))
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":  # renamed/copied: the original path follows
            i += 1
        status = "added" if code in ("??", "A ", "AM") or "A" in code else "deleted" if "D" in code else "modified"
        if classify(_rel(path, prefix)) is not None:  # only files the graph shows
            working.append({"path": _rel(path, prefix), "status": status})

    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).strip() or None
    unpushed: set[str] = set()
    behind = 0
    if upstream:
        unpushed = set(git(root, "rev-list", f"{upstream}..HEAD", check=False).split())
        behind = len(git(root, "rev-list", f"HEAD..{upstream}", check=False).split())

    log_fmt = "%x1e%H%x1f%P%x1f%an%x1f%aI%x1f%s"
    out = git(root, "log", f"-n{max(commits, 1)}", "--first-parent", f"--format={log_fmt}", "--name-status",
              "--no-renames", "HEAD", *spec, check=False)
    entries = []
    for block in out.split("\x1e")[1:]:
        head_line, _, rest = block.partition("\n")
        sha, parents, author, date, subject = (head_line.split("\x1f") + [""] * 5)[:5]
        files = []
        for line in rest.strip().splitlines():
            st, _, path = line.partition("\t")
            if not path or classify(_rel(path, prefix)) is None:
                continue
            files.append({"path": _rel(path, prefix), "status": {"A": "added", "D": "deleted"}.get(st[:1], "modified")})
        entries.append({"sha": sha, "parent": (parents.split() or [""])[0], "author": author, "date": date,
                        "subject": subject, "files": files, "pushed": bool(upstream) and sha not in unpushed})
    return {"top": str(top), "prefix": prefix, "head": head, "branch": branch, "upstream": upstream,
            "ahead": len(unpushed), "behind": behind, "working": working, "commits": entries[:commits]}


def _safe(key: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", key)[:180]


def _read_json(path: Path | None) -> Any:
    import json
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text("utf8"))
    except Exception:
        return None


def _write_json(path: Path, data: Any) -> None:
    import json
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, separators=(",", ":")), "utf8")
    except Exception as e:
        log.warning("couldn't write %s: %s", path, e)


def _rel(path: str, prefix: str) -> str:
    return path[len(prefix) + 1:] if prefix and path.startswith(prefix + "/") else path


def materialize(top: Path, sha: str, prefix: str, dest: Path) -> Path:
    """Write the tree of `sha` (only `prefix` when set) into `dest` without touching the repository."""
    data = git(top, "archive", "--format=tar", sha, *([prefix] if prefix else []), binary=True, timeout=300)
    base = dest.resolve()
    strip = prefix + "/" if prefix else ""
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf:
            name = m.name
            if strip:
                if not name.startswith(strip):
                    continue
                name = name[len(strip):]
            if not name or not m.isfile():
                continue
            target = (base / name).resolve()
            if not str(target).startswith(str(base) + os.sep):
                continue  # never write outside dest
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is not None:
                with src, open(target, "wb") as fh:
                    shutil.copyfileobj(src, fh)
    return dest


# ── comparing two graphs ─────────────────────────────────────────────────────

def _sig(n: G.Node) -> Any:
    """What has to differ for a node that exists in both graphs to count as changed."""
    p = n.props
    if n.label in G.FILE_LABELS:
        return p.get("hash")
    if n.label in (G.FUNCTION, G.CLASS):
        return p.get("h") or (p.get("signature"), p.get("loc"))
    if n.label == G.ENDPOINT:
        return (p.get("h"), p.get("file_path"), p.get("defined_in_repo"))
    if n.label == G.AGENT:
        return (tuple(p.get("flow") or ()), p.get("steps"), p.get("framework"))
    if n.label == G.AGENT_STEP:
        return (p.get("runs"), p.get("kind"))
    if n.label == G.MCP_SERVER:
        return (p.get("tools"), p.get("resources"), p.get("prompts"))
    if n.label == G.ENV_VAR:
        return (p.get("mode"), p.get("warning"))
    if n.label == G.SERVICE:
        return tuple(sorted(p.get("sdks") or ()))
    return None


def _rec(n: G.Node, **extra: Any) -> dict[str, Any]:
    r = {"id": n.id, "l": n.label, "n": n.name, "p": n.path}
    q = n.props.get("qualname")
    if q and q != n.name:
        r["q"] = q
    if n.props.get("line"):
        r["line"] = n.props["line"]
    r.update(extra)
    return r


def diff_graphs(old: CodeGraph, new: CodeGraph, limit: int | None = MAX_LISTED) -> dict[str, Any]:
    """Nodes added, changed and removed between two graphs; each list is cut at `limit` (None = all)."""
    added, changed, removed = [], [], []
    for nid, n in new.nodes.items():
        if n.label not in TRACKED:
            continue
        o = old.nodes.get(nid)
        if o is None:
            added.append(_rec(n))
        elif _sig(o) != _sig(n):
            extra = {}
            if n.label in (G.FUNCTION, G.CLASS, G.ENDPOINT) and o.props.get("signature") != n.props.get("signature"):
                extra["sig"] = True  # callers may need to change too
            changed.append(_rec(n, **extra))
    for nid, o in old.nodes.items():
        if o.label in TRACKED and nid not in new.nodes:
            removed.append(_rec(o))

    def links(g: CodeGraph) -> set[tuple[str, str, str]]:
        return {(s, t, r) for s, t, r, _ in g.edges if r != G.BELONGS_TO}

    a, b = links(old), links(new)
    counts: dict[str, list[int]] = {}
    for kind, lst in (("added", added), ("changed", changed), ("removed", removed)):
        for r in lst:
            counts.setdefault(r["l"], [0, 0, 0])[("added", "changed", "removed").index(kind)] += 1
    order = {lbl: i for i, lbl in enumerate((G.ENDPOINT, G.AGENT, G.SERVICE, G.MCP_SERVER, G.ENV_VAR, G.CLASS,
                                             G.FUNCTION, G.AGENT_STEP, G.SOURCE_FILE, G.TEMPLATE_FILE,
                                             G.TEST_FILE, G.DOC_FILE))}
    for lst in (added, changed, removed):
        lst.sort(key=lambda r: (order.get(r["l"], 99), r["p"], r.get("line", 0)))
    return {
        "added": added[:limit], "changed": changed[:limit], "removed": removed[:limit],
        "counts": counts,
        "links": {"added": len(b - a), "removed": len(a - b)},
        "truncated": limit is not None and max(len(added), len(changed), len(removed)) > limit,
    }


def cut(d: dict[str, Any], limit: int = MAX_LISTED) -> dict[str, Any]:
    """`d` with each list cut at `limit`, as the view takes it."""
    longest = max(len(d["added"]), len(d["changed"]), len(d["removed"]))
    return {**d, "added": d["added"][:limit], "changed": d["changed"][:limit], "removed": d["removed"][:limit],
            "truncated": d.get("truncated", False) or longest > limit}


def describe_counts(d: dict[str, Any]) -> str:
    """`+3 functions, ~2 changed, -1 removed · new endpoint POST /x · new service OpenAI`"""
    c = d.get("counts", {})
    code = [sum(c.get(lbl, [0, 0, 0])[i] for lbl in (G.FUNCTION, G.CLASS)) for i in range(3)]
    files = [sum(c.get(lbl, [0, 0, 0])[i] for lbl in G.FILE_LABELS) for i in range(3)]
    bits = []
    if any(files):
        bits.append(f"{files[0] + files[1] + files[2]} file{'s' if files[0] + files[1] + files[2] != 1 else ''}")
    if any(code):
        bits.append(" ".join(f"{sym}{v}" for sym, v in zip("+~-", code) if v) + " functions/classes")
    for lbl, word in ((G.ENDPOINT, "endpoint"), (G.SERVICE, "service"), (G.AGENT, "agent"), (G.MCP_SERVER, "MCP server"),
                      (G.ENV_VAR, "env var")):
        news = [r["n"] for r in d.get("added", []) if r["l"] == lbl]
        gone = [r["n"] for r in d.get("removed", []) if r["l"] == lbl]
        if news:
            bits.append(f"new {word}{'s' if len(news) > 1 else ''} " + ", ".join(news[:3]) + (" …" if len(news) > 3 else ""))
        if gone:
            bits.append(f"removed {word}{'s' if len(gone) > 1 else ''} " + ", ".join(gone[:3]))
    return " · ".join(bits) or "no code changes"


# ── putting it together ──────────────────────────────────────────────────────

class Snapshots:
    """Graphs of commits, stored by key (repo + SHA) as gzipped JSON in the cache folder."""

    def __init__(self, store):
        self.store = store

    def get(self, key: str) -> CodeGraph | None:
        g = self.store.get(key)
        if g is not None and g.repo.get("builder") != __version__:
            return None  # built by another version of the parser: rebuild
        return g

    def put(self, key: str, g: CodeGraph) -> None:
        g.repo["builder"] = __version__
        self.store.put(key, g)


def build_history(root: Path, info: dict[str, Any], key_for: Callable[[str], str],
                  build: Callable[[Path, dict[str, Any]], CodeGraph], snapshots: Snapshots,
                  commits: int = DEFAULT_COMMITS, commit_url: Callable[[str], str] | None = None,
                  log_fn: Callable[[str], None] | None = None,
                  cache_dir: Path | None = None) -> tuple[CodeGraph, dict[str, Any] | None]:
    """Graph of what's on disk now, plus the changes in it and in recent commits.

    `key_for(sha)` names a commit's snapshot; `build(folder, info)` builds a graph. Returns the
    current graph (HEAD's snapshot when nothing is uncommitted) and the history for the view.
    """
    say = log_fn or (lambda m: None)
    state = repo_state(root, commits)
    if state is None:
        return build(root, info), None
    t0 = time.perf_counter()
    top, prefix = Path(state["top"]), state["prefix"]
    built: dict[str, CodeGraph] = {}
    tmp_root = Path(tempfile.mkdtemp(prefix="repo-graph-hist-"))

    def commit_graph(sha: str) -> CodeGraph | None:
        if not sha:
            return None
        if sha in built:
            return built[sha]
        key = key_for(sha)
        g = snapshots.get(key)
        if g is None:
            dest = tmp_root / sha[:12]
            try:
                materialize(top, sha, prefix, dest)
            except Exception as e:
                log.warning("couldn't read commit %s: %s", sha[:12], e)
                return None
            say(f"building {sha[:7]}")
            g = build(dest, {**info, "key": key, "sha": sha, "ref": info.get("ref")})
            snapshots.put(key, g)
            shutil.rmtree(dest, ignore_errors=True)
        built[sha] = g
        return g

    try:
        head_g = commit_graph(state["head"]) if state["working"] else None
        if state["working"]:
            current = build(root, {**info, "sha": state["head"], "dirty": True})
        else:
            current = commit_graph(state["head"])
            if current is None:  # couldn't read HEAD's tree: build what's on disk
                current = build(root, info)
                snapshots.put(key_for(state["head"]), current)
        entries: list[dict[str, Any]] = []
        if state["working"]:
            d = diff_graphs(head_g, current) if head_g else None
            entries.append({"id": "working", "kind": "working", "title": "Uncommitted changes",
                            "files": state["working"][:200], "diff": d,
                            "summary": describe_counts(d) if d else f"{len(state['working'])} files"})
        # commit entries depend only on HEAD and the upstream; reuse them when HEAD hasn't moved
        cache_file = cache_dir / f"{_safe(key_for(state['head']))}-{commits}.json" if cache_dir else None
        cached = _read_json(cache_file)
        if cached and cached.get("version") == __version__:
            for e in cached["entries"]:
                e["pushed"] = bool(state["upstream"]) and e["sha"] in {c["sha"] for c in state["commits"] if c["pushed"]}
            entries += cached["entries"]
            state["commits"] = []
        new_entries: list[dict[str, Any]] = []
        for c in state["commits"]:
            after = commit_graph(c["sha"])
            before = commit_graph(c["parent"]) if c["parent"] else CodeGraph({})
            d = diff_graphs(before, after) if after is not None and before is not None else None
            entries.append({
                "id": c["sha"], "kind": "commit", "sha": c["sha"], "short": c["sha"][:7], "title": c["subject"],
                "author": c["author"], "date": c["date"], "pushed": c["pushed"], "files": c["files"][:200],
                **({"url": commit_url(c["sha"])} if commit_url else {}),
                "diff": d, "summary": describe_counts(d) if d else f"{len(c['files'])} files",
            })
            new_entries.append(entries[-1])
        if new_entries and cache_file:
            _write_json(cache_file, {"version": __version__, "entries": new_entries})
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    if state["branch"] and state["branch"] != "HEAD":
        current.repo["ref"] = state["branch"] + ("*" if state["working"] else "")
    hist = {
        "head": state["head"], "short": state["head"][:7], "branch": state["branch"],
        "upstream": state["upstream"], "ahead": state["ahead"], "behind": state["behind"],
        "dirty": bool(state["working"]), "entries": entries,
        "built_ms": int((time.perf_counter() - t0) * 1000),
    }
    if commit_url:
        hist["head_url"] = commit_url(state["head"])
    return current, hist
