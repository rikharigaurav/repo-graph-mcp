from pathlib import Path

import pytest

from repo_graph_mcp import graph as G
from repo_graph_mcp.builder import build_graph
from repo_graph_mcp.summary import summarize

APP = Path(__file__).parent / "fixtures" / "genai_app"
AGENT = "AGENT:agent/graph.py:builder@31"
STEP = "AGENTSTEP:agent/graph.py:builder@31:"


@pytest.fixture(scope="module")
def g():
    return build_graph(APP, {"name": "genai_app"})


def rel(g, r, s, t):
    return (s, t, r) in g._edges


def props(g, r, s, t):
    return g._edges[(s, t, r)]


def test_hub_groups_the_layer(g):
    hub = g.nodes["GENAI:layer"]
    assert hub.label == G.GENAI
    assert rel(g, G.BELONGS_TO, "GENAI:layer", "FOLDER:.")
    children = {s for s, r in g.in_adj["GENAI:layer"] if r == G.BELONGS_TO}
    assert {AGENT, "MCPSERVER:server/mcp_app.py:mcp", "SERVICE:OpenAI", "GENAI:env"} <= children
    assert g.stats["genai"]["agents"] == 1


def test_langgraph_agent_steps_and_flow(g):
    a = g.nodes[AGENT]
    assert a.name == "research_agent"  # compile(name=...)
    assert a.props["entry"] == ["plan"]
    steps = {g.nodes[s].name: g.nodes[s] for s, r in g.in_adj[AGENT] if r == G.BELONGS_TO}
    assert [steps[n].props["order"] for n in ("plan", "act", "tools", "review")] == [1, 2, 3, 4]
    assert rel(g, G.RUNS, STEP + "plan", "FUNCTION:agent/graph.py:plan")
    assert props(g, G.RUNS, STEP + "plan", "FUNCTION:agent/graph.py:plan")["line"] == 32
    assert props(g, G.NEXT, STEP + "plan", STEP + "act")["kind"] == "edge"
    # router with a Literal return type, and Command(goto=...) inside a step
    assert props(g, G.NEXT, STEP + "act", STEP + "tools")["kind"] == "conditional"
    assert rel(g, G.ROUTES, STEP + "act", "FUNCTION:agent/graph.py:route")
    assert props(g, G.NEXT, STEP + "act", STEP + "review")["kind"] == "goto"
    assert steps["act"].props.get("can_end") and steps["review"].props.get("can_end")


def test_tool_node_resolves_list_from_other_module(g):
    assert rel(g, G.USES_TOOL, STEP + "tools", "FUNCTION:agent/tools.py:search")
    assert rel(g, G.USES_TOOL, STEP + "tools", "FUNCTION:agent/tools.py:fetch_page")
    assert g.nodes["FUNCTION:agent/tools.py:search"].props["ai_tool"] == "LangChain tool"
    assert g.nodes["FUNCTION:agent/tools.py:fetch_page"].props["ai_tool"] == "agent tool"


def test_mcp_server_exposes_decorated_functions(g):
    m = g.nodes["MCPSERVER:server/mcp_app.py:mcp"]
    assert m.name == "demo-tools" and m.props["transport"] == "stdio"
    assert props(g, G.EXPOSES, m.id, "FUNCTION:server/mcp_app.py:lookup")["kind"] == "tool"
    assert props(g, G.EXPOSES, m.id, "FUNCTION:server/mcp_app.py:note")["kind"] == "resource"
    assert (m.props["tools"], m.props["resources"]) == (1, 1)


def test_services_link_to_the_functions_that_use_them(g):
    # module-level client, used inside a function: inferred, with the call site
    p = props(g, G.USES, "FUNCTION:agent/llm.py:ask", "SERVICE:OpenAI")
    assert p["basis"] == "inferred" and p["line"] == 8
    assert rel(g, G.USES, "FUNCTION:agent/tools.py:search", "SERVICE:Tavily")
    # "provider:model" string
    assert rel(g, G.USES, "FUNCTION:agent/llm.py:planner_model", "SERVICE:Anthropic")
    # `with driver.session() as session`, and a client imported from another module
    assert rel(g, G.USES, "FUNCTION:server/db.py:run", "SERVICE:Neo4j")
    assert rel(g, G.USES, "FUNCTION:server/use_db.py:count_nodes", "SERVICE:Neo4j")
    assert g.nodes["SERVICE:Neo4j"].props["category"] == "Graph DB"
    assert g.nodes["SERVICE:Neo4j"].props["sdks"] == ["neo4j"]
    # JS: exported client, and a fetch to a known API host
    assert rel(g, G.USES, "FUNCTION:web/stripe.ts:charge", "SERVICE:Stripe")
    assert rel(g, G.USES, "FUNCTION:web/page.tsx:Page.load", "SERVICE:GitHub API")


def test_env_vars_modes_and_browser_secrets(g):
    env = {n.name: n for n in g.nodes.values() if n.label == G.ENV_VAR}
    assert env["NEO4J_URI"].props["mode"] == "required"  # os.environ[...]
    assert env["NEO4J_PASSWORD"].props["mode"] == "optional"  # os.getenv without default
    assert env["APP_DB_URL"].props["mode"] == "required"  # settings field, env_prefix applied
    assert env["APP_DEBUG"].props["mode"] == "default"
    assert rel(g, G.READS_ENV, "CLASS:server/settings.py:Settings", "ENV:APP_DB_URL")
    assert "warning" in env["NEXT_PUBLIC_DB_PASSWORD"].props
    assert "warning" not in env["NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY"].props  # public by design
    assert env["NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY"].props["mode"] == "default"  # `?? ""`
    assert "NEXT_PUBLIC_COMMENTED_OUT" not in env
    assert all(rel(g, G.BELONGS_TO, n.id, "GENAI:env") for n in env.values())


def test_docs_and_examples_are_not_scanned(g):
    assert not any(n.path.startswith("docs/") for n in g.nodes.values() if n.label == G.AGENT)
    assert g.stats["genai"]["files_skipped_nonprod"] == 1


def test_ui_payload_carries_provenance(g):
    payload = g.to_ui_payload()
    runs = [e for e in payload["edges"] if e["r"] == G.RUNS]
    assert runs and all(e["x"]["line"] and e["x"]["basis"] == "declared" for e in runs)
    assert all("x" not in e for e in payload["edges"] if e["r"] == G.CALLS)


def test_summary_and_description(g):
    text = summarize(g, None, 0.1, False, False)
    assert "Agent research_agent [LangGraph] agent/graph.py:31" in text
    assert "MCP server demo-tools (server/mcp_app.py, stdio) — exposes: lookup, note" in text
    assert "Warning: NEXT_PUBLIC_DB_PASSWORD (web/page.tsx:3)" in text
    desc = g.describe_node(g.nodes[AGENT])
    assert "step 1. plan (entry) runs plan (agent/graph.py)" in desc
    assert "[agent/graph.py:32; add_node('plan')]" in desc


def test_repo_without_ai_has_no_hub():
    g = build_graph(Path(__file__).parent / "fixtures" / "sample_repo", {"name": "sample_repo"})
    assert "GENAI:layer" not in g.nodes or g.nodes["GENAI:layer"].label == G.GENAI
    assert not any(n.label == G.AGENT for n in g.nodes.values())
