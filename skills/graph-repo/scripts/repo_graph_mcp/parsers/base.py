"""Language-neutral parse results. Parsers fill these; the builder resolves them."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Symbol:
    kind: str  # "function" | "class" | "method"
    name: str
    qualname: str  # e.g. "MyClass.method" or "outer.inner"
    line: int
    end_line: int
    signature: str = ""
    return_type: str = ""
    docstring: str = ""
    decorators: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)  # classes only
    is_async: bool = False
    exported: bool = False
    body_hash: str = ""  # fingerprint of its source lines, for telling what changed between builds


@dataclass
class Call:
    caller: str | None  # qualname of enclosing symbol; None = module level
    name: str  # called name ("foo" in foo() / obj.foo())
    obj: str | None = None  # receiver text ("self", "utils", "app.db"), None for bare calls
    line: int = 0


@dataclass
class Import:
    module: str  # as written: "utils.neodb", ".x", "./util", "@/lib/api"
    names: dict[str, str] = field(default_factory=dict)  # local alias -> original exported name ("*" / "default")
    alias: str | None = None  # `import a.b as ab` / `import * as ns from` / `const x = require()`
    line: int = 0


@dataclass
class Endpoint:
    method: str
    route: str
    handler: str | None  # qualname of handler symbol when it is a named function
    line: int
    framework: str
    inline_scope: str | None = None  # synthetic caller name for calls inside an inline handler


@dataclass
class HttpCall:
    caller: str | None
    method: str
    url: str
    line: int
    client: str


@dataclass
class FileParse:
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    http_calls: list[HttpCall] = field(default_factory=list)
    instances: dict[str, str] = field(default_factory=dict)  # module var -> class name it was built from
    route_prefixes: dict[str, str] = field(default_factory=dict)  # router var -> prefix
    module_doc: str = ""
    default_export: str | None = None  # JS/TS: local name bound to `export default`
    framework: str = ""
    loc: int = 0
    error: str | None = None


def first_doc_line(text: str, limit: int = 160) -> str:
    if not text:
        return ""
    t = text.strip()
    for q in ('"""', "'''", '"', "'"):
        if t.startswith(q) and t.endswith(q) and len(t) >= 2 * len(q):
            t = t[len(q):-len(q)]
            break
    t = t.lstrip("/*! \n").rstrip("*/ \n")
    for line in t.splitlines():
        line = line.strip().lstrip("*").strip()
        if line and not line.startswith("@"):
            return line[:limit]
    return ""


_PARAM_RE = re.compile(r"\$\{[^}]*\}|\{[^}]*\}|<[^>]*>|:[A-Za-z_]\w*|\[\[?\.{0,3}[^\]]*\]\]?")


def normalize_route(route: str) -> str:
    """Canonical form used to match client calls to server routes:
    host/query stripped, path params collapsed to {}, no trailing slash."""
    r = route.strip().strip("`'\"")
    r = re.sub(r"^[a-z]+://[^/]+", "", r, flags=re.I)  # scheme+host
    r = re.sub(r"^\$\{[^}]*\}", "", r)  # `${BASE_URL}/x`
    r = re.sub(r"^\{[^}]*\}", "", r)  # f"{BASE}/x"
    r = r.split("?", 1)[0].split("#", 1)[0]
    r = _PARAM_RE.sub("{}", r)
    if not r.startswith("/"):
        r = "/" + r
    r = re.sub(r"/{2,}", "/", r)
    if len(r) > 1:
        r = r.rstrip("/")
    return r or "/"


def looks_like_route(value: str) -> bool:
    v = value.strip()
    if not v or len(v) > 300 or "\n" in v:
        return False
    return v.startswith("/") or bool(re.match(r"^(https?:)?//", v)) or v.startswith("${") or v.startswith("{")
