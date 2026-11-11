"""Tools the research agent can call."""
from langchain_core.tools import tool
from tavily import TavilyClient


@tool
def search(query: str) -> str:
    """Search the web."""
    client = TavilyClient()
    return client.search(query)


def fetch_page(url: str) -> str:
    return url


TOOLS = [search, fetch_page]
