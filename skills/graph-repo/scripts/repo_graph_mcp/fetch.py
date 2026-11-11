"""Get a repository's files onto local disk as fast as possible.

GitHub repos are fetched as a single tarball (no git, no history) and
extracted with ignored folders and oversized files skipped on the fly.
Other git hosts fall back to `git clone --depth 1`. Local paths are read in
place. The commit SHA is resolved first so an unchanged repo is served from
cache without downloading anything.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .classify import is_ignored_dir

if TYPE_CHECKING:  # httpx is only needed by the MCP server's tarball path, not by the agent-mode CLI
    import httpx

log = logging.getLogger("repo_graph_mcp")

GITHUB_API = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
MAX_DOWNLOAD_BYTES = int(os.getenv("REPO_GRAPH_MAX_DOWNLOAD_MB", "300")) * 1024 * 1024
MAX_FILE_BYTES = int(os.getenv("REPO_GRAPH_MAX_FILE_KB", "768")) * 1024
ALLOW_LOCAL = os.getenv("REPO_GRAPH_ALLOW_LOCAL_PATHS", "1") not in ("0", "false", "no")

_GH_PATTERNS = [
    re.compile(r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?(?:/(?:tree|blob)/(?P<rest>.+?))?/?$"),
    re.compile(r"^git@github\.com:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?$"),
    re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:@(?P<ref>[\w./-]+))?$"),
]


class FetchError(RuntimeError):
    pass


@dataclass
class RepoSpec:
    kind: str  # "github" | "git" | "local"
    owner: str = ""
    name: str = ""
    ref: str | None = None
    subpath: str = ""
    url: str = ""
    local_path: str = ""
    tree_link: bool = False  # ref/subpath came from a /tree/<a>/<b>/<c> link and may need re-splitting

    def ref_candidates(self) -> list[tuple[str, str]]:
        """For /tree/feat/mcp-server/src: ('feat/mcp-server/src',''), ('feat/mcp-server','src'), ('feat','mcp-server/src')."""
        if not self.tree_link or not self.ref:
            return [(self.ref or "", self.subpath)]
        parts = [self.ref, *[p for p in self.subpath.split("/") if p]]
        return [("/".join(parts[:i]), "/".join(parts[i:])) for i in range(len(parts), 0, -1)]

    @property
    def display(self) -> str:
        if self.kind == "github":
            return f"{self.owner}/{self.name}"
        if self.kind == "local":
            return Path(self.local_path).name
        return self.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


@dataclass
class Checkout:
    root: Path  # directory to walk
    sha: str
    ref: str
    cleanup_dir: Path | None = None
    skipped_large: int = 0

    def cleanup(self) -> None:
        if self.cleanup_dir and self.cleanup_dir.exists():
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)


def parse_repo_spec(repo: str, ref: str | None = None, subpath: str | None = None) -> RepoSpec:
    s = repo.strip()
    if not s:
        raise FetchError("No repository given.")
    if ALLOW_LOCAL and (s.startswith(("/", "~", "./", "../")) or re.match(r"^[A-Za-z]:[\\/]", s)):
        p = Path(os.path.expanduser(s)).resolve()
        if not p.is_dir():
            raise FetchError(f"Local path not found on the MCP server's machine: {p}")
        return RepoSpec(kind="local", local_path=str(p), subpath=subpath or "")
    for pat in _GH_PATTERNS:
        m = pat.match(s)
        if m:
            gd = m.groupdict()
            spec = RepoSpec(kind="github", owner=gd["owner"], name=gd["repo"], ref=ref or gd.get("ref"))
            rest = gd.get("rest")
            if rest and not ref:
                # /tree/<branch>/<sub/path>: branch names may contain slashes, so the split is
                # provisional; fetchers try ref_candidates() against the real branch/tag list
                head, _, tail = rest.partition("/")
                spec.ref = head
                spec.subpath = tail
                spec.tree_link = bool(tail)
            spec.subpath = subpath or spec.subpath
            spec.url = f"https://github.com/{spec.owner}/{spec.name}"
            return spec
    if re.match(r"^(https?://|git@|ssh://)", s):
        return RepoSpec(kind="git", url=s, ref=ref, subpath=subpath or "")
    raise FetchError(
        f"Couldn't understand repository '{repo}'. Use a GitHub URL, 'owner/repo', any git URL, "
        "or an absolute local path."
    )


def _gh_headers(token: str | None = None) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "repo-graph-mcp"}
    tok = token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def resolve_github_sha(spec: RepoSpec, client: httpx.AsyncClient) -> tuple[str, str]:
    """Return (sha, ref) without downloading the repo."""
    ref = spec.ref
    if not ref:
        r = await client.get(f"{GITHUB_API}/repos/{spec.owner}/{spec.name}", headers=_gh_headers())
        _raise_for_github(r, spec)
        ref = r.json().get("default_branch") or "HEAD"
    candidates = spec.ref_candidates() if spec.ref else [(ref, spec.subpath)]
    for i, (cand, sub) in enumerate(candidates):
        r = await client.get(
            f"{GITHUB_API}/repos/{spec.owner}/{spec.name}/commits/{cand}",
            headers={**_gh_headers(), "Accept": "application/vnd.github.sha"},
        )
        if r.status_code in (404, 422) and i < len(candidates) - 1:
            continue  # not a ref; try a shorter branch name
        _raise_for_github(r, spec, cand)
        spec.ref, spec.subpath, spec.tree_link = cand, sub, False
        return r.text.strip(), cand
    raise FetchError(f"No branch or tag of {spec.display} matches '{spec.ref}'.")


def as_git(spec: RepoSpec) -> RepoSpec:
    """The same GitHub repo, fetched with `git clone` (uses the caller's own git credentials)."""
    return dataclasses.replace(spec, kind="git", url=spec.url + ".git")


def resolve_tree_ref_git(spec: RepoSpec) -> None:
    """Re-split an ambiguous /tree/ link using the remote's real branch and tag names."""
    if not spec.tree_link:
        return
    p = subprocess.run(["git", "ls-remote", "--heads", "--tags", spec.url], capture_output=True, text=True,
                       timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    names = set()
    for line in p.stdout.splitlines():
        ref = line.split("\t")[-1]
        for prefix in ("refs/heads/", "refs/tags/"):
            if ref.startswith(prefix):
                names.add(ref[len(prefix):].removesuffix("^{}"))
    for cand, sub in spec.ref_candidates():
        if cand in names:
            spec.ref, spec.subpath, spec.tree_link = cand, sub, False
            return


def _raise_for_github(r: httpx.Response, spec: RepoSpec, ref: str | None = None) -> None:
    if r.status_code < 400:
        return
    if r.status_code == 404:
        what = f"ref '{ref}' of " if ref else ""
        hint = "" if os.getenv("GITHUB_TOKEN") else " If it is private, set GITHUB_TOKEN for the MCP server."
        raise FetchError(f"GitHub couldn't find {what}{spec.owner}/{spec.name}.{hint}")
    if r.status_code in (401, 403, 429):
        raise FetchError(
            f"GitHub refused the request ({r.status_code}): {r.text[:200]}. "
            "Set GITHUB_TOKEN for higher rate limits or private repos."
        )
    raise FetchError(f"GitHub API error {r.status_code}: {r.text[:200]}")


async def download_github(spec: RepoSpec, sha: str, ref: str, client: httpx.AsyncClient, work_dir: Path) -> Checkout:
    url = f"{GITHUB_API}/repos/{spec.owner}/{spec.name}/tarball/{sha}"
    tar_path = work_dir / "repo.tar.gz"
    size = 0
    async with client.stream("GET", url, headers=_gh_headers(), follow_redirects=True) as r:
        _raise_for_github(r, spec, ref)
        with open(tar_path, "wb") as fh:
            async for chunk in r.aiter_bytes(1 << 16):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise FetchError(
                        f"Repository archive exceeds {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB. "
                        "Pass `path` to graph a sub-folder, or raise REPO_GRAPH_MAX_DOWNLOAD_MB."
                    )
                fh.write(chunk)
    dest = work_dir / "src"
    dest.mkdir()
    skipped = _safe_extract(tar_path, dest, spec.subpath)
    tar_path.unlink(missing_ok=True)
    tops = [p for p in dest.iterdir() if p.is_dir()]
    root = tops[0] if len(tops) == 1 else dest
    if spec.subpath:
        root = root / spec.subpath
        if not root.is_dir():
            raise FetchError(f"Path '{spec.subpath}' not found in {spec.display}@{ref}.")
    return Checkout(root=root, sha=sha, ref=ref, cleanup_dir=work_dir, skipped_large=skipped)


def _safe_extract(tar_path: Path, dest: Path, subpath: str = "") -> int:
    skipped = 0
    sub = subpath.strip("/")
    with tarfile.open(tar_path, "r:gz") as tf:
        for m in tf:
            parts = Path(m.name).parts
            if not parts or m.name.startswith("/") or ".." in parts:
                continue
            inner = parts[1:]  # strip the "<owner>-<repo>-<sha>/" prefix
            if any(is_ignored_dir(p) for p in inner[:-1]) or (m.isdir() and inner and is_ignored_dir(inner[-1])):
                continue
            if sub and inner and not "/".join(inner).startswith(sub):
                continue
            if m.isdir():
                (dest / m.name).mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():  # symlinks, devices, hardlinks
                continue
            if m.size > MAX_FILE_BYTES:
                skipped += 1
                continue
            target = dest / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return skipped


def remote_head(spec: RepoSpec) -> str | None:
    """The commit a remote branch (or the default branch) points at, without cloning."""
    if not shutil.which("git"):
        return None
    resolve_tree_ref_git(spec)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(["git", "ls-remote", spec.url, spec.ref or "HEAD"], capture_output=True, text=True,
                       timeout=60, env=env)
    for line in p.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name in ("HEAD", spec.ref, f"refs/heads/{spec.ref}", f"refs/tags/{spec.ref}"):
            return sha
    return None


def clone_git(spec: RepoSpec, work_dir: Path, depth: int = 1) -> Checkout:
    """Shallow clone; `depth` > 1 keeps that many commits of history (for the changes view)."""
    if not shutil.which("git"):
        raise FetchError("git is not installed on the MCP server, so only GitHub repos and local paths work.")
    resolve_tree_ref_git(spec)
    dest = work_dir / "src"
    cmd = ["git", "clone", "--depth", str(max(depth, 1)), "--single-branch", "--no-tags"]
    if spec.ref:
        cmd += ["--branch", spec.ref]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run([*cmd, spec.url, str(dest)], capture_output=True, text=True, timeout=300, env=env)
    if p.returncode != 0:
        raise FetchError(f"git clone failed: {p.stderr.strip()[:400]}")
    sha = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    ref = spec.ref or subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    root = dest / spec.subpath if spec.subpath else dest
    return Checkout(root=root, sha=sha or "unknown", ref=ref or "HEAD", cleanup_dir=work_dir)


def local_checkout(spec: RepoSpec) -> Checkout:
    root = Path(spec.local_path) / spec.subpath if spec.subpath else Path(spec.local_path)
    sha, ref = "local", "working-tree"
    if shutil.which("git"):
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
        if p.returncode == 0:
            sha = p.stdout.strip()
            dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True)
            ref = "working-tree" + ("*" if dirty.stdout.strip() else "")
    return Checkout(root=root, sha=sha, ref=ref)


def new_work_dir() -> Path:
    base = os.getenv("REPO_GRAPH_CACHE_DIR") or tempfile.gettempdir()
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="repo-graph-", dir=base))
