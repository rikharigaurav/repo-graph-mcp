# Hosted mode: streamable HTTP MCP endpoint at /mcp, for use as a claude.ai custom connector.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[neo4j]"

RUN useradd --create-home --uid 10001 graph && mkdir -p /data && chown graph /data
USER graph

ENV HOST=0.0.0.0 \
    PORT=8000 \
    REPO_GRAPH_CACHE_DIR=/data \
    REPO_GRAPH_ALLOW_LOCAL_PATHS=0
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
CMD ["sh", "-c", "repo-graph-mcp --http --host \"$HOST\" --port \"$PORT\""]
