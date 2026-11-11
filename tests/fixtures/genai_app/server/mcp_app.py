from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo-tools")


@mcp.tool()
def lookup(term: str) -> str:
    """Look a term up."""
    return term


@mcp.resource("notes://{name}")
def note(name: str) -> str:
    return name


if __name__ == "__main__":
    mcp.run(transport="stdio")
