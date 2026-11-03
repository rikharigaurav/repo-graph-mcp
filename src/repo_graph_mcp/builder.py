"""Turn a checked-out repository into a CodeGraph.

This is the graph half of Read-Code-Structure's `getFilesContext`, without the
Mistral summaries and Pinecone embeddings that made it slow. Everything here
is deterministic: walk → classify → tree-sitter parse → resolve names.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import graph as G
from .genai import add_genai_layer
from .classify import classify, detect_test_framework, ext_of, is_ignored_dir, language_of
from .graph import CodeGraph, Node
from .parsers.base import FileParse, normalize_route
from .parsers.js_parser import parse_js
from .parsers.python_parser import parse_python

log = logging.getLogger("repo_graph_mcp")

MAX_FILES = int(os.getenv("REPO_GRAPH_MAX_FILES", "6000"))
MAX_PARSE_BYTES = int(os.getenv("REPO_GRAPH_MAX_FILE_KB", "768")) * 1024
JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte")
PY_FRAMEWORKS = (("fastapi", "fastapi"), ("flask", "flask"), ("starlette", "starlette"), ("sanic", "sanic"),
                 ("quart", "quart"), ("aiohttp", "aiohttp"), ("ninja", "django-ninja"), ("litestar", "litestar"))
JS_FRAMEWORKS = (("express", "express"), ("fastify", "fastify"), ("hono", "hono"), ("koa", "koa"),
                 ("next", "next.js"), ("@nestjs", "nestjs"), ("elysia", "elysia"))
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")


@dataclass
class FileInfo:
    rel: str
    label: str
    language: str | None
    size: int
    node_id: str
    parse: FileParse | None = None
    sym_ids: dict[str, list[str]] = field(default_factory=dict)  # qualname -> node ids
    module_aliases: dict[str, str] = field(default_factory=dict)  # receiver text -> file rel
    imported: dict[str, tuple[str, str]] = field(default_factory=dict)  # local -> (file rel, original name)
    star_imports: list[str] = field(default_factory=list)


def file_node_id(label: str, rel: str) -> str:
    prefix = {G.SOURCE_FILE: "SOURCECODEFILE", G.TEST_FILE: "TESTINGFILE",
              G.DOC_FILE: "DOCUMENTATIONFILE", G.TEMPLATE_FILE: "TEMPLATEMARKUPFILE"}[label]
    return f"{prefix}:{rel}:{ext_of(rel)}"


def folder_id(rel: str) -> str:
    return f"FOLDER:{rel or '.'}"


class Builder:
    def __init__(self, root: Path, repo: dict, progress: Callable[[float, str], None] | None = None):
        self.root = root
        self.repo = repo
        self.g = CodeGraph(repo)
        self.files: dict[str, FileInfo] = {}
        self.progress = progress or (lambda p, m: None)
        self.py_full: dict[str, str] = {}
        self.py_suffix: dict[str, list[str]] = defaultdict(list)
        self.ts_configs: list[tuple[str, str, dict[str, list[str]]]] = []  # (dir, baseUrl dir, paths)
        self.endpoint_index: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.endpoint_by_route: dict[str, list[str]] = defaultdict(list)
        self._routes_by_len: dict[int, list[tuple[list[str], str, list[str]]]] | None = None
        self.counters: Counter = Counter()
        self.genai_stats: dict[str, int] = {}

    # ── 1. walk ────────────────────────────────────────────────────────────
    def walk(self) -> None:
        root_name = self.repo.get("name") or self.root.name
        self.g.add_node(Node(folder_id(""), G.FOLDER, root_name, "",
                             {"folder_name": root_name, "directory_path": "", "is_root": True}))
        kept: list[tuple[str, str, int]] = []
        skipped = Counter()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not is_ignored_dir(d))
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            for fn in sorted(filenames):
                rel = f"{rel_dir}/{fn}" if rel_dir else fn
                if fn in ("tsconfig.json", "jsconfig.json"):
                    self._load_tsconfig(rel)
                label = classify(rel)
                if label is None:
                    skipped["ignored"] += 1
                    continue
                try:
                    size = os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    continue
                kept.append((rel, label, size))
        if len(kept) > MAX_FILES:
            kept.sort(key=lambda x: (x[0].count("/"), x[0]))
            self.g.warnings.append(
                f"Repository has {len(kept)} relevant files; graphed the {MAX_FILES} shallowest "
                "(set REPO_GRAPH_MAX_FILES or pass `path` to focus on a sub-folder)."
            )
            kept = kept[:MAX_FILES]
        for rel, label, size in kept:
            self._ensure_folder(posixpath.dirname(rel))
            nid = file_node_id(label, rel)
            lang = language_of(rel)
            self.g.add_node(Node(nid, label, posixpath.basename(rel), rel, {
                "file_name": posixpath.basename(rel), "file_path": rel, "file_ext": ext_of(rel),
                "size": size, **({"language": lang} if lang else {}),
            }))
            self.g.add_edge(nid, folder_id(posixpath.dirname(rel)), G.BELONGS_TO)
            self.files[rel] = FileInfo(rel, label, lang, size, nid)
        self.counters["files_ignored"] = skipped["ignored"]

    def _ensure_folder(self, rel_dir: str) -> None:
        if not rel_dir:
            return
        fid = folder_id(rel_dir)
        if fid in self.g.nodes:
            return
        parent = posixpath.dirname(rel_dir)
        self._ensure_folder(parent)
        name = posixpath.basename(rel_dir)
        self.g.add_node(Node(fid, G.FOLDER, name, rel_dir, {"folder_name": name, "directory_path": rel_dir}))
        self.g.add_edge(fid, folder_id(parent), G.BELONGS_TO)

    def _load_tsconfig(self, rel: str) -> None:
        try:
            raw = (self.root / rel).read_text("utf8", "replace")
            data = json.loads(_strip_jsonc(raw))
        except Exception:
            return
        opts = data.get("compilerOptions") or {}
        paths = opts.get("paths") or {}
        cfg_dir = posixpath.dirname(rel)
        base = posixpath.normpath(posixpath.join(cfg_dir, opts.get("baseUrl", "."))) if cfg_dir or opts.get("baseUrl") else ""
        base = "" if base == "." else base
        if isinstance(paths, dict):
            self.ts_configs.append((cfg_dir, base, {k: v for k, v in paths.items() if isinstance(v, list)}))

    # ── 2. parse ───────────────────────────────────────────────────────────
    def parse_all(self) -> None:
        targets = [f for f in self.files.values() if f.language and f.label in (G.SOURCE_FILE, G.TEST_FILE)]
        total = max(len(targets), 1)
        for i, f in enumerate(targets):
            if i % 50 == 0:
                self.progress(0.35 + 0.45 * i / total, f"Parsing {i}/{total} files")
            if f.size > MAX_PARSE_BYTES:
                self.counters["files_too_large"] += 1
                continue
            try:
                data = (self.root / f.rel).read_bytes()
            except OSError:
                continue
            if _looks_minified(data):
                self.counters["files_minified"] += 1
                continue
            try:
                f.parse = parse_python(data) if f.language == "python" else parse_js(data, f.language)
                self.counters["files_parsed"] += 1
            except Exception as e:  # never let one weird file kill the graph
                log.warning("parse failed for %s: %s", f.rel, e)
                self.counters["files_failed"] += 1
                continue
            node = self.g.nodes[f.node_id]
            node.props["loc"] = f.parse.loc
            if f.parse.module_doc:
                node.props["summary"] = f.parse.module_doc
            if f.label == G.TEST_FILE:
                head = data[:4000].decode("utf8", "replace")
                node.props["test_framework"] = detect_test_framework([i.module for i in f.parse.imports], head)
            f.parse.framework = _framework(f)
        # module index for python
        for f in self.files.values():
            if f.language == "python":
                mod = _py_module_name(f.rel)
                if not mod:
                    continue
                self.py_full.setdefault(mod, f.rel)
                parts = mod.split(".")
                pkg_dir = posixpath.dirname(f.rel)
                if f.rel.endswith("__init__.py"):
                    pkg_dir = posixpath.dirname(pkg_dir)
                for k in range(1, len(parts)):
                    # single-segment names ("utils") only count when they sit in a source root
                    # (a folder without __init__.py), otherwise `import json` would hit utils/json.py
                    if len(parts) - k == 1 and f"{pkg_dir}/__init__.py" in self.files:
                        continue
                    self.py_suffix[".".join(parts[k:])].append(f.rel)

    # ── 3. symbols & endpoints ─────────────────────────────────────────────
    def add_symbols(self) -> None:
        for f in self.files.values():
            p = f.parse
            if p is None:
                continue
            next_route = _nextjs_route(f.rel)
            pages_api = _pages_api_route(f.rel)
            handler_eps: dict[str, list[str]] = defaultdict(list)
            # routes declared inside test files are fixtures, not part of the app's API
            for ep in ([] if f.label == G.TEST_FILE else p.endpoints):
                eid = self._add_endpoint(ep.method, ep.route, f, ep.line, p.framework or ep.framework, ep.handler)
                if ep.handler:
                    handler_eps[ep.handler].append(eid)
                if ep.inline_scope:
                    f.sym_ids.setdefault(ep.inline_scope, []).append(eid)
            if f.label == G.TEST_FILE:
                for s in p.symbols:  # tests stay folded into their file node (same as the original schema)
                    f.sym_ids[s.qualname] = [f.node_id]
                continue
            for s in p.symbols:
                if next_route and s.kind == "function" and s.name.upper() in G.HTTP_METHODS and "." not in s.qualname:
                    eid = self._add_endpoint(s.name.upper(), next_route, f, s.line, "next.js", s.qualname,
                                             doc=s.docstring, sig=s.signature)
                    f.sym_ids[s.qualname] = [eid]
                    continue
                if pages_api and p.default_export == s.name:
                    eid = self._add_endpoint("ANY", pages_api, f, s.line, "next.js", s.qualname,
                                             doc=s.docstring, sig=s.signature)
                    f.sym_ids[s.qualname] = [eid]
                    continue
                if s.qualname in handler_eps:
                    for eid in handler_eps[s.qualname]:
                        n = self.g.nodes[eid]
                        n.props.update({"line": s.line, "end_line": s.end_line, "signature": s.signature,
                                        **({"summary": s.docstring} if s.docstring else {})})
                    f.sym_ids[s.qualname] = handler_eps[s.qualname]
                    continue
                label = G.CLASS if s.kind == "class" else G.FUNCTION
                prefix = "CLASS" if label == G.CLASS else "FUNCTION"
                nid = f"{prefix}:{f.rel}:{s.qualname}"
                props = {
                    "qualname": s.qualname, "file_path": f.rel, "line": s.line, "end_line": s.end_line,
                    "loc": s.end_line - s.line + 1, "signature": s.signature, "kind": s.kind,
                }
                if label == G.FUNCTION:
                    props["function_name"] = s.name
                    props["return_type"] = s.return_type
                if s.docstring:
                    props["summary"] = s.docstring
                if s.decorators:
                    props["decorators"] = s.decorators[:6]
                if s.is_async:
                    props["async"] = True
                if s.bases:
                    props["bases"] = s.bases
                self.g.add_node(Node(nid, label, s.name, f.rel, props))
                f.sym_ids.setdefault(s.qualname, []).append(nid)
            # containment: method -> class, nested -> outer, top-level -> file
            for s in p.symbols:
                ids = f.sym_ids.get(s.qualname) or []
                for nid in ids:
                    if nid == f.node_id or self.g.nodes[nid].label == G.ENDPOINT:
                        continue
                    parent_q = s.qualname.rpartition(".")[0]
                    parent_ids = f.sym_ids.get(parent_q) if parent_q else None
                    parent = parent_ids[0] if parent_ids else f.node_id
                    if self.g.nodes.get(parent) is None or self.g.nodes[parent].label == G.ENDPOINT:
                        parent = f.node_id
                    self.g.add_edge(nid, parent, G.BELONGS_TO)

    def _add_endpoint(self, method: str, route: str, f: FileInfo, line: int, framework: str,
                      handler: str | None, doc: str = "", sig: str = "") -> str:
        route = route or "/"
        eid = f"APIENDPOINT:{route}:{method}"
        props = {"endpoint_name": route, "http_method": method, "route": route, "framework": framework,
                 "file_path": f.rel, "line": line, "normalized": normalize_route(route)}
        if handler:
            props["handler"] = handler
        if doc:
            props["summary"] = doc
        if sig:
            props["signature"] = sig
        is_new = eid not in self.g.nodes
        self.g.add_node(Node(eid, G.ENDPOINT, f"{method} {route}", f.rel, props))
        if is_new:
            self.endpoint_index[(props["normalized"], method)].append(eid)
            self.endpoint_by_route[props["normalized"]].append(eid)
        self.g.add_edge(eid, f.node_id, G.BELONGS_TO)
        return eid

    # ── 4. imports ─────────────────────────────────────────────────────────
    def resolve_imports(self) -> None:
        for f in self.files.values():
            p = f.parse
            if p is None:
                continue
            for imp in p.imports:
                target = self._resolve_module(imp.module, f)
                if f.language == "python":
                    if not imp.names:  # import a.b [as c]
                        if target:
                            f.module_aliases[imp.alias or imp.module] = target
                    else:
                        for local, orig in imp.names.items():
                            if orig == "*":
                                if target:
                                    f.star_imports.append(target)
                                continue
                            sub = self._resolve_module(f"{imp.module}.{orig}" if not imp.module.endswith(".") else imp.module + orig, f)
                            if sub:  # from pkg import module
                                f.module_aliases[local] = sub
                                self.g.add_edge(f.node_id, self.files[sub].node_id, G.IMPORTS)
                            elif target:
                                f.imported[local] = (target, orig)
                else:
                    if not target:
                        continue
                    if imp.alias:
                        f.module_aliases[imp.alias] = target
                    for local, orig in imp.names.items():
                        if orig == "*":
                            f.star_imports.append(target)
                        else:
                            f.imported[local] = (target, orig)
                if target and target != f.rel:
                    self.g.add_edge(f.node_id, self.files[target].node_id, G.IMPORTS)

    def _resolve_module(self, spec: str, f: FileInfo) -> str | None:
        if not spec:
            return None
        if f.language == "python":
            return self._resolve_py(spec, f.rel)
        return self._resolve_js(spec, f.rel)

    def _resolve_py(self, mod: str, from_rel: str) -> str | None:
        if mod.startswith("."):
            level = len(mod) - len(mod.lstrip("."))
            rest = mod[level:]
            pkg = posixpath.dirname(from_rel).split("/") if posixpath.dirname(from_rel) else []
            if level > 1:
                pkg = pkg[: len(pkg) - (level - 1)] if len(pkg) >= level - 1 else []
            full = ".".join([*pkg, *([rest] if rest else [])])
            return self.py_full.get(full)
        if mod in self.py_full:
            return self.py_full[mod]
        cands = self.py_suffix.get(mod)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        here = posixpath.dirname(from_rel)
        return max(cands, key=lambda c: len(posixpath.commonprefix([posixpath.dirname(c), here])))

    def _resolve_js(self, spec: str, from_rel: str) -> str | None:
        bases: list[str] = []
        if spec.startswith("."):
            bases.append(posixpath.normpath(posixpath.join(posixpath.dirname(from_rel), spec)))
        else:
            for cfg_dir, base_dir, paths in self._tsconfigs_for(from_rel):
                for pattern, targets in paths.items():
                    if pattern.endswith("*"):
                        pre = pattern[:-1]
                        if not spec.startswith(pre):
                            continue
                        tail = spec[len(pre):]
                    elif pattern == spec:
                        tail = ""
                    else:
                        continue
                    for tgt in targets:
                        t = tgt.replace("*", tail)
                        bases.append(posixpath.normpath(posixpath.join(base_dir, t)))
            if spec.startswith("/"):
                bases.append(spec.lstrip("/"))
        for b in bases:
            hit = self._probe(b)
            if hit:
                return hit
        return None

    def _tsconfigs_for(self, rel: str):
        matches = [c for c in self.ts_configs if not c[0] or rel.startswith(c[0] + "/")]
        matches.sort(key=lambda c: -len(c[0]))
        return matches or self.ts_configs[:1]

    def _probe(self, base: str) -> str | None:
        base = base.lstrip("./") if base.startswith("./") else base
        if base in self.files:
            return base
        stem = re.sub(r"\.(m|c)?jsx?$", "", base)
        for cand_base in dict.fromkeys([base, stem]):
            for ext in JS_EXTS:
                if cand_base + ext in self.files:
                    return cand_base + ext
            for ext in JS_EXTS:
                c = f"{cand_base}/index{ext}"
                if c in self.files:
                    return c
        return None

    # ── 5. calls, inheritance, http ────────────────────────────────────────
    def resolve_calls(self) -> None:
        for f in self.files.values():
            p = f.parse
            if p is None:
                continue
            is_test = f.label == G.TEST_FILE
            for call in p.calls:
                sources = self._caller_ids(f, call.caller)
                targets = self._resolve_call(f, call.name, call.obj, call.caller)
                for s in sources:
                    for t in targets:
                        if s == t:
                            continue
                        rel = G.TEST if is_test else G.CALLS
                        src = f.node_id if is_test else s
                        if self.g.add_edge(src, t, rel, count=1):
                            self.counters[f"edges_{rel}"] += 1
            for s in p.symbols:
                if s.kind != "class" or not s.bases:
                    continue
                for cid in f.sym_ids.get(s.qualname, []):
                    for b in s.bases:
                        obj, _, nm = b.rpartition(".")
                        for t in self._resolve_name(f, nm, obj or None):
                            if self.g.nodes[t].label == G.CLASS:
                                self.g.add_edge(cid, t, G.EXTENDS)
            for hc in p.http_calls:
                # tests hitting routes we can't find aren't worth a placeholder node
                targets, how = self._match_endpoint(hc.method, hc.url, f, create=not is_test)
                sources = [f.node_id] if is_test else self._caller_ids(f, hc.caller)
                for s in sources:
                    for t in targets:
                        rel = G.TEST if is_test else (hc.method if hc.method in G.HTTP_METHODS else "ANY")
                        self.g.add_edge(s, t, rel, count=1, client=hc.client, match=how, url=hc.url[:200])

    def _caller_ids(self, f: FileInfo, caller: str | None) -> list[str]:
        if caller is None:
            return [f.node_id]
        q = caller
        while q:
            ids = f.sym_ids.get(q)
            if ids:
                return ids
            q = q.rpartition(".")[0]
        return [f.node_id]

    def _enclosing_class(self, f: FileInfo, caller: str | None) -> str | None:
        if not caller or f.parse is None:
            return None
        classes = {s.qualname for s in f.parse.symbols if s.kind == "class"}
        q = caller
        while q:
            q = q.rpartition(".")[0]
            if q in classes:
                return q
        return None

    def _resolve_call(self, f: FileInfo, name: str, obj: str | None, caller: str | None) -> list[str]:
        if obj in ("self", "this", "cls"):
            cls = self._enclosing_class(f, caller)
            if cls:
                return self._method_in_class(f, cls, name, depth=0)
            if caller and "." in caller:  # app.handle = function () { this.lazyrouter() }
                hit = f.sym_ids.get(f"{caller.rpartition('.')[0]}.{name}")
                if hit:
                    return hit
            return []
        if obj and obj.startswith("super"):
            cls = self._enclosing_class(f, caller)
            if cls:
                out: list[str] = []
                for base in self._class_bases(f, cls):
                    out += self._method_of_class_node(base, name, 0)
                return out
            return []
        return self._resolve_name(f, name, obj)

    def _resolve_name(self, f: FileInfo, name: str, obj: str | None, hops: int = 0) -> list[str]:
        if hops > 4:
            return []
        if obj is None:
            ids = f.sym_ids.get(name)
            if ids:
                return [i for i in ids if i != f.node_id]
            if name in f.imported:
                tgt_rel, orig = f.imported[name]
                return self._export_of(tgt_rel, orig, hops + 1)
            for star in f.star_imports:
                hit = self._export_of(star, name, hops + 1)
                if hit:
                    return hit
            return []
        # obj.name(...)
        direct = f.sym_ids.get(f"{obj}.{name}")  # app.handle = function () {} ; app.handle()
        if direct:
            return [i for i in direct if i != f.node_id]
        ctor = re.match(r"^(?:new\s+)?([A-Za-z_$][\w$]*)\(.*\)$", obj, re.S)  # UserService(db).fetch()
        if ctor:
            cls_ids = [i for i in self._resolve_name(f, ctor.group(1), None, hops + 1) if self.g.nodes[i].label == G.CLASS]
            return [m for c in cls_ids for m in self._method_of_class_node(c, name, 0)]
        if obj in f.module_aliases:
            return self._export_of(f.module_aliases[obj], name, hops + 1)
        head = obj.split(".")[0]
        if obj in f.sym_ids:  # Class.static() or local instance
            cls_ids = [i for i in f.sym_ids[obj] if self.g.nodes.get(i) and self.g.nodes[i].label == G.CLASS]
            if cls_ids:
                return [m for c in cls_ids for m in self._method_of_class_node(c, name, 0)]
        if f.parse and obj in f.parse.instances:
            cls_name = f.parse.instances[obj]
            cls_ids = [i for i in self._resolve_name(f, cls_name, None, hops + 1) if self.g.nodes[i].label == G.CLASS]
            return [m for c in cls_ids for m in self._method_of_class_node(c, name, 0)]
        if obj in f.imported:
            tgt_rel, orig = f.imported[obj]
            tgt = self.files.get(tgt_rel)
            if tgt and tgt.parse:
                if orig in tgt.parse.instances:  # from utils.neodb import app ; app.create_relation()
                    cls_ids = [i for i in self._resolve_name(tgt, tgt.parse.instances[orig], None, hops + 1)
                               if self.g.nodes[i].label == G.CLASS]
                    return [m for c in cls_ids for m in self._method_of_class_node(c, name, 0)]
                cls_ids = [i for i in self._export_of(tgt_rel, orig, hops + 1) if self.g.nodes[i].label == G.CLASS]
                if cls_ids:
                    return [m for c in cls_ids for m in self._method_of_class_node(c, name, 0)]
        if "." in obj and head in f.module_aliases:  # import a ; a.b.c()
            return []
        return []

    def _export_of(self, rel: str, name: str, hops: int) -> list[str]:
        tgt = self.files.get(rel)
        if tgt is None:
            return []
        if name == "default":
            if tgt.parse and tgt.parse.default_export:
                name = tgt.parse.default_export
            else:
                return []
        ids = tgt.sym_ids.get(name)
        if ids:
            return [i for i in ids if i != tgt.node_id]
        # re-exports: from .x import name / export { name } from './x'
        return self._resolve_name(tgt, name, None, hops + 1) if hops <= 4 else []

    def _class_bases(self, f: FileInfo, cls_qual: str) -> list[str]:
        out = []
        for cid in f.sym_ids.get(cls_qual, []):
            for t, r in self.g.out_adj.get(cid, []):
                if r == G.EXTENDS:
                    out.append(t)
        if not out and f.parse:  # EXTENDS edges may not exist yet; resolve on the fly
            for s in f.parse.symbols:
                if s.qualname == cls_qual:
                    for b in s.bases:
                        o, _, nm = b.rpartition(".")
                        out += [i for i in self._resolve_name(f, nm, o or None) if self.g.nodes[i].label == G.CLASS]
        return out

    def _method_in_class(self, f: FileInfo, cls_qual: str, name: str, depth: int) -> list[str]:
        ids = f.sym_ids.get(f"{cls_qual}.{name}")
        if ids:
            return ids
        if depth > 3:
            return []
        out: list[str] = []
        for base in self._class_bases(f, cls_qual):
            out += self._method_of_class_node(base, name, depth + 1)
        return out

    def _method_of_class_node(self, class_id: str, name: str, depth: int) -> list[str]:
        node = self.g.nodes.get(class_id)
        if node is None:
            return []
        fi = self.files.get(node.path)
        if fi is None:
            return []
        hits = self._method_in_class(fi, node.props.get("qualname", node.name), name, depth)
        return hits or ([class_id] if name in ("__init__", "constructor") else [])

    def _lookup_route(self, norm: str, method: str) -> list[str]:
        hits = self.endpoint_index.get((norm, method)) or self.endpoint_index.get((norm, "ANY"))
        if not hits and len(self.endpoint_by_route.get(norm, [])) == 1:
            hits = self.endpoint_by_route[norm]
        if not hits:
            hits = self._pattern_match(norm, method)
        return hits or []

    def _pattern_match(self, norm: str, method: str) -> list[str]:
        """Concrete URLs (`/items/42`, common in tests) against parameterised routes (`/items/{}`):
        segment-wise, `{}` matches anything; the most specific route (fewest params) wins."""
        if self._routes_by_len is None:
            self._routes_by_len = defaultdict(list)
            for (route, m), ids in self.endpoint_index.items():
                if "{}" in route:
                    self._routes_by_len[route.count("/")].append((route.split("/"), m, ids))
        segs = norm.split("/")
        best: tuple[int, list[str]] | None = None
        for rsegs, m, ids in self._routes_by_len.get(norm.count("/"), []):
            if m not in (method, "ANY"):
                continue
            if all(r == s or r == "{}" for r, s in zip(rsegs, segs)):
                params = rsegs.count("{}")
                if best is None or params < best[0]:
                    best = (params, ids)
        return best[1] if best else []

    def _match_endpoint(self, method: str, url: str, f: FileInfo, create: bool = True) -> tuple[list[str], str]:
        """Return (endpoint ids, how they matched)."""
        norm = normalize_route(url)
        host_m = re.match(r"^(?:[a-z]+:)?//([^/]+)", url.strip("`'\""), re.I)
        host = host_m.group(1) if host_m else ""
        external = bool(host) and not host.startswith(LOCAL_HOSTS)
        if not external:
            hits = self._lookup_route(norm, method)
            if hits:
                return hits, "exact"
            # frontends commonly reach the backend through a proxy/mount prefix
            # (/api/chat -> /chat, /api/v1/items -> /items)
            rest, stripped = norm, []
            for _ in range(2):
                m = re.match(r"^/(api|backend|server|rest|v\d+)(/.+)$", rest)
                if not m:
                    break
                stripped.append(m.group(1))
                rest = m.group(2)
                hits = self._lookup_route(rest, method)
                if hits:
                    return hits, "via /" + "/".join(stripped) + " prefix"
        if norm in ("/", "/{}") or len(norm) < 2 or not create:
            return [], ""
        display = f"{host}{norm}" if external else norm
        eid = f"APIENDPOINT:{display}:{method}"
        if eid not in self.g.nodes:
            self.g.add_node(Node(eid, G.ENDPOINT, f"{method} {display}", "", {
                "endpoint_name": display, "http_method": method, "route": display, "normalized": norm,
                "external": external, "defined_in_repo": False, **({"host": host} if host else {}),
            }))
        return [eid], "unresolved"

    # ── 6. finish ──────────────────────────────────────────────────────────
    def finalize(self, started: float, extra: dict) -> CodeGraph:
        labels = Counter(n.label for n in self.g.nodes.values())
        rels = Counter(r for _, _, r, _ in self.g.edges)
        langs = Counter(f.language for f in self.files.values() if f.language)
        for nid, node in self.g.nodes.items():
            if node.label == G.FOLDER:
                node.props["file_count"] = sum(1 for s, r in self.g.in_adj.get(nid, []) if r == G.BELONGS_TO
                                               and self.g.nodes[s].label in G.FILE_LABELS)
        self.g.stats = {
            "nodes": len(self.g.nodes), "edges": self.g.edge_count,
            "labels": dict(labels), "relationships": dict(rels), "languages": dict(langs),
            "files_parsed": self.counters["files_parsed"], "files_ignored": self.counters["files_ignored"],
            "files_too_large": self.counters["files_too_large"] + extra.get("skipped_large", 0),
            "files_minified": self.counters["files_minified"], "files_failed": self.counters["files_failed"],
            "build_ms": int((time.perf_counter() - started) * 1000),
            **({"genai": self.genai_stats} if self.genai_stats else {}),
        }
        return self.g


def build_graph(root: Path, repo: dict, progress: Callable[[float, str], None] | None = None,
                extra: dict | None = None) -> CodeGraph:
    started = time.perf_counter()
    b = Builder(root, repo, progress)
    b.progress(0.3, "Scanning files")
    b.walk()
    b.parse_all()
    b.progress(0.82, "Resolving symbols")
    b.add_symbols()
    b.resolve_imports()
    b.progress(0.9, "Linking calls")
    b.resolve_calls()
    b.progress(0.95, "Finding agents, services and env vars")
    add_genai_layer(b)
    return b.finalize(started, extra or {})


# ── helpers ────────────────────────────────────────────────────────────────

def _py_module_name(rel: str) -> str:
    if not rel.endswith((".py", ".pyi")):
        return ""
    parts = rel.rsplit(".", 1)[0].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if any(not re.match(r"^[A-Za-z_][\w]*$", p) for p in parts):
        # dirs like "my-app" aren't importable, but keep the importable tail
        while parts and not re.match(r"^[A-Za-z_][\w]*$", parts[0]):
            parts = parts[1:]
    return ".".join(parts)


def _framework(f: FileInfo) -> str:
    mods = [i.module.lower() for i in f.parse.imports] if f.parse else []
    table = PY_FRAMEWORKS if f.language == "python" else JS_FRAMEWORKS
    for needle, name in table:
        if any(m == needle or m.startswith(needle + ".") or m.startswith(needle + "/") for m in mods):
            return name
    return ""


_NEXT_APP_ROUTE = re.compile(r"(?:^|/)app/(?P<route>(?:.+/)?)route\.(?:m|c)?[jt]sx?$")
_NEXT_PAGES_API = re.compile(r"(?:^|/)pages/(?P<route>api(?:/.+)?)\.(?:m|c)?[jt]sx?$")


def _next_segments(path: str) -> str:
    segs = []
    for seg in path.strip("/").split("/"):
        if not seg or (seg.startswith("(") and seg.endswith(")")) or seg.startswith("@"):
            continue
        m = re.match(r"^\[\[?(?:\.\.\.)?([^\]]+)\]\]?$", seg)
        segs.append("{" + m.group(1) + "}" if m else seg)
    return "/" + "/".join(segs)


def _nextjs_route(rel: str) -> str | None:
    m = _NEXT_APP_ROUTE.search(rel)
    return _next_segments(m.group("route")) if m else None


def _pages_api_route(rel: str) -> str | None:
    m = _NEXT_PAGES_API.search(rel)
    if not m:
        return None
    r = _next_segments(m.group("route"))
    return r[: -len("/index")] if r.endswith("/index") else r


def _looks_minified(data: bytes) -> bool:
    if len(data) < 20_000:
        return False
    lines = data.count(b"\n") + 1
    return len(data) / lines > 400


def _strip_jsonc(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))
