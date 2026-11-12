"""Review a range of changes by what they did to the code's structure.

A pull request, a branch, a range of commits or the uncommitted work in a folder: `open_range`
finds the two sides (in a local checkout, or in a partial clone of a remote; a GitHub pull request
by number), `run` graphs each side once (cached by commit, like the history view) and `review`
compares them:

- what was added, changed, moved or removed (functions, classes, endpoints, agents, services…);
- callers that weren't updated: of a changed signature, of a removed function or endpoint;
- untouched callers of changed code, which may now behave differently;
- changed or new code that no test reaches, and the tests worth running for this change;
- new external services, env vars, endpoints, agents and MCP servers, and new or removed packages.

`to_markdown` writes the review up for a person or a model. Everything is static: calls are
resolved by name, so dynamic dispatch, reflection and string-built routes aren't followed. The
repository itself is never modified.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import fetch as F
from . import graph as G
from . import history as H
from .classify import classify
from .graph import CodeGraph

log = logging.getLogger("repo_graph_mcp")

WORKING = {"working", "worktree", "work-tree", "disk", "."}
CODE = (G.FUNCTION, G.CLASS, G.ENDPOINT)
CALLER_RELS = {G.CALLS, G.RUNS, G.ROUTES, G.USES_TOOL, G.EXTENDS, *G.HTTP_METHODS}
SURFACE = ((G.ENDPOINT, "endpoint"), (G.AGENT, "agent"), (G.MCP_SERVER, "MCP server"),
           (G.SERVICE, "external service"), (G.ENV_VAR, "env var"))
MANIFESTS = re.compile(r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml)$")
SHOW = 25  # items per list in the write-up


class RangeError(F.FetchError):
    pass


@dataclass
class Range:
    top: Path  # the repository's top folder (the local checkout, or a temporary clone)
    prefix: str  # the graphed sub-folder within it ("" = all of it)
    base: str | None  # base commit; None = nothing (the head is a root commit)
    head: str | None  # head commit; None = the working tree, as it is on disk now
    base_label: str
    head_label: str
    head_sha: str = ""  # the commit the head is on (HEAD, for the working tree)
    pr: dict[str, Any] | None = None
    local: bool = True
    commits: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)  # every changed file under prefix
    cleanup_dir: Path | None = None

    def close(self) -> None:
        if self.cleanup_dir:
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)
            self.cleanup_dir = None

    @property
    def short(self) -> str:
        return f"{(self.base or 'root')[:7]}..{(self.head or 'working')[:7]}"


# ── finding the two sides ─────────────────────────────────────────────────────

def _sha(top: Path, ref: str) -> str | None:
    for cand in (ref, f"origin/{ref}"):
        out = H.git(top, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False).strip()
        if out:
            return out
    return None


def _need(top: Path, ref: str) -> str:
    sha = _sha(top, ref)
    if not sha:
        raise RangeError(f"There's no commit, branch or tag called '{ref}' here.")
    return sha


def _default_branch(top: Path) -> str | None:
    head = H.git(top, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if head:
        return head
    for cand in ("main", "master", "origin/main", "origin/master", "trunk", "develop"):
        if _sha(top, cand):
            return cand
    return None


def _parent(top: Path, sha: str) -> str | None:
    return H.git(top, "rev-parse", "--verify", "--quiet", f"{sha}^1", check=False).strip() or None


def _pr_meta(spec: F.RepoSpec, number: int) -> dict[str, Any] | None:
    """Title, base branch and link of a GitHub pull request (None when the API can't be reached)."""
    if spec.kind != "github":
        return None
    import urllib.request

    req = urllib.request.Request(f"https://api.github.com/repos/{spec.owner}/{spec.name}/pulls/{number}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "repo-graph"})
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except Exception as e:  # rate limit, private repo without a token, offline…
        log.info("couldn't read PR #%s from the GitHub API: %s", number, e)
        return None
    return {"number": number, "title": data.get("title") or "", "url": data.get("html_url") or "",
            "base": (data.get("base") or {}).get("ref") or "", "base_sha": (data.get("base") or {}).get("sha") or "",
            "author": (data.get("user") or {}).get("login") or "",
            "state": data.get("state") or ""}


def _clone(spec: F.RepoSpec, work: Path, say: Callable[[str], None]) -> Path:
    """A clone with every branch's history but file contents fetched only when read (blob:none)."""
    if not shutil.which("git"):
        raise RangeError("Comparing two versions needs git, and git isn't installed here.")
    dest = work / "repo"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    say(f"cloning {spec.display} (history only; files are fetched as needed) …")
    base = ["git", "clone", "--quiet", "--no-checkout", "--no-tags"]
    for extra in (["--filter=blob:none"], []):  # not every server does partial clones
        shutil.rmtree(dest, ignore_errors=True)
        p = subprocess.run([*base, *extra, spec.url, str(dest)], capture_output=True, text=True, timeout=600, env=env)
        if p.returncode == 0:
            return dest
    raise RangeError(f"git clone failed: {p.stderr.strip()[:400]}")


def open_range(spec: F.RepoSpec, base: str | None = None, head: str | None = None, pr: int | None = None,
               say: Callable[[str], None] | None = None) -> Range:
    """The two sides to compare. Defaults: a local folder compares HEAD with what's on disk now (the
    uncommitted work); a ref is compared with where it branched from the default branch (or with its
    parent, when it is on the default branch); a pull request with where it branched from its base."""
    say = say or (lambda m: None)
    if spec.kind == "local":
        if pr:
            raise RangeError("A pull request needs the repository's URL or owner/repo; for a local checkout, "
                             "fetch the PR's branch and pass it as the head.")
        root = Path(spec.local_path).resolve()
        if not H.is_git(root):
            raise RangeError(f"{root} isn't a git checkout, so there are no two versions to compare.")
        top = Path(H.git(root, "rev-parse", "--show-toplevel").strip())
        prefix = H.git(root, "rev-parse", "--show-prefix").strip().rstrip("/")
        if spec.subpath.strip("/"):
            prefix = "/".join(p for p in (prefix, spec.subpath.strip("/")) if p)
        rng = Range(top, prefix, None, None, "", "", local=True)
    else:
        git_spec = spec if spec.kind == "git" else F.as_git(spec)
        F.resolve_tree_ref_git(git_spec)  # /tree/<branch>/<dir> links
        spec.ref, spec.subpath = git_spec.ref, git_spec.subpath
        work = F.new_work_dir()
        try:
            top = _clone(git_spec, work, say)
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        rng = Range(top, spec.subpath.strip("/"), None, None, "", "", local=False, cleanup_dir=work)
        if not head and spec.ref and not pr:
            head = spec.ref
    try:
        top = rng.top
        if pr:
            say(f"fetching pull request #{pr} …")
            p = subprocess.run(["git", "-C", str(top), "fetch", "--quiet", "origin", f"pull/{pr}/head:refs/remotes/origin/pr-{pr}"],
                               capture_output=True, text=True, timeout=300, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if p.returncode != 0:
                raise RangeError(f"Couldn't fetch pull request #{pr}: {p.stderr.strip()[:300]}")
            rng.pr = _pr_meta(spec, pr) or {"number": pr, "title": "", "url": "", "base": "", "author": "", "state": ""}
            if spec.kind == "github" and not rng.pr["url"]:
                rng.pr["url"] = f"{spec.url}/pull/{pr}"
            head = f"origin/pr-{pr}"
        if head is None and rng.local:
            head = "working"
        if head and head.lower() in WORKING:
            if not rng.local:
                raise RangeError("The working tree only exists for a local folder; pass a branch, tag or commit.")
            rng.head, rng.head_label = None, "working tree"
            rng.head_sha = _need(top, "HEAD")
        else:
            rng.head = rng.head_sha = _need(top, head or "HEAD")
            rng.head_label = f"PR #{pr}" if pr else (head or "HEAD")
        if base:
            rng.base, rng.base_label = _need(top, base), base
        elif rng.head is None:
            rng.base, rng.base_label = rng.head_sha, "HEAD"
        else:
            trunk = (rng.pr or {}).get("base") or _default_branch(top)
            mb = ""
            # a merged PR is already in its base branch; the base's tip from before the merge still
            # tells where it branched
            for against in ((rng.pr or {}).get("base_sha"), trunk, f"origin/{trunk}" if trunk else None):
                if against and _sha(top, against):
                    mb = H.git(top, "merge-base", against, rng.head, check=False).strip()
                    if mb and mb != rng.head:
                        break
            if mb and mb != rng.head:
                rng.base, rng.base_label = mb, f"where it branched from {trunk.removeprefix('origin/')}"
            else:
                rng.base = _parent(top, rng.head)
                rng.base_label = f"{rng.head_label}^ (its parent)" if rng.base else "nothing (first commit)"
        rng.commits = _commits(rng)
        rng.files = _files(rng)
    except Exception:
        rng.close()
        raise
    return rng


def _spec(rng: Range) -> list[str]:
    return ["--", rng.prefix] if rng.prefix else ["--", "."]


def _commits(rng: Range) -> list[dict[str, Any]]:
    end = rng.head or rng.head_sha
    span = f"{rng.base}..{end}" if rng.base else end
    out = H.git(rng.top, "log", "-n200", "--no-merges", "--format=%H%x1f%an%x1f%aI%x1f%s", span, *_spec(rng), check=False)
    commits = []
    for line in out.splitlines():
        sha, author, date, subject = (line.split("\x1f") + [""] * 4)[:4]
        if sha:
            commits.append({"sha": sha, "short": sha[:7], "author": author, "date": date, "subject": subject})
    return commits


def _files(rng: Range) -> list[dict[str, str]]:
    """Every file under the prefix that differs between the two sides (paths relative to the prefix)."""
    names = {"A": "added", "D": "deleted"}
    files: dict[str, str] = {}
    if rng.base is None:  # the head is a first commit: everything in it is new
        for path in H.git(rng.top, "ls-tree", "-r", "-z", "--name-only", rng.head or rng.head_sha, *_spec(rng)).split("\0"):
            if path:
                files[H._rel(path, rng.prefix)] = "added"
    else:
        args = ["diff", "--name-status", "--no-renames", "-z", rng.base] + ([rng.head] if rng.head else [])
        parts = H.git(rng.top, *args, *_spec(rng)).split("\0")
        for st, path in zip(parts[0::2], parts[1::2]):
            if path:
                files[H._rel(path, rng.prefix)] = names.get(st[:1], "modified")
        if rng.head is None:  # new files not added to git yet
            for path in H.git(rng.top, "ls-files", "--others", "--exclude-standard", "-z", *_spec(rng)).split("\0"):
                if path:
                    files.setdefault(H._rel(path, rng.prefix), "added")
    return [{"path": p, "status": s} for p, s in sorted(files.items())]


# ── graphing each side ─────────────────────────────────────────────────────────

def graph_side(rng: Range, sha: str | None, info: dict[str, Any], build: Callable[[Path, dict[str, Any]], CodeGraph],
               snapshots: H.Snapshots, key_for: Callable[[str], str], say: Callable[[str], None]) -> CodeGraph:
    """The graph of one side: a commit's snapshot (built once from `git archive`), or what's on disk."""
    if sha is None:
        say("building the working tree")
        return build(rng.top / rng.prefix if rng.prefix else rng.top,
                     {**info, "key": key_for(rng.head_sha) + "-dirty", "sha": rng.head_sha, "dirty": True})
    key = key_for(sha)
    g = snapshots.get(key)
    if g is not None:
        return g
    tmp = Path(tempfile.mkdtemp(prefix="repo-graph-diff-"))
    try:
        H.materialize(rng.top, sha, rng.prefix, tmp)
        say(f"building {sha[:7]}")
        g = build(tmp, {**info, "key": key, "sha": sha})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    snapshots.put(key, g)
    return g


def run(spec: F.RepoSpec, base: str | None, head: str | None, pr: int | None, *, info: dict[str, Any],
        build: Callable[[Path, dict[str, Any]], CodeGraph], snapshots: H.Snapshots, key_for: Callable[[str], str],
        say: Callable[[str], None] | None = None) -> tuple[CodeGraph, dict[str, Any], dict[str, Any]]:
    """Compare two sides of `spec`. Returns the head's graph, the review and the history for the view."""
    say = say or (lambda m: None)
    t0 = time.perf_counter()
    rng = open_range(spec, base, head, pr, say)
    try:
        sub = rng.prefix if rng.local else spec.subpath.strip("/")
        side = dict(info, subpath=sub)
        old = graph_side(rng, rng.base, side, build, snapshots, key_for, say) if rng.base else CodeGraph({})
        new = graph_side(rng, rng.head, side, build, snapshots, key_for, say)
        deps = dependency_changes(rng)
    finally:
        rng.close()
    rep = review(old, new, rng, deps=deps)
    url = None
    if spec.kind == "github":
        url = (rng.pr or {}).get("url") or (f"{spec.url}/compare/{rng.base}...{rng.head}" if rng.base and rng.head
                                            else f"{spec.url}/commit/{rng.head}" if rng.head else None)
    rep["url"] = url
    rep["built_ms"] = int((time.perf_counter() - t0) * 1000)
    return new, rep, history_for(rep)


# ── packages ───────────────────────────────────────────────────────────────────

def _show(rng: Range, sha: str | None, path: str) -> str | None:
    full = f"{rng.prefix}/{path}" if rng.prefix else path
    if sha is None:
        p = rng.top / full
        try:
            return p.read_text("utf8", "replace") if p.is_file() else None
        except OSError:
            return None
    try:
        return H.git(rng.top, "show", f"{sha}:{full}", check=True, timeout=120)
    except Exception:
        return None


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _packages(path: str, text: str | None) -> dict[str, str]:
    """name -> version spec, for one manifest."""
    if not text:
        return {}
    name = path.rsplit("/", 1)[-1]
    out: dict[str, str] = {}
    try:
        if name == "package.json":
            data = json.loads(text)
            for sect in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for k, v in (data.get(sect) or {}).items():
                    out[k] = str(v) + (" (dev)" if sect == "devDependencies" else "")
        elif name.endswith(".txt"):
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)", line)
                if m:
                    out[_norm(m.group(1))] = m.group(3).split(";")[0].strip()
        elif name == "pyproject.toml":
            try:
                import tomllib
            except ImportError:  # Python 3.10
                return {}
            data = tomllib.loads(text)
            reqs = list((data.get("project") or {}).get("dependencies") or [])
            for group in ((data.get("project") or {}).get("optional-dependencies") or {}).values():
                reqs += list(group)
            for line in reqs:
                m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)", line)
                if m:
                    out[_norm(m.group(1))] = m.group(3).split(";")[0].strip()
            poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
            for k, v in poetry.items():
                if k.lower() != "python":
                    out[_norm(k)] = v if isinstance(v, str) else str(v.get("version", "")) if isinstance(v, dict) else ""
    except Exception as e:
        log.info("couldn't read %s: %s", path, e)
    return out


def dependency_changes(rng: Range) -> list[dict[str, Any]]:
    changes = []
    for f in rng.files:
        if not MANIFESTS.search(f["path"]) or "node_modules/" in f["path"]:
            continue
        before = _packages(f["path"], _show(rng, rng.base, f["path"]) if rng.base else None)
        after = _packages(f["path"], _show(rng, rng.head, f["path"]))
        added = sorted(k for k in after if k not in before)
        removed = sorted(k for k in before if k not in after)
        bumped = sorted(k for k in after if k in before and after[k] != before[k])
        if added or removed or bumped:
            changes.append({"file": f["path"], "added": [{"name": k, "spec": after[k]} for k in added],
                            "removed": removed,
                            "changed": [{"name": k, "from": before[k], "to": after[k]} for k in bumped]})
    return changes


# ── the review ────────────────────────────────────────────────────────────────

def _params(sig: str) -> list[str] | None:
    """The parameters of a signature like `f(a, b: int = 1, *, c=None) -> X`, or None if it can't tell."""
    if not sig or "…" in sig or "(" not in sig:
        return None
    start, depth, out, cur = sig.index("(") + 1, 1, [], ""
    for ch in sig[start:]:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
            if depth == 0:
                break
        if ch == "," and depth == 1:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    else:
        return None  # unbalanced: cut short
    if cur.strip():
        out.append(cur.strip())
    return out


def _pname(param: str) -> str:
    return re.split(r"[:=]", param, maxsplit=1)[0].strip()


def compatible(before: str, after: str) -> bool:
    """True when every call that matched `before` still matches `after`: same parameters in the same
    order, defaults kept, and anything new is optional (has a default, or is *args / **kwargs)."""
    a, b = _params(before), _params(after)
    if a is None or b is None or len(b) < len(a):
        return False
    for old, new in zip(a, b):
        if _pname(old) != _pname(new) or ("=" in old and "=" not in new):
            return False
    return all("=" in x or x.startswith("*") for x in b[len(a):])


def _is_test(path: str) -> bool:
    return classify(path) == G.TEST_FILE


def _caller(g: CodeGraph, sid: str, rel: str) -> dict[str, Any]:
    n = g.nodes[sid]
    return H._rec(n, rel=rel)


def _test_reach(g: CodeGraph, depth: int = 3) -> tuple[set[str], set[str]]:
    """Code a test calls directly, and code reached from there within `depth` calls."""
    direct = {t for _s, t, r, _p in g.edges if r == G.TEST}
    reach, frontier = set(direct), list(direct)
    for _ in range(depth):
        nxt = []
        for nid in frontier:
            for t, r in g.out_adj.get(nid, ()):
                if r in CALLER_RELS and r != G.EXTENDS and t not in reach:
                    reach.add(t)
                    nxt.append(t)
        frontier = nxt
    return direct, reach


def _tests_for(g: CodeGraph, ids: list[str], depth: int = 2) -> dict[str, dict[str, Any]]:
    """Test files that call these nodes, or call code that calls them (up to `depth` steps away)."""
    tests: dict[str, dict[str, Any]] = {}
    seen, frontier = set(ids), [(i, i, 0) for i in ids]
    while frontier:
        nxt = []
        for nid, target, hops in frontier:
            for s, r in g.in_adj.get(nid, ()):
                if r == G.TEST:
                    path = g.nodes[s].path
                    t = tests.get(path)
                    if t is None or hops < t["hops"]:
                        tests[path] = {"path": path, "hops": hops, "reaches": g.nodes[target].props.get("qualname") or g.nodes[target].name}
                elif r in CALLER_RELS and r != G.EXTENDS and hops < depth and s not in seen:
                    seen.add(s)
                    nxt.append((s, target, hops + 1))
        frontier = nxt
    return tests


def review(old: CodeGraph, new: CodeGraph, rng: Range, deps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    d = H.diff_graphs(old, new, limit=None)
    files = rng.files
    touched = {f["path"] for f in files}
    graphed = [f for f in files if classify(f["path"]) is not None]
    code_add = [r for r in d["added"] if r["l"] in CODE]
    code_chg = [r for r in d["changed"] if r["l"] in CODE]
    code_rem = [r for r in d["removed"] if r["l"] in CODE]

    # moved: the same source lines now somewhere else
    by_hash: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in code_add:
        h = new.nodes[r["id"]].props.get("h")
        if h:
            by_hash[(r["l"], h)].append(r)
    moved = []
    for r in code_rem:
        h = old.nodes[r["id"]].props.get("h")
        cand = by_hash.get((r["l"], h)) if h else None
        if cand:
            moved.append({"from": r, "to": cand.pop(0)})
    moved_from = {m["from"]["id"]: m["to"]["id"] for m in moved}
    moved_to = {m["to"]["id"] for m in moved}
    added = [r for r in code_add if r["id"] not in moved_to]
    removed = [r for r in code_rem if r["id"] not in moved_from]

    # removed (or moved) code whose callers are still there, in files this range didn't touch
    still_called = []
    for r in code_rem:
        to = moved_from.get(r["id"])
        callers, tests = [], []
        for s, rel in old.in_adj.get(r["id"], ()):
            if rel not in CALLER_RELS and rel != G.TEST:
                continue
            if s not in new.nodes or new.nodes[s].path in touched:
                continue  # the caller went too, or its file changed (the call may have been updated)
            if to and any(t == to for t, _r in new.out_adj.get(s, ())):
                continue  # it now calls the moved copy
            (tests if rel == G.TEST else callers).append(_caller(new, s, rel))
        if callers or tests:
            still_called.append({**r, "moved_to": new.nodes[to].path if to else None,
                                 "callers": callers, "tests": tests})

    # changed signatures, and changed code, with callers in untouched files
    sig_changes, affected = [], []
    for r in code_chg:
        n, o = new.nodes[r["id"]], old.nodes[r["id"]]
        callers = [_caller(new, s, rel) for s, rel in new.in_adj.get(r["id"], ())
                   if rel in CALLER_RELS and new.nodes[s].path not in touched]
        tests = [new.nodes[s].path for s, rel in new.in_adj.get(r["id"], ()) if rel == G.TEST]
        if r.get("sig"):
            before, after = o.props.get("signature") or "", n.props.get("signature") or ""
            sig_changes.append({**r, "before": before, "after": after, "compatible": compatible(before, after),
                                "callers": callers, "tests": sorted(set(tests))})
        elif callers and r["l"] != G.CLASS:  # a class "changes" with any method: its methods say it better
            affected.append({**r, "callers": callers})

    # tests: what reaches the new and changed code, and which test files to run
    direct, reach = _test_reach(new)

    def via_owner(nid: str) -> bool:
        # calls on an instance (obj.method()) can't be resolved statically, so a method counts as
        # reached when a test reaches its class (or the function it is nested in)
        p = new.parent_of(nid)
        while p and new.nodes[p].label in (G.CLASS, G.FUNCTION):
            if p in reach:
                return True
            p = new.parent_of(p)
        return False

    coverage = {"direct": [], "indirect": [], "none": []}
    for r in added + code_chg + [m["to"] for m in moved]:
        if r["l"] == G.ENDPOINT and not new.nodes[r["id"]].props.get("defined_in_repo", True):
            continue  # a route the code calls but doesn't serve
        if _is_test(r["p"]) or r["l"] == G.CLASS:  # a class is covered through its methods
            continue
        kind = "direct" if r["id"] in direct else "indirect" if r["id"] in reach or via_owner(r["id"]) else "none"
        coverage[kind].append(r)
    impact = _tests_for(new, [r["id"] for r in added + code_chg + [m["to"] for m in moved] if not _is_test(r["p"])])
    for f in files:
        if _is_test(f["path"]) and f["status"] != "deleted":
            impact.setdefault(f["path"], {"path": f["path"], "hops": -1, "reaches": "changed in this range"})
    tests_to_run = sorted(impact.values(), key=lambda t: (t["hops"], t["path"]))

    # what the app is built on: endpoints, agents, MCP servers, services, env vars
    surface: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for lbl, _word in SURFACE:
        a = [r for r in d["added"] if r["l"] == lbl and r["id"] not in moved_to]
        c = [r for r in d["changed"] if r["l"] == lbl]
        x = [r for r in d["removed"] if r["l"] == lbl and r["id"] not in moved_from]
        if lbl == G.SERVICE:
            for r in a:
                r["used_by"] = sorted({new.nodes[s].path for s, rel in new.in_adj.get(r["id"], ()) if rel == G.USES})[:5]
        if lbl == G.ENV_VAR:
            for r in a + c:
                w = new.nodes[r["id"]].props.get("warning")
                if w:
                    r["warning"] = w
                r["read_in"] = list(new.nodes[r["id"]].props.get("files") or [])[:5]
        if a or c or x:
            surface[lbl] = {"added": a, "changed": c, "removed": x}

    rep: dict[str, Any] = {
        "base": rng.base, "head": rng.head, "head_sha": rng.head_sha, "base_label": rng.base_label,
        "head_label": rng.head_label, "short": rng.short, "pr": rng.pr, "commits": rng.commits,
        "files": files, "graphed_files": len(graphed), "summary": H.describe_counts(d),
        "added": added, "changed": code_chg, "removed": removed, "moved": moved,
        "still_called": still_called, "signature_changes": sig_changes, "affected": affected,
        "coverage": coverage, "tests_to_run": tests_to_run, "surface": surface, "dependencies": deps or [],
        "warnings": list(new.warnings)[:10],
        "diff": H.cut(d), "graphed": [f for f in graphed][:200],
    }
    rep["check_first"] = _check_first(rep)
    return rep


def _name(r: dict[str, Any]) -> str:
    return (r.get("q") or r["n"]) + ("()" if r["l"] == G.FUNCTION else "")


def _where(r: dict[str, Any]) -> str:
    return r["p"] + (f":{r['line']}" if r.get("line") else "")


def _check_first(rep: dict[str, Any]) -> list[str]:
    """The few things a reviewer should look at before anything else, most likely breakage first."""
    out = []
    for r in rep["still_called"]:
        who = r["callers"] + r["tests"]
        where = ", ".join(f"`{_name(c)}` ({_where(c)})" for c in who[:3]) + (f" and {len(who) - 3} more" if len(who) > 3 else "")
        what = f"moved to `{r['moved_to']}`" if r["moved_to"] else "removed"
        out.append(f"**`{_name(r)}` was {what}, but files this change didn't touch still call it:** {where}.")
    for r in rep["signature_changes"]:
        if r["callers"] and not r["compatible"]:
            where = ", ".join(f"`{_name(c)}` ({_where(c)})" for c in r["callers"][:3]) + (
                f" and {len(r['callers']) - 3} more" if len(r["callers"]) > 3 else "")
            out.append(f"**`{_name(r)}` changed its signature** (`{r['before']}` → `{r['after']}`) and "
                       f"{len(r['callers'])} caller{'s' if len(r['callers']) != 1 else ''} in unchanged files may still use the old one: {where}.")
    for r in rep["surface"].get(G.ENV_VAR, {}).get("added", []) + rep["surface"].get(G.ENV_VAR, {}).get("changed", []):
        if r.get("warning"):
            out.append(f"**Env var `{r['n']}`:** {r['warning']}")
    for dep in rep["dependencies"]:
        if dep["added"]:
            names = ", ".join(f"`{a['name']}`" for a in dep["added"][:8]) + (" …" if len(dep["added"]) > 8 else "")
            out.append(f"**New package{'s' if len(dep['added']) > 1 else ''} in `{dep['file']}`:** {names}. "
                       "Check each one exists, is the one meant, and is maintained.")
    news = [(lbl, word, r) for lbl, word in SURFACE if lbl != G.ENDPOINT
            for r in rep["surface"].get(lbl, {}).get("added", [])]
    if news:
        out.append("**New dependencies on the outside world:** " + ", ".join(
            f"{word} `{r['n']}`" for _l, word, r in news[:8]) + (" …" if len(news) > 8 else "")
                   + ". Each needs configuring wherever this runs.")
    gone = [r for r in rep["surface"].get(G.ENDPOINT, {}).get("removed", [])]
    if gone:
        out.append("**Endpoints removed:** " + ", ".join(f"`{r['n']}`" for r in gone[:6])
                   + ". Clients outside this repo may still call them.")
    none = rep["coverage"]["none"]
    if none:
        names = ", ".join(f"`{_name(r)}`" for r in none[:6]) + (f" and {len(none) - 6} more" if len(none) > 6 else "")
        out.append(f"**{len(none)} new or changed function{'s' if len(none) != 1 else ''} no test reaches:** {names}.")
    return out


# ── for the view ──────────────────────────────────────────────────────────────

def history_for(rep: dict[str, Any]) -> dict[str, Any]:
    """A history whose one entry is this range, so the view opens with its changes marked."""
    pr = rep.get("pr")
    shas = all(re.fullmatch(r"[0-9a-f]{7,40}", x or "") for x in (rep["base_label"], rep["head_label"]))
    if pr:
        title = f"PR #{pr['number']}" + (f": {pr['title']}" if pr.get("title") else "")
    elif rep["head"] is None:
        title = "Uncommitted work" if not rep["commits"] else f"{rep['base_label']} → working tree"
    elif len(rep["commits"]) == 1:
        title = rep["commits"][0]["subject"]
    elif shas:
        title = f"{len(rep['commits'])} commits"
    else:
        title = f"{rep['base_label']} → {rep['head_label']}"
    entry = {"id": "range", "kind": "range", "title": title, "short": rep["short"],
             "files": [{"path": f["path"], "status": f["status"]} for f in rep["graphed"]],
             "diff": rep["diff"], "summary": rep["summary"], "commits": len(rep["commits"])}
    if rep.get("url"):
        entry["url"] = rep["url"]
    return {"head": rep["head"] or rep["head_sha"], "short": (rep["head"] or rep["head_sha"])[:7],
            "branch": rep["head_label"], "upstream": None, "ahead": 0, "behind": 0, "dirty": rep["head"] is None,
            "range": {"base": rep["base"], "head": rep["head"], "label": rep["short"], "base_label": rep["base_label"],
                      "head_label": rep["head_label"], "commits": len(rep["commits"])},
            "entries": [entry], "built_ms": rep.get("built_ms", 0)}


# ── the write-up ──────────────────────────────────────────────────────────────

def _items(lst: list[Any], fmt: Callable[[Any], str], limit: int = SHOW) -> list[str]:
    lines = [f"- {fmt(x)}" for x in lst[:limit]]
    if len(lst) > limit:
        lines.append(f"- … and {len(lst) - limit} more")
    return lines


def to_markdown(rep: dict[str, Any], repo_name: str = "") -> str:
    pr = rep.get("pr")
    head = f"PR #{pr['number']}" + (f" · {pr['title']}" if pr.get("title") else "") if pr else rep["head_label"]
    lines = [f"# Changes in {repo_name or 'the repo'}: {rep['base_label']} → {head}", ""]
    facts = [f"`{rep['short']}`"]
    if rep["commits"]:
        facts.append(f"{len(rep['commits'])} commit{'s' if len(rep['commits']) != 1 else ''}")
    if rep["head"] is None:
        facts.append("including uncommitted work")
    facts.append(f"{len(rep['files'])} file{'s' if len(rep['files']) != 1 else ''} changed"
                 + (f" ({rep['graphed_files']} in the graph)" if rep["graphed_files"] != len(rep["files"]) else ""))
    if pr and pr.get("author"):
        facts.append(f"by {pr['author']}")
    lines += [" · ".join(facts), "", f"**Structure:** {rep['summary']}", ""]
    if rep.get("url"):
        lines += [f"On GitHub: {rep['url']}", ""]

    lines += ["## Check first", ""]
    lines += [f"{i}. {c}" for i, c in enumerate(rep["check_first"], 1)] or \
        ["Nothing stood out in the structure. This is a static check: still read the diff for the logic itself."]
    lines.append("")

    def section(title: str, body: list[str], note: str = "") -> None:
        if body:
            lines.extend([f"## {title}", ""] + ([note, ""] if note else []) + body + [""])

    section("Added", _items(rep["added"], lambda r: f"`{_name(r)}` — {_where(r)}"))
    section("Changed", _items(rep["changed"], lambda r: f"`{_name(r)}` — {_where(r)}" + (" · **signature changed**" if r.get("sig") else "")))
    section("Moved", _items(rep["moved"], lambda m: f"`{_name(m['from'])}` {m['from']['p']} → {_where(m['to'])}"))
    section("Removed", _items(rep["removed"], lambda r: f"`{_name(r)}` — {_where(r)}"))
    section("Signature changes", _items(rep["signature_changes"], lambda r: (
        f"`{_name(r)}` ({_where(r)}): `{r['before']}` → `{r['after']}`"
        + (" · backward compatible (only optional parameters added)" if r["compatible"] else
           f" · callers not updated: " + ", ".join(f"`{_name(c)}` ({_where(c)})" for c in r["callers"][:5]) if r["callers"]
           else " · every caller was updated")
        + (f" · tests: {', '.join(r['tests'][:3])}" if r["tests"] else ""))))
    section("May behave differently", _items(rep["affected"], lambda r: (
        f"`{_name(r)}` changed; unchanged callers: " + ", ".join(f"`{_name(c)}` ({_where(c)})" for c in r["callers"][:4])
        + (f" and {len(r['callers']) - 4} more" if len(r["callers"]) > 4 else ""))),
        "Code that calls something this change rewrote, in files it didn't touch.")
    cov = rep["coverage"]
    total = sum(len(v) for v in cov.values())
    if total:
        body = [f"Of {total} new or changed functions and endpoints: {len(cov['direct'])} called by a test, "
                f"{len(cov['indirect'])} reached only through other code or their class, "
                f"{len(cov['none'])} not reached by any test.", ""]
        body += _items(cov["none"], lambda r: f"no test: `{_name(r)}` — {_where(r)}")
        section("Tests", body)
    section("Tests to run", _items(rep["tests_to_run"], lambda t: (
        f"`{t['path']}` — " + ("changed in this range" if t["hops"] < 0 else
                               f"calls `{t['reaches']}`" if t["hops"] == 0 else
                               f"reaches `{t['reaches']}` through {t['hops']} call{'s' if t['hops'] > 1 else ''}"))))
    surf = []
    for lbl, word in SURFACE:
        s = rep["surface"].get(lbl)
        if not s:
            continue
        for r in s["added"]:
            extra = ""
            if r.get("used_by"):
                extra = " · used in " + ", ".join(r["used_by"][:3])
            if r.get("read_in"):
                extra = " · read in " + ", ".join(r["read_in"][:3])
            surf.append(f"- new {word} `{r['n']}`{extra}")
        surf += [f"- changed {word} `{r['n']}`" for r in s["changed"][:10]]
        surf += [f"- removed {word} `{r['n']}`" for r in s["removed"][:10]]
    section("Endpoints, services and config", surf)
    deps = []
    for dep in rep["dependencies"]:
        deps += [f"- `{dep['file']}`: + `{a['name']}` {a['spec']}".rstrip() for a in dep["added"]]
        deps += [f"- `{dep['file']}`: − `{n}`" for n in dep["removed"]]
        deps += [f"- `{dep['file']}`: `{c['name']}` {c['from']} → {c['to']}" for c in dep["changed"]]
    section("Packages", deps)
    section("Commits", _items(rep["commits"], lambda c: f"`{c['short']}` {c['subject']} ({c['author']})", 30))
    section("Files", _items(rep["files"], lambda f: f"{ {'added': '+', 'deleted': '−'}.get(f['status'], '~') } {f['path']}", 60))
    if rep["warnings"]:
        section("Graph warnings", [f"- {w}" for w in rep["warnings"]])
    lines += ["---", "Static analysis of the code's structure (repo-graph): calls are matched by name, so dynamic "
              "dispatch, reflection and string-built routes aren't followed, and only Python and JavaScript/TypeScript "
              "are parsed into functions. It points at where to look; it doesn't replace reading the diff."]
    return "\n".join(lines).rstrip() + "\n"
