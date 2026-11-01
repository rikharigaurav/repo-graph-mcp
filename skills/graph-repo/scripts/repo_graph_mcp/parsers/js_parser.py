"""JavaScript / TypeScript / JSX / TSX structure extraction with tree-sitter."""

from __future__ import annotations

import re

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from .base import Call, Endpoint, FileParse, HttpCall, Import, Symbol, first_doc_line, looks_like_route

LANGS = {
    "javascript": Language(tsjs.language()),
    "typescript": Language(tsts.language_typescript()),
    "tsx": Language(tsts.language_tsx()),
}

HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}
ROUTE_VERBS = HTTP_VERBS | {"all"}
CLIENT_OBJS = {"axios", "api", "http", "client", "$http", "ky", "superagent", "httpclient", "apiclient",
               "instance", "request", "fetcher", "agent", "got", "needle"}
CLIENT_RE = re.compile(r"(client|axios|api\b|http|fetcher|instance|agent)", re.I)
ROUTER_RE = re.compile(r"^(app|router|server|route|routes|r|api_?router|fastify|hono|express|koa)$|router$|app$", re.I)
FUNC_TYPES = {"arrow_function", "function_expression", "function", "generator_function"}
CLASS_TYPES = {"class_declaration", "abstract_class_declaration", "class"}


def _t(node: Node | None) -> str:
    return node.text.decode("utf8", "replace") if node is not None and node.text is not None else ""


