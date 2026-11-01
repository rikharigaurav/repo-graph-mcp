"""Python structure extraction with tree-sitter (no LLM).

Covers what language/tree_python.py in Read-Code-Structure extracted —
functions, API endpoint decorators, HTTP client calls, from-imports, call
sites — plus classes/methods, bases, docstrings, `import x as y`, relative
imports, module-level instances (`app = App()`) and router prefixes.
"""

from __future__ import annotations

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from .base import Call, Endpoint, FileParse, HttpCall, Import, Symbol, first_doc_line, looks_like_route

PY = Language(tspython.language())

ROUTE_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "route", "api_route", "websocket"}
HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}
CLIENT_NAMES = {"requests", "httpx", "session", "client", "api", "http", "aiohttp", "sess", "s", "async_client"}
ROUTER_FACTORIES = {"APIRouter": "prefix", "Blueprint": "url_prefix", "Router": "prefix"}


def _t(node: Node | None) -> str:
    return node.text.decode("utf8", "replace") if node is not None and node.text is not None else ""


def string_value(node: Node | None) -> str | None:
    """Literal value of a string-ish expression; interpolations become {}."""
    if node is None:
        return None
    if node.type == "string":
        out = []
        for ch in node.children:
            if ch.type == "string_content":
                out.append(_t(ch))
            elif ch.type == "interpolation":
                out.append("{}")
            elif ch.type == "escape_sequence":
                out.append(_t(ch))
        return "".join(out)
    if node.type == "concatenated_string":
        parts = [string_value(c) for c in node.named_children]
        return "".join(p for p in parts if p is not None)
    if node.type == "binary_operator":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        lv = string_value(left)
        rv = string_value(right)
        return (lv if lv is not None else "{}") + (rv if rv is not None else "{}")
    if node.type == "parenthesized_expression" and node.named_child_count == 1:
        return string_value(node.named_children[0])
    return None


def _args(call: Node) -> tuple[list[Node], dict[str, Node]]:
    pos: list[Node] = []
    kw: dict[str, Node] = {}
    args = call.child_by_field_name("arguments")
    if args is None:
        return pos, kw
    for a in args.named_children:
        if a.type == "keyword_argument":
            k = a.child_by_field_name("name")
            v = a.child_by_field_name("value")
            if k is not None and v is not None:
                kw[_t(k)] = v
        elif a.type not in ("comment", "list_splat", "dictionary_splat"):
            pos.append(a)
    return pos, kw


def _docstring(body: Node | None) -> str:
    if body is None:
        return ""
    for stmt in body.named_children:
        if stmt.type == "comment":
            continue
        if stmt.type == "expression_statement" and stmt.named_child_count and stmt.named_children[0].type == "string":
            return first_doc_line(string_value(stmt.named_children[0]) or "")
        return ""
    return ""


def parse_python(source: bytes) -> FileParse:
    parser = Parser(PY)
    tree = parser.parse(source)
    root = tree.root_node
    res = FileParse(language="python", loc=source.count(b"\n") + 1)
    if root.has_error:
        res.error = "syntax errors (partial parse)"
    res.module_doc = _docstring(root)

    # iterative walk: (node, scope) where scope is a tuple of (kind, qualname)
    stack: list[tuple[Node, tuple[tuple[str, str], ...], list[str] | None]] = [
        (c, (), None) for c in reversed(root.children)
    ]
    while stack:
        node, scope, decorators = stack.pop()
        t = node.type

        if t == "decorated_definition":
            decos = [c for c in node.children if c.type == "decorator"]
            definition = node.child_by_field_name("definition")
            if definition is not None:
                _check_endpoint(definition, decos, scope, res)
                stack.append((definition, scope, [_t(d) for d in decos]))
            continue

        if t in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _t(name_node)
            parent_q = scope[-1][1] if scope else ""
            qual = f"{parent_q}.{name}" if parent_q else name
            body = node.child_by_field_name("body")
            if t == "function_definition":
                params = node.child_by_field_name("parameters")
                ret = node.child_by_field_name("return_type")
                kind = "method" if scope and scope[-1][0] == "class" else "function"
                sig = " ".join(_t(params).split())
                if len(sig) > 180:
                    sig = sig[:177] + "…)"
                res.symbols.append(Symbol(
                    kind=kind, name=name, qualname=qual,
                    line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                    signature=f"{name}{sig}" + (f" -> {_t(ret)}" if ret is not None else ""),
                    return_type=_t(ret), docstring=_docstring(body), decorators=decorators or [],
                    is_async=any(c.type == "async" for c in node.children),
                    exported=not name.startswith("_") and not scope,
                ))
                new_scope = scope + (("function", qual),)
            else:
                bases = []
                sup = node.child_by_field_name("superclasses")
                if sup is not None:
                    for b in sup.named_children:
                        if b.type in ("identifier", "attribute"):
                            bases.append(_t(b))
                res.symbols.append(Symbol(
                    kind="class", name=name, qualname=qual,
                    line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                    signature=f"class {name}" + (f"({', '.join(bases)})" if bases else ""),
                    docstring=_docstring(body), decorators=decorators or [], bases=bases,
                    exported=not name.startswith("_") and not scope,
                ))
                new_scope = scope + (("class", qual),)
            if body is not None:
                stack.extend((c, new_scope, None) for c in reversed(body.children))
            continue

        if t == "import_statement":
            for c in node.named_children:
                if c.type == "dotted_name":
                    res.imports.append(Import(module=_t(c), line=node.start_point[0] + 1))
                elif c.type == "aliased_import":
                    res.imports.append(Import(
                        module=_t(c.child_by_field_name("name")),
                        alias=_t(c.child_by_field_name("alias")), line=node.start_point[0] + 1,
                    ))
            continue

        if t == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            module = _t(mod_node)
            imp = Import(module=module, line=node.start_point[0] + 1)
            for c in node.named_children:
                if c is mod_node:
                    continue
                if c.type == "dotted_name":
                    imp.names[_t(c).split(".")[-1]] = _t(c)
                elif c.type == "aliased_import":
                    imp.names[_t(c.child_by_field_name("alias"))] = _t(c.child_by_field_name("name"))
                elif c.type == "wildcard_import":
                    imp.names["*"] = "*"
            res.imports.append(imp)
            continue

        if t == "expression_statement" and not scope:
            _module_assignment(node, res)

        if t == "call":
            _record_call(node, scope, res)

        stack.extend((c, scope, None) for c in reversed(node.children))

    return res


