from pathlib import Path

import pytest

from repo_graph_mcp import graph as G
from repo_graph_mcp.builder import build_graph
from repo_graph_mcp.classify import classify
from repo_graph_mcp.fetch import FetchError, parse_repo_spec
from repo_graph_mcp.parsers.base import normalize_route

SAMPLE = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(scope="module")
def g():
    return build_graph(SAMPLE, {"name": "sample_repo"})


def edge(g, rel, src_suffix, dst_suffix):
    return any(r == rel and s.endswith(src_suffix) and t.endswith(dst_suffix) for s, t, r, _ in g.edges)


def test_labels(g):
    labels = g.stats["labels"]
    assert labels[G.SOURCE_FILE] == 7
    assert labels[G.TEST_FILE] == 1
    assert labels[G.DOC_FILE] == 1
    assert labels[G.ENDPOINT] == 3


def test_router_prefix_and_endpoints(g):
    names = sorted(n.name for n in g.nodes.values() if n.label == G.ENDPOINT)
    assert names == ["GET /health", "GET /users/{user_id}", "POST /users/"]
    ep = g.nodes["APIENDPOINT:/users/{user_id}:GET"]
    assert ep.props["framework"] == "fastapi"
    assert ep.props["summary"] == "Return one user."


def test_containment(g):
    assert edge(g, G.BELONGS_TO, "services.py:UserService.fetch", "services.py:UserService")
    assert edge(g, G.BELONGS_TO, "CLASS:backend/app/services.py:UserService", "backend/app/services.py:py")
    assert edge(g, G.BELONGS_TO, "FOLDER:backend/app", "FOLDER:backend")


def test_calls_resolution(g):
    # endpoint handler -> imported function
    assert edge(g, G.CALLS, "/users/:POST", "services.py:normalize")
    # constructor call -> the class; chained call on the new instance -> its method
    assert edge(g, G.CALLS, "/users/{user_id}:GET", "CLASS:backend/app/services.py:UserService")
    assert edge(g, G.CALLS, "/users/{user_id}:GET", "services.py:UserService.fetch")
    assert edge(g, G.CALLS, "/users/:POST", "services.py:UserService.create")
    # self.log -> inherited method on base class
    assert edge(g, G.CALLS, "services.py:UserService.fetch", "services.py:BaseService.log")
    # module-level instance imported from another module: store.ping()
    assert edge(g, G.CALLS, "/health:GET", "db.py:Store.ping")


def test_imports_and_inheritance(g):
    assert edge(g, G.IMPORTS, "backend/app/main.py:py", "backend/app/services.py:py")
    assert edge(g, G.IMPORTS, "web/src/components/UserCard.tsx:tsx", "web/src/api.ts:ts")  # tsconfig @/ alias
    assert edge(g, G.EXTENDS, "CLASS:backend/app/services.py:UserService", "CLASS:backend/app/services.py:BaseService")


def test_jsx_and_http_links(g):
    assert edge(g, G.CALLS, "UserCard.tsx:UserCard", "Avatar.tsx:Avatar")
    assert edge(g, G.CALLS, "UserCard.tsx:UserCard", "web/src/api.ts:loadUser")
    assert edge(g, "GET", "web/src/api.ts:loadUser", "APIENDPOINT:/users/{user_id}:GET")
    assert edge(g, "POST", "web/src/api.ts:createUser", "APIENDPOINT:/users/:POST")


def test_tests_link_to_code(g):
    test_file = "TESTINGFILE:backend/tests/test_users.py:py"
    assert g.nodes[test_file].props["test_framework"] in ("pytest", "None")
    assert edge(g, G.TEST, test_file, "services.py:normalize")
    assert edge(g, G.TEST, test_file, "APIENDPOINT:/health:GET")
    # concrete URL in a test matches the parameterised route
    assert edge(g, G.TEST, test_file, "APIENDPOINT:/users/{user_id}:GET")
    # unmatched test calls don't create placeholder endpoints
    assert not any("/nowhere" in n for n in g.nodes)


def test_queries(g):
    assert g.resolve("normalize").id.endswith("services.py:normalize")
    assert g.resolve("UserService.fetch").label == G.FUNCTION
    sub = g.neighborhood(g.resolve("normalize").id, depth=1, direction="in")
    assert any(n.label == G.ENDPOINT for n in sub.nodes.values())
    payload = g.to_ui_payload()
    assert len(payload["nodes"]) == len(g.nodes) and not payload["truncated"]
    small = g.to_ui_payload(max_nodes=10)
    assert small["truncated"] and len(small["nodes"]) == 10


@pytest.mark.parametrize("path,label", [
    ("src/app.py", G.SOURCE_FILE), ("tests/test_app.py", G.TEST_FILE), ("src/Button.test.tsx", G.TEST_FILE),
    ("templates/index.html", G.TEMPLATE_FILE), ("docs/guide.md", G.DOC_FILE), ("LICENSE", G.DOC_FILE),
    ("package.json", None), ("dist/app.min.js", None), ("yarn.lock", None), ("logo.png", None),
])
def test_classify(path, label):
    assert classify(path) == label


@pytest.mark.parametrize("raw,norm", [
    ("/users/{user_id}", "/users/{}"), ("${BASE}/api/users/${id}", "/api/users/{}"),
    ("http://localhost:8000/chat/", "/chat"), ("/items/:id?x=1", "/items/{}"), ("/a/[slug]", "/a/{}"),
])
def test_normalize_route(raw, norm):
    assert normalize_route(raw) == norm


def test_repo_spec():
    s = parse_repo_spec("https://github.com/rikharigaurav/Read-Code-Structure/tree/feat/mcp-server/utils")
    assert (s.kind, s.owner, s.name) == ("github", "rikharigaurav", "Read-Code-Structure")
    # branch names can contain slashes; fetchers try the longest real ref first
    assert s.ref_candidates() == [("feat/mcp-server/utils", ""), ("feat/mcp-server", "utils"), ("feat", "mcp-server/utils")]
    s = parse_repo_spec("pallets/flask@3.0.0")
    assert (s.owner, s.name, s.ref) == ("pallets", "flask", "3.0.0")
    assert parse_repo_spec("git@github.com:psf/requests.git").name == "requests"
    assert parse_repo_spec("https://gitlab.com/a/b.git").kind == "git"
    with pytest.raises(FetchError):
        parse_repo_spec("not a repo")