def string_value(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return "".join(_t(c) for c in node.named_children if c.type in ("string_fragment", "escape_sequence"))
    if node.type == "template_string":
        out = []
        for c in node.named_children:
            if c.type == "string_fragment":
                out.append(_t(c))
            elif c.type == "template_substitution":
                out.append("{}")
            elif c.type == "escape_sequence":
                out.append(_t(c))
        return "".join(out)
    if node.type == "binary_expression":
        lv = string_value(node.child_by_field_name("left"))
        rv = string_value(node.child_by_field_name("right"))
        if lv is None and rv is None:
            return None
        return (lv if lv is not None else "{}") + (rv if rv is not None else "{}")
    if node.type == "parenthesized_expression" and node.named_child_count == 1:
        return string_value(node.named_children[0])
    return None


def _jsdoc(node: Node) -> str:
    target = node.parent if node.parent is not None and node.parent.type == "export_statement" else node
    if target.parent is not None and target.parent.type in ("variable_declarator",):
        target = target.parent.parent or target
        if target.parent is not None and target.parent.type == "export_statement":
            target = target.parent
    prev = target.prev_named_sibling
    if prev is not None and prev.type == "comment" and _t(prev).startswith("/**") and prev.end_point[0] >= target.start_point[0] - 1:
        return first_doc_line(_t(prev))
    return ""


def _object_prop(obj: Node | None, key: str) -> Node | None:
    if obj is None or obj.type != "object":
        return None
    for p in obj.named_children:
        if p.type == "pair" and _t(p.child_by_field_name("key")).strip("'\"") == key:
            return p.child_by_field_name("value")
        if p.type == "shorthand_property_identifier" and _t(p) == key:
            return p
    return None


def parse_js(source: bytes, language: str) -> FileParse:
    parser = Parser(LANGS[language])
    tree = parser.parse(source)
    root = tree.root_node
    res = FileParse(language=language, loc=source.count(b"\n") + 1)
    if root.has_error:
        res.error = "syntax errors (partial parse)"
    for c in root.named_children:
        if c.type == "comment":
            if _t(c).startswith("/**"):
                res.module_doc = first_doc_line(_t(c))
            continue
        break

    stack: list[tuple[Node, tuple[tuple[str, str], ...]]] = [(c, ()) for c in reversed(root.children)]
    while stack:
        node, scope = stack.pop()
        t = node.type

        if t == "import_statement":
            _import_statement(node, res)
            continue
        if t == "export_statement":
            src = node.child_by_field_name("source")
            if src is not None:  # export { x } from './y'  /  export * from './y'
                res.imports.append(Import(module=string_value(src) or "", names={"*": "*"}, line=node.start_point[0] + 1))
                continue
            if any(c.type == "default" for c in node.children):
                decl = node.child_by_field_name("declaration") or node.child_by_field_name("value")
                if decl is not None:
                    nm = decl.child_by_field_name("name")
                    if nm is not None:
                        res.default_export = _t(nm)
                    elif decl.type == "identifier":
                        res.default_export = _t(decl)
            stack.extend((c, scope) for c in reversed(node.children))
            continue

        if t in ("function_declaration", "generator_function_declaration") or t in CLASS_TYPES and node.child_by_field_name("name") is not None:
            name = _t(node.child_by_field_name("name"))
            new_scope = _add_symbol(node, name, scope, res)
            body = node.child_by_field_name("body")
            if body is not None:
                stack.extend((c, new_scope) for c in reversed(body.children))
            continue

        if t == "method_definition":
            name = _t(node.child_by_field_name("name"))
            new_scope = _add_symbol(node, name, scope, res, kind="method")
            body = node.child_by_field_name("body")
            if body is not None:
                stack.extend((c, new_scope) for c in reversed(body.children))
            continue

        if t == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if value is not None and name_node is not None and name_node.type == "identifier":
                if value.type in FUNC_TYPES:
                    new_scope = _add_symbol(value, _t(name_node), scope, res, decl=node)
                    body = value.child_by_field_name("body")
                    if body is not None:
                        stack.append((body, new_scope))
                    continue
                if value.type == "call_expression" and not scope:
                    fn = _t(value.child_by_field_name("function"))
                    cls = fn.split(".")[-1]
                    if fn == "require":
                        pass
                    elif cls[:1].isupper():
                        res.instances[_t(name_node)] = cls
                if value.type == "new_expression" and not scope:
                    ctor = _t(value.child_by_field_name("constructor")).split(".")[-1]
                    if ctor:
                        res.instances[_t(name_node)] = ctor

        if t == "assignment_expression":
            # CommonJS / prototype style: exports.foo = function () {}, app.handle = function handle() {}
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and left.type == "member_expression" and right.type in FUNC_TYPES:
                obj = _t(left.child_by_field_name("object"))
                prop = _t(left.child_by_field_name("property"))
                if prop and len(obj) < 60:
                    qual = prop if obj in ("exports", "module.exports") else f"{obj.replace('.prototype', '')}.{prop}"
                    new_scope = _add_symbol(right, prop, (), res, decl=node)
                    if qual != prop:
                        res.symbols[-1].qualname = qual
                        new_scope = scope + (("function", qual),)
                    body = right.child_by_field_name("body")
                    if body is not None:
                        stack.append((body, new_scope))
                    continue

        if t == "object" and node.named_child_count >= 2:
            # generated API clients: request({ method: "GET", url: "/api/v1/items/" })
            url = string_value(_object_prop(node, "url"))
            method = string_value(_object_prop(node, "method"))
            if url is not None and method and looks_like_route(url) and method.upper() in {m.upper() for m in HTTP_VERBS}:
                res.http_calls.append(HttpCall(caller=_caller(scope), method=method.upper(), url=url,
                                               line=node.start_point[0] + 1, client="request-object"))

        if t == "call_expression":
            if _call(node, scope, res, stack):
                continue

        if t in ("jsx_opening_element", "jsx_self_closing_element"):
            nm = node.child_by_field_name("name")
            text = _t(nm)
            if text[:1].isupper():
                obj, _, prop = text.rpartition(".")
                res.calls.append(Call(caller=_caller(scope), name=prop or text, obj=obj or None, line=node.start_point[0] + 1))

        stack.extend((c, scope) for c in reversed(node.children))

    return res


def _caller(scope) -> str | None:
    return scope[-1][1] if scope else None


def _add_symbol(node: Node, name: str, scope, res: FileParse, kind: str | None = None, decl: Node | None = None):
    parent_q = scope[-1][1] if scope else ""
    if parent_q.startswith("<endpoint"):
        parent_q = ""
    qual = f"{parent_q}.{name}" if parent_q else name
    is_class = node.type in CLASS_TYPES
    if kind is None:
        kind = "class" if is_class else ("method" if scope and scope[-1][0] == "class" else "function")
    params = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
    ret = node.child_by_field_name("return_type")
    bases: list[str] = []
    if is_class:
        for c in node.named_children:
            if c.type == "class_heritage":
                for h in c.named_children:
                    if h.type == "extends_clause":
                        v = h.child_by_field_name("value") or (h.named_children[0] if h.named_child_count else None)
                        if v is not None:
                            bases.append(_t(v))
                    elif h.type in ("identifier", "member_expression"):
                        bases.append(_t(h))
        sig = f"class {name}" + (f" extends {', '.join(bases)}" if bases else "")
    else:
        p = " ".join(_t(params).split()) if params is not None else "()"
        if not p.startswith("("):
            p = f"({p})"
        if len(p) > 180:
            p = p[:177] + "…)"
        sig = f"{name}{p}" + (_t(ret) if ret is not None else "")
    anchor = decl or node
    exported = anchor.parent is not None and (
        anchor.parent.type == "export_statement"
        or (anchor.parent.parent is not None and anchor.parent.parent.type == "export_statement")
    )
    res.symbols.append(Symbol(
        kind=kind, name=name, qualname=qual,
        line=anchor.start_point[0] + 1, end_line=node.end_point[0] + 1,
        signature=sig, return_type=_t(ret).lstrip(": ").strip() if ret is not None else "",
        docstring=_jsdoc(node), bases=bases,
        is_async=any(c.type == "async" for c in node.children),
        exported=exported,
    ))
    return scope + (("class" if is_class else "function", qual),)


def _import_statement(node: Node, res: FileParse) -> None:
    src = node.child_by_field_name("source")
    module = string_value(src) or ""
    imp = Import(module=module, line=node.start_point[0] + 1)
    for c in node.named_children:
        if c.type != "import_clause":
            continue
        for part in c.named_children:
            if part.type == "identifier":
                imp.names[_t(part)] = "default"
            elif part.type == "namespace_import":
                ids = [x for x in part.named_children if x.type == "identifier"]
                if ids:
                    imp.alias = _t(ids[-1])
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type != "import_specifier":
                        continue
                    orig = _t(spec.child_by_field_name("name"))
                    alias = spec.child_by_field_name("alias")
                    imp.names[_t(alias) if alias is not None else orig] = orig
    res.imports.append(imp)


def _call(node: Node, scope, res: FileParse, stack) -> bool:
    """Record a call. Returns True when children were already scheduled."""
    fn = node.child_by_field_name("function")
    args_node = node.child_by_field_name("arguments")
    args = [a for a in (args_node.named_children if args_node is not None else []) if a.type != "comment"]
    line = node.start_point[0] + 1
    caller = _caller(scope)
    # TS parses `await axios.post<T>(…)` as call(await(member), type_args, args)
    while fn is not None and fn.type in ("await_expression", "parenthesized_expression", "non_null_expression") and fn.named_child_count:
        fn = fn.named_children[-1] if fn.type != "await_expression" else fn.named_children[0]
    if fn is None:
        return False

    # require('./x') / import('./x')
    if fn.type in ("identifier", "import") and _t(fn) in ("require", "import") and args:
        mod = string_value(args[0])
        if mod is not None:
            imp = Import(module=mod, line=line)
            parent = node.parent
            if parent is not None and parent.type == "await_expression":
                parent = parent.parent
            if parent is not None and parent.type == "variable_declarator":
                nm = parent.child_by_field_name("name")
                if nm is not None and nm.type == "identifier":
                    imp.alias = _t(nm)
                elif nm is not None and nm.type == "object_pattern":
                    for p in nm.named_children:
                        if p.type == "shorthand_property_identifier_pattern":
                            imp.names[_t(p)] = _t(p)
                        elif p.type == "pair_pattern":
                            imp.names[_t(p.child_by_field_name("value"))] = _t(p.child_by_field_name("key"))
            res.imports.append(imp)
        return False

    if fn.type == "identifier":
        name = _t(fn)
        if name == "fetch" and args:
            url = string_value(args[0])
            if url is not None and looks_like_route(url):
                method_node = _object_prop(args[1] if len(args) > 1 else None, "method")
                method = (string_value(method_node) or "GET").upper()
                res.http_calls.append(HttpCall(caller=caller, method=method, url=url, line=line, client="fetch"))
        elif name in ("axios", "request", "ky") and args:
            # axios({ url, method }) or axios('/x')
            url_node = _object_prop(args[0], "url") if args[0].type == "object" else args[0]
            url = string_value(url_node)
            if url is not None and looks_like_route(url):
                method = (string_value(_object_prop(args[0], "method")) or "GET").upper() if args[0].type == "object" else "GET"
                res.http_calls.append(HttpCall(caller=caller, method=method, url=url, line=line, client=name))
        res.calls.append(Call(caller=caller, name=name, line=line))
        return False

    if fn.type == "member_expression":
        obj = fn.child_by_field_name("object")
        prop = _t(fn.child_by_field_name("property"))
        obj_text = _t(obj)
        recv = obj_text.split(".")[-1].lower()
        first = string_value(args[0]) if args else None
        if first is None and args and args[0].type == "object":  # client.post({ url: "/x" })
            first = string_value(_object_prop(args[0], "url"))
        is_client = recv in CLIENT_OBJS or bool(CLIENT_RE.search(obj_text)) or (
            obj is not None and obj.type == "call_expression" and _t(obj.child_by_field_name("function")) in ("request", "supertest")
        )
        if prop in HTTP_VERBS and first is not None and looks_like_route(first) and is_client:
            res.http_calls.append(HttpCall(caller=caller, method=prop.upper(), url=first, line=line, client=recv))
        elif (
            prop in ROUTE_VERBS and first is not None and (first.startswith("/") or first == "*")
            and len(args) >= 2 and ROUTER_RE.search(obj_text.split(".")[-1] or "")
        ):
            handler_arg = args[-1]
            method = "ANY" if prop == "all" else prop.upper()
            synth = f"<endpoint {method} {first}>"
            ep = Endpoint(method=method, route=first, handler=None, line=line, framework="express-style", inline_scope=synth)
            if handler_arg.type in ("identifier", "member_expression"):
                h = _t(handler_arg)
                hobj, _, hname = h.rpartition(".")
                res.calls.append(Call(caller=synth, name=hname or h, obj=hobj or None, line=line))
            res.endpoints.append(ep)
            # calls inside inline handlers / middleware belong to the endpoint
            for a in args[1:]:
                stack.append((a, scope + (("function", synth),)))
            return True
        res.calls.append(Call(caller=caller, name=prop, obj=obj_text, line=line))
    return False
