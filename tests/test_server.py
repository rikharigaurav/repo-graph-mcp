"""End-to-end through the MCP protocol, as Claude would call it."""

from pathlib import Path

from mcp.client import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

from repo_graph_mcp.server import VIEW_URI, mcp

SAMPLE = str((Path(__file__).parent / "fixtures" / "sample_repo").resolve())
APPS = advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})


async def test_tools_are_bound_to_the_view():
    async with Client(mcp, extensions=[APPS]) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert {"graph_repo", "explore_graph", "search_graph", "graph_view_data"} <= set(tools)
        assert tools["graph_repo"].meta["ui"]["resourceUri"] == VIEW_URI
        assert tools["graph_view_data"].meta["ui"]["visibility"] == ["app"]
        res = await c.read_resource(VIEW_URI)
        content = res.contents[0]
        assert content.mime_type == APP_MIME_TYPE
        assert "<html" in content.text.lower()


async def test_graph_repo_returns_summary_and_ui_payload():
    async with Client(mcp, extensions=[APPS]) as c:
        r = await c.call_tool("graph_repo", {"repo": SAMPLE})
        assert not r.is_error, r.content
        text = r.content[0].text
        assert "graph_id:" in text and "API endpoints (3)" in text
        assert "interactive graph" in text  # client advertised MCP Apps
        sc = r.structured_content
        assert sc["mode"] == "repo" and sc["nodes"] and sc["edges"]
        graph_id = sc["graphId"]

        r = await c.call_tool("search_graph", {"graph": graph_id, "query": "normalize"})
        assert "services.py" in r.content[0].text

        r = await c.call_tool("explore_graph", {"graph": graph_id, "node": "normalize", "direction": "in"})
        assert not r.is_error
        assert r.structured_content["focus"].endswith(":normalize")
        assert "<-[CALLS]-" in r.content[0].text

        r = await c.call_tool("explore_graph", {"graph": graph_id, "node": "does_not_exist_xyz"})
        assert r.is_error

        r = await c.call_tool("graph_view_data", {"graph": graph_id})
        assert r.structured_content["graphId"] == graph_id


async def test_text_only_client_gets_no_ui_claim():
    async with Client(mcp) as c:
        r = await c.call_tool("graph_repo", {"repo": SAMPLE})
        assert "interactive graph" not in r.content[0].text


async def test_bad_repo_is_a_clean_error():
    async with Client(mcp) as c:
        r = await c.call_tool("graph_repo", {"repo": "/definitely/not/here"})
        assert r.is_error and "not found" in r.content[0].text