def _caller(scope: tuple[tuple[str, str], ...]) -> str | None:
    for kind, qual in reversed(scope):
        if kind == "function":
            return qual
    return scope[-1][1] if scope else None


def _record_call(node: Node, scope, res: FileParse) -> None:
    fn = node.child_by_field_name("function")
    if fn is None:
        return
    line = node.start_point[0] + 1
    caller = _caller(scope)
    if fn.type == "identifier":
        res.calls.append(Call(caller=caller, name=_t(fn), line=line))
    elif fn.type == "attribute":
        obj = fn.child_by_field_name("object")
        attr = _t(fn.child_by_field_name("attribute"))
        obj_text = _t(obj)
        res.calls.append(Call(caller=caller, name=attr, obj=obj_text, line=line))
        recv = obj_text.split(".")[-1].lower()
        if (recv in CLIENT_NAMES or recv.endswith("client") or recv.endswith("session")) and (
            attr in HTTP_VERBS or attr == "request"
        ):
            pos, kw = _args(node)
            method = attr.upper()
            url_node = None
            if attr == "request":
                if pos:
                    method = (string_value(pos[0]) or "ANY").upper()
                url_node = pos[1] if len(pos) > 1 else kw.get("url")
            else:
                url_node = pos[0] if pos else kw.get("url")
            url = string_value(url_node)
            if url is not None and (looks_like_route(url) or url == ""):
                res.http_calls.append(HttpCall(caller=caller, method=method, url=url or "/", line=line, client=recv))


def _module_assignment(stmt: Node, res: FileParse) -> None:
    if not stmt.named_child_count:
        return
    a = stmt.named_children[0]
    if a.type != "assignment":
        return
    left = a.child_by_field_name("left")
    right = a.child_by_field_name("right")
    if left is None or right is None or left.type != "identifier" or right.type != "call":
        return
    fn = right.child_by_field_name("function")
    cls = _t(fn).split(".")[-1]
    if not cls or not cls[0].isupper():
        return
    var = _t(left)
    res.instances[var] = cls
    if cls in ROUTER_FACTORIES:
        _, kw = _args(right)
        prefix = string_value(kw.get(ROUTER_FACTORIES[cls]))
        if prefix:
            res.route_prefixes[var] = prefix


def _check_endpoint(definition: Node, decorators: list[Node], scope, res: FileParse) -> None:
    if definition.type != "function_definition":
        return
    name = _t(definition.child_by_field_name("name"))
    parent_q = scope[-1][1] if scope else ""
    qual = f"{parent_q}.{name}" if parent_q else name
    for d in decorators:
        expr = next((c for c in d.named_children if c.type != "comment"), None)
        if expr is None or expr.type != "call":
            continue
        fn = expr.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            continue
        meth = _t(fn.child_by_field_name("attribute"))
        if meth not in ROUTE_METHODS:
            continue
        router = _t(fn.child_by_field_name("object"))
        pos, kw = _args(expr)
        route = string_value(pos[0]) if pos else string_value(kw.get("path") or kw.get("rule"))
        if route is None or not (route == "" or looks_like_route(route)):
            continue
        prefix = res.route_prefixes.get(router, "")
        full = (prefix.rstrip("/") + "/" + route.lstrip("/")) if prefix else route
        if meth in ("route", "api_route"):
            methods: list[str] = []
            m = kw.get("methods")
            if m is not None and m.type in ("list", "tuple", "set"):
                methods = [(string_value(x) or "").upper() for x in m.named_children]
            methods = [x for x in methods if x] or ["GET"]
            framework = "flask" if meth == "route" else "fastapi"
        elif meth == "websocket":
            methods, framework = ["WS"], "fastapi"
        else:
            methods, framework = [meth.upper()], "fastapi/starlette"
        for method in methods:
            res.endpoints.append(Endpoint(
                method=method, route=full or "/", handler=qual,
                line=definition.start_point[0] + 1, framework=framework,
            ))
