"""GenAI layer: AI agents, their tools, MCP servers, external services and env vars.

Runs after the code graph is resolved and links into it: an agent's steps point at the
functions that implement them, a service points back at the functions that call it, an env
var at the code that reads it. Everything sits under one hub node so the view can show the
layer as its own territory and break each part down on demand.

Static and deterministic like the rest of the builder. Every edge records the file and line
it came from (`file`, `line`) and what it rests on: `basis` is "declared" when the code says
it outright (`add_node("plan", plan)`, `@mcp.tool()`, `os.getenv("X")`) and "inferred" when
it follows a value (a call on an object that was built from an SDK client).

Not covered, on purpose: infrastructure files (compose, Kubernetes, Terraform) and anything
configured outside the repository. Code under docs/, examples/ and test folders is skipped.
"""

from __future__ import annotations

import ast
import logging
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import graph as G
from .graph import Node

if TYPE_CHECKING:
    from .builder import Builder, FileInfo

log = logging.getLogger("repo_graph_mcp")

HUB_ID = "GENAI:layer"
ENV_GROUP_ID = "GENAI:env"

# ── catalogue ──────────────────────────────────────────────────────────────────
LLM, VECTOR, GRAPHDB, DB, CACHE, QUEUE, CLOUD = "LLM", "Vector DB", "Graph DB", "Database", "Cache", "Queue", "Cloud"
PAYMENTS, BACKEND, OBS, SEARCH, MSG, API = "Payments", "Backend", "Observability", "Search API", "Messaging", "API"
DATASTORES = {VECTOR, GRAPHDB, DB, CACHE}

PY_SERVICES: dict[str, tuple[str, str]] = {
    "openai": ("OpenAI", LLM), "langchain_openai": ("OpenAI", LLM),
    "anthropic": ("Anthropic", LLM), "langchain_anthropic": ("Anthropic", LLM),
    "mistralai": ("Mistral", LLM), "langchain_mistralai": ("Mistral", LLM),
    "cohere": ("Cohere", LLM), "langchain_cohere": ("Cohere", LLM),
    "google.generativeai": ("Gemini", LLM), "google.genai": ("Gemini", LLM), "langchain_google_genai": ("Gemini", LLM),
    "vertexai": ("Vertex AI", LLM), "langchain_google_vertexai": ("Vertex AI", LLM),
    "groq": ("Groq", LLM), "langchain_groq": ("Groq", LLM),
    "ollama": ("Ollama", LLM), "langchain_ollama": ("Ollama", LLM),
    "together": ("Together AI", LLM), "langchain_together": ("Together AI", LLM),
    "fireworks": ("Fireworks", LLM), "langchain_fireworks": ("Fireworks", LLM),
    "huggingface_hub": ("Hugging Face", LLM), "langchain_huggingface": ("Hugging Face", LLM),
    "litellm": ("LiteLLM", LLM), "voyageai": ("Voyage AI", LLM), "replicate": ("Replicate", LLM),
    "langchain_aws": ("AWS Bedrock", LLM), "langchain_deepseek": ("DeepSeek", LLM), "langchain_xai": ("xAI", LLM),
    "tavily": ("Tavily", SEARCH), "langchain_tavily": ("Tavily", SEARCH), "exa_py": ("Exa", SEARCH),
    "serpapi": ("SerpAPI", SEARCH), "duckduckgo_search": ("DuckDuckGo", SEARCH), "ddgs": ("DuckDuckGo", SEARCH),
    "pinecone": ("Pinecone", VECTOR), "langchain_pinecone": ("Pinecone", VECTOR),
    "qdrant_client": ("Qdrant", VECTOR), "langchain_qdrant": ("Qdrant", VECTOR),
    "chromadb": ("Chroma", VECTOR), "langchain_chroma": ("Chroma", VECTOR),
    "weaviate": ("Weaviate", VECTOR), "langchain_weaviate": ("Weaviate", VECTOR),
    "pymilvus": ("Milvus", VECTOR), "langchain_milvus": ("Milvus", VECTOR), "lancedb": ("LanceDB", VECTOR),
    "pgvector": ("pgvector", VECTOR), "langchain_postgres": ("PostgreSQL (pgvector)", VECTOR),
    "langchain_google_alloydb_pg": ("AlloyDB", VECTOR), "langchain_mongodb": ("MongoDB Atlas", VECTOR),
    "langchain_elasticsearch": ("Elasticsearch", VECTOR), "langchain_redis": ("Redis", CACHE),
    "neo4j": ("Neo4j", GRAPHDB), "langchain_neo4j": ("Neo4j", GRAPHDB),
    "psycopg": ("PostgreSQL", DB), "psycopg2": ("PostgreSQL", DB), "asyncpg": ("PostgreSQL", DB),
    "pymysql": ("MySQL", DB), "mysql": ("MySQL", DB), "aiomysql": ("MySQL", DB),
    "pymongo": ("MongoDB", DB), "motor": ("MongoDB", DB), "beanie": ("MongoDB", DB),
    "sqlalchemy": ("SQL database", DB), "sqlmodel": ("SQL database", DB),
    "elasticsearch": ("Elasticsearch", DB), "opensearchpy": ("OpenSearch", DB),
    "redis": ("Redis", CACHE), "aioredis": ("Redis", CACHE),
    "kafka": ("Kafka", QUEUE), "confluent_kafka": ("Kafka", QUEUE), "aiokafka": ("Kafka", QUEUE),
    "pika": ("RabbitMQ", QUEUE), "aio_pika": ("RabbitMQ", QUEUE), "celery": ("Celery", QUEUE),
    "boto3": ("AWS", CLOUD), "aiobotocore": ("AWS", CLOUD), "google.cloud": ("Google Cloud", CLOUD),
    "azure": ("Azure", CLOUD),
    "stripe": ("Stripe", PAYMENTS), "supabase": ("Supabase", BACKEND), "firebase_admin": ("Firebase", BACKEND),
    "sentry_sdk": ("Sentry", OBS), "langsmith": ("LangSmith", OBS), "langfuse": ("Langfuse", OBS),
    "twilio": ("Twilio", MSG), "sendgrid": ("SendGrid", MSG), "slack_sdk": ("Slack", MSG),
    "github": ("GitHub API", API),
}

JS_SERVICES: dict[str, tuple[str, str]] = {
    "openai": ("OpenAI", LLM), "@ai-sdk/openai": ("OpenAI", LLM), "@langchain/openai": ("OpenAI", LLM),
    "@anthropic-ai/sdk": ("Anthropic", LLM), "@ai-sdk/anthropic": ("Anthropic", LLM), "@langchain/anthropic": ("Anthropic", LLM),
    "@mistralai/mistralai": ("Mistral", LLM), "@ai-sdk/mistral": ("Mistral", LLM), "@langchain/mistralai": ("Mistral", LLM),
    "cohere-ai": ("Cohere", LLM), "@google/generative-ai": ("Gemini", LLM), "@google/genai": ("Gemini", LLM),
    "@ai-sdk/google": ("Gemini", LLM), "@langchain/google-genai": ("Gemini", LLM), "groq-sdk": ("Groq", LLM),
    "@pinecone-database/pinecone": ("Pinecone", VECTOR), "@qdrant/js-client-rest": ("Qdrant", VECTOR),
    "chromadb": ("Chroma", VECTOR), "weaviate-client": ("Weaviate", VECTOR), "weaviate-ts-client": ("Weaviate", VECTOR),
    "neo4j-driver": ("Neo4j", GRAPHDB), "use-neo4j": ("Neo4j", GRAPHDB),
    "pg": ("PostgreSQL", DB), "postgres": ("PostgreSQL", DB), "@neondatabase/serverless": ("PostgreSQL", DB),
    "@vercel/postgres": ("PostgreSQL", DB), "mysql": ("MySQL", DB), "mysql2": ("MySQL", DB),
    "mongodb": ("MongoDB", DB), "mongoose": ("MongoDB", DB), "@prisma/client": ("SQL database", DB),
    "drizzle-orm": ("SQL database", DB), "typeorm": ("SQL database", DB), "sequelize": ("SQL database", DB),
    "knex": ("SQL database", DB),
    "redis": ("Redis", CACHE), "ioredis": ("Redis", CACHE), "@upstash/redis": ("Redis", CACHE),
    "kafkajs": ("Kafka", QUEUE), "amqplib": ("RabbitMQ", QUEUE), "bullmq": ("BullMQ", QUEUE),
    "aws-sdk": ("AWS", CLOUD), "@aws-sdk/": ("AWS", CLOUD), "@google-cloud/": ("Google Cloud", CLOUD), "@azure/": ("Azure", CLOUD),
    "stripe": ("Stripe", PAYMENTS), "@stripe/stripe-js": ("Stripe", PAYMENTS),
    "@supabase/supabase-js": ("Supabase", BACKEND), "@supabase/ssr": ("Supabase", BACKEND),
    "firebase": ("Firebase", BACKEND), "firebase/": ("Firebase", BACKEND), "firebase-admin": ("Firebase", BACKEND),
    "@sentry/": ("Sentry", OBS), "posthog-js": ("PostHog", OBS), "posthog-node": ("PostHog", OBS),
    "twilio": ("Twilio", MSG), "@sendgrid/": ("SendGrid", MSG), "resend": ("Resend", MSG),
    "@octokit/": ("GitHub API", API), "octokit": ("GitHub API", API),
    "neovis.js": ("Neo4j", GRAPHDB), "@neo4j-nvl/": ("Neo4j", GRAPHDB), "@neo4j-devtools/": ("Neo4j", GRAPHDB),
}

# hosts reached with plain HTTP clients (fetch, axios, httpx…) that are well-known APIs
API_HOSTS: dict[str, tuple[str, str]] = {
    "api.openai.com": ("OpenAI", LLM), "api.anthropic.com": ("Anthropic", LLM), "api.mistral.ai": ("Mistral", LLM),
    "api.cohere.ai": ("Cohere", LLM), "api.cohere.com": ("Cohere", LLM), "api.groq.com": ("Groq", LLM),
    "generativelanguage.googleapis.com": ("Gemini", LLM), "api-inference.huggingface.co": ("Hugging Face", LLM),
    "api.tavily.com": ("Tavily", SEARCH), "api.github.com": ("GitHub API", API), "api.stripe.com": ("Stripe", PAYMENTS),
    "hooks.slack.com": ("Slack", MSG), "slack.com": ("Slack", MSG),
}

# "provider:model" strings (init_chat_model, create_react_agent, PydanticAI Agent…)
PROVIDERS: dict[str, tuple[str, str]] = {
    "openai": ("OpenAI", LLM), "azure_openai": ("Azure OpenAI", LLM), "anthropic": ("Anthropic", LLM),
    "google_genai": ("Gemini", LLM), "google-gla": ("Gemini", LLM), "gemini": ("Gemini", LLM),
    "google_vertexai": ("Vertex AI", LLM), "google-vertex": ("Vertex AI", LLM), "mistralai": ("Mistral", LLM),
    "mistral": ("Mistral", LLM), "groq": ("Groq", LLM), "ollama": ("Ollama", LLM), "bedrock": ("AWS Bedrock", LLM),
    "bedrock_converse": ("AWS Bedrock", LLM), "cohere": ("Cohere", LLM), "fireworks": ("Fireworks", LLM),
    "together": ("Together AI", LLM), "deepseek": ("DeepSeek", LLM), "xai": ("xAI", LLM),
    "huggingface": ("Hugging Face", LLM), "perplexity": ("Perplexity", LLM),
}
PROVIDER_SLASH_RE = re.compile(r"^(" + "|".join(re.escape(p) for p in sorted(PROVIDERS, key=len, reverse=True)) + r")/[\w.:@-]{2,80}$")
PROVIDER_RE = re.compile(r"^(" + "|".join(re.escape(p) for p in sorted(PROVIDERS, key=len, reverse=True)) + r"):[\w.:/@-]{2,80}$")

AGENT_MODULES = {
    "langgraph": "LangGraph", "langchain": "LangChain", "deepagents": "Deep Agents", "crewai": "CrewAI",
    "agents": "OpenAI Agents SDK", "pydantic_ai": "PydanticAI", "smolagents": "smolagents",
    "autogen_agentchat": "AutoGen", "autogen": "AutoGen", "llama_index": "LlamaIndex", "google.adk": "Google ADK",
    "strands": "Strands Agents", "agno": "Agno",
}
PREBUILT = {"create_react_agent", "create_agent", "create_supervisor", "create_swarm", "create_deep_agent",
            "create_tool_calling_agent", "create_openai_functions_agent", "create_openai_tools_agent", "AgentExecutor"}
AGENT_CTORS = {"Agent", "CodeAgent", "ToolCallingAgent", "AssistantAgent", "ReActAgent", "FunctionAgent", "LlmAgent"}
GRAPH_CTORS = {"StateGraph", "MessageGraph"}
GRAPH_METHODS = {"add_node", "add_edge", "add_conditional_edges", "set_entry_point", "set_finish_point",
                 "add_sequence", "set_conditional_entry_point"}
MCP_CTORS = {"FastMCP", "MCPServer", "Server", "McpServer"}
MCP_KINDS = {"tool": "tool", "resource": "resource", "prompt": "prompt", "call_tool": "tool handler",
             "list_tools": "tool listing", "read_resource": "resource handler", "get_prompt": "prompt handler"}
TOOL_WRAPPERS = {"RunnableLambda", "partial", "RunnableParallel", "traceable"}
GENERIC_NAMES = {"graph", "app", "workflow", "builder", "agent", "g", "sg", "state_graph", "compiled", "compiled_graph",
                 "runnable", "chain", "graph_builder", "workflow_builder", "agent_builder", "_graph", "_app"}

ENV_CALLS = {"os.getenv", "os.environ.get", "os.environ.setdefault", "os.environ.pop", "os.getenvb", "decouple.config"}
SECRETISH = re.compile(r"(SECRET|PASSWORD|PASSWD|PRIVATE|SERVICE_ROLE|TOKEN|API_?KEY|_KEY$|CREDENTIAL|AUTH)", re.I)
PUBLIC_BY_DESIGN = re.compile(r"(PUBLISHABLE|ANON|SITE_KEY|SEARCH(_ONLY)?_(API_)?KEY|FIREBASE|POSTHOG|SENTRY|DSN|"
                              r"ANALYTICS|MEASUREMENT|GTM|_GA_|MAPS|MAPBOX|CLIENT_ID|_URL$|_URI$|_HOST$|_PORT$)", re.I)
BROWSER_PREFIXES = ("NEXT_PUBLIC_", "VITE_", "REACT_APP_", "NUXT_PUBLIC_", "EXPO_PUBLIC_", "GATSBY_", "PUBLIC_")
# ORM/schema declarations aren't calls to the service itself
SCHEMA_CALLS = {"Field", "Column", "relationship", "Relationship", "ForeignKey", "mapped_column", "String", "Integer",
                "Boolean", "DateTime", "Text", "Float", "JSON", "Enum", "Index", "UniqueConstraint", "Table", "MetaData",
                "declarative_base", "BaseModel", "SQLModel", "UUID", "Uuid", "AutoString", "VARCHAR", "Date", "Numeric",
                "BigInteger", "SmallInteger", "LargeBinary", "ARRAY", "JSONB", "TIMESTAMP", "Interval", "Sequence",
                "PrimaryKeyConstraint", "ForeignKeyConstraint", "CheckConstraint"}
NONPROD_DIRS = {"docs", "docs_src", "doc", "examples", "example", "samples", "sample", "tutorial", "tutorials",
                "benchmarks", "benchmark", "test", "tests", "__tests__", "spec", "e2e", "fixtures", "testing", "cookbook",
                "migrations", "alembic"}
PY_TRIGGERS = ("getenv", "environ", "BaseSettings", "decouple", "Graph(", "tool", "MCP", "Server(", "agent", "Agent")


def nonprod(rel: str) -> bool:
    return any(p.lower() in NONPROD_DIRS for p in rel.split("/")[:-1])


def _service_for(module: str, table: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """Longest matching module prefix: `langchain_openai.chat_models` → OpenAI; `@aws-sdk/client-s3` → AWS."""
    best: tuple[int, tuple[str, str]] | None = None
    for key, svc in table.items():
        if key.endswith("/"):
            ok = module.startswith(key)
        else:
            ok = module == key or module.startswith(key + ".") or module.startswith(key + "/")
        if ok and (best is None or len(key) > best[0]):
            best = (len(key), svc)
    return best[1] if best else None


def dotted(node: ast.AST | None) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(dotted(node.func) + "()")
    else:
        return ""
    return ".".join(reversed(parts))


def lit(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in ("END", "START"):
        return "__" + node.id.lower() + "__"
    if isinstance(node, ast.Attribute) and node.attr in ("END", "START"):
        return "__" + node.attr.lower() + "__"
    return None


def _nice(name: str) -> str:
    """`build_issue_solver_graph` → `issue_solver`."""
    n = re.sub(r"^(build|create|make|get|init|setup|compile|construct)_", "", name)
    n = re.sub(r"_(graph|workflow|builder)$", "", n)
    return n or name


# ── collected pieces ───────────────────────────────────────────────────────────

@dataclass
class _Step:
    name: str
    line: int
    order: int
    runs: str = ""
    targets: list[str] = field(default_factory=list)  # node ids it runs
    tools: list[str] = field(default_factory=list)  # tool function ids (ToolNode)
    tools_text: list[str] = field(default_factory=list)  # tools we couldn't resolve
    kind: str = "node"
    subgraph: "_Graph | None" = None
    scope: tuple[str, ...] = ()


@dataclass
class _Graph:
    rel: str
    var: str
    line: int
    scope: tuple[str, ...]
    framework: str = "LangGraph"
    steps: dict[str, _Step] = field(default_factory=dict)
    edges: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    compiled_as: str = ""
    compile_name: str = ""
    unresolved: list[str] = field(default_factory=list)
    node_id: str = ""


class Layer:
    """Collects GenAI findings across files, then writes them into the CodeGraph."""

    def __init__(self, b: "Builder"):
        self.b = b
        self.g = b.g
        self.services: dict[str, dict[str, Any]] = {}
        self.env: dict[str, dict[str, Any]] = {}
        self.top: list[str] = []  # direct children of the hub
        self.asts: dict[str, ast.Module | None] = {}
        self.defs: dict[str, dict[str, ast.AST]] = {}  # rel -> qualname -> FunctionDef
        self.counts: defaultdict[str, int] = defaultdict(int)

    # ── node/edge helpers ──
    def service(self, name: str, category: str, sdk: str = "") -> str:
        sid = f"SERVICE:{name}"
        if sid not in self.g.nodes:
            self.g.add_node(Node(sid, G.SERVICE, name, "", {"category": category, "sdks": []}))
            self.top.append(sid)
        if sdk and sdk not in self.g.nodes[sid].props["sdks"]:
            self.g.nodes[sid].props["sdks"].append(sdk)
        return sid

    def use(self, src_ids: list[str], svc: tuple[str, str], rel: str, line: int, how: str, basis: str, sdk: str = "") -> None:
        sid = self.service(svc[0], svc[1], sdk)
        for s in src_ids:
            if self.g.add_edge(s, sid, G.USES, count=1, file=rel, line=line, how=how, basis=basis):
                self.counts["uses"] += 1

    def env_read(self, src_ids: list[str], name: str, rel: str, line: int, how: str, mode: str) -> None:
        """mode: "required" (fails when unset: os.environ[...], a settings field without a default),
        "default" (falls back to a value) or "optional" (None/undefined when unset)."""
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
            self.counts["env_dynamic"] += 1
            return
        info = self.env.setdefault(name, {"reads": 0, "files": set(), "modes": set(), "how": set()})
        info["reads"] += 1
        info["files"].add(rel)
        info["how"].add(how)
        info["modes"].add(mode)
        eid = f"ENV:{name}"
        if eid not in self.g.nodes:
            self.g.add_node(Node(eid, G.ENV_VAR, name, "", {}))
        for s in src_ids:
            self.g.add_edge(s, eid, G.READS_ENV, count=1, file=rel, line=line, how=how, basis="declared", mode=mode)

    def mark_tool(self, fid: str, kind: str, name: str | None) -> None:
        n = self.g.nodes.get(fid)
        if n is None:
            return
        n.props["ai_tool"] = kind
        if name and name != n.name:
            n.props["tool_name"] = name
        self.counts["tools"] += 1

    # ── python ASTs, shared across files (steps often live in another module) ──
    def tree(self, rel: str) -> ast.Module | None:
        if rel not in self.asts:
            try:
                src = (self.b.root / rel).read_text("utf8", "replace")
                self.asts[rel] = ast.parse(src)
            except (SyntaxError, ValueError, OSError, RecursionError):
                self.asts[rel] = None
            defs: dict[str, ast.AST] = {}
            if self.asts[rel] is not None:
                stack: list[tuple[ast.AST, tuple[str, ...]]] = [(self.asts[rel], ())]
                while stack:
                    node, scope = stack.pop()
                    for c in ast.iter_child_nodes(node):
                        if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            q = scope + (c.name,)
                            defs[".".join(q)] = c
                            stack.append((c, q))
                        elif not isinstance(c, (ast.expr, ast.Import, ast.ImportFrom)):
                            stack.append((c, scope))
            self.defs[rel] = defs
        return self.asts[rel]

    def def_of(self, node_id: str) -> ast.AST | None:
        n = self.g.nodes.get(node_id)
        if n is None or n.label not in (G.FUNCTION, G.ENDPOINT) or not n.path.endswith((".py", ".pyi")):
            return None
        self.tree(n.path)
        q = n.props.get("qualname") or n.props.get("handler")
        return self.defs.get(n.path, {}).get(q) if q else None

    # ── run ──
    def run(self) -> None:
        files = []
        for f in self.b.files.values():
            if f.parse is None or f.label != G.SOURCE_FILE:
                continue
            if nonprod(f.rel):
                self.counts["nonprod_files"] += 1
                continue
            files.append(f)
        # 1. per file: imports and the client objects it builds
        scans: dict[str, "_PyScan | _JsScan"] = {}
        later: list["FileInfo"] = []
        for f in files:
            sc = self._guard(f, lambda: self._prepare(f))
            if sc is not None:
                scans[f.rel] = sc
            else:
                later.append(f)
        # 2. clients built at module level can be imported elsewhere (`from app.db import engine`)
        self.module_clients = {(rel, text): hit for rel, sc in scans.items() for text, hit in sc.module_level_clients()}
        for f in later:
            if any(tgt in self.module_clients for tgt in f.imported.values()):
                sc = self._guard(f, lambda: self._prepare(f, force=True))
                if sc is not None:
                    scans[f.rel] = sc
        # 3. everything else, now that imported clients are known
        for rel, sc in scans.items():
            self._guard(self.b.files[rel], sc.scan)
        for f in files:
            self._http_hosts(f)
        self.finish()

    def _guard(self, f: "FileInfo", fn):
        try:
            return fn()
        except RecursionError:
            return None
        except Exception as e:  # one odd file must never break the graph
            log.warning("GenAI scan failed for %s: %s", f.rel, e)
            return None

    def _prepare(self, f: "FileInfo", force: bool = False) -> "_PyScan | _JsScan | None":
        if f.language != "python":
            js = _JsScan(self, f)
            return js if js.prepare(force) else None
        mods = [i.module for i in f.parse.imports]
        relevant = force or any(_service_for(m, PY_SERVICES) for m in mods) or any(
            m.split(".")[0] in ("langgraph", "langchain", "langchain_core", "mcp", "fastmcp", "pydantic_settings",
                                "decouple", *[k.split(".")[0] for k in AGENT_MODULES]) for m in mods)
        if not relevant:
            try:
                head = (self.b.root / f.rel).read_text("utf8", "replace")
            except OSError:
                return None
            if not any(t in head for t in PY_TRIGGERS):
                return None
        tree = self.tree(f.rel)
        if tree is None:
            return None
        sc = _PyScan(self, f, tree)
        sc.prepare()
        return sc

    def _http_hosts(self, f: "FileInfo") -> None:
        for hc in f.parse.http_calls:
            m = re.match(r"^(?:[a-z]+:)?//([^/:]+)", hc.url.strip("`'\""), re.I)
            if not m:
                continue
            svc = API_HOSTS.get(m.group(1).lower())
            if svc:
                self.use(self.b._caller_ids(f, hc.caller), svc, f.rel, hc.line, f"HTTP {hc.method} {m.group(1)}", "declared")

    # ── agents ──
    def emit_graph(self, gr: _Graph, f: "FileInfo") -> None:
        if not gr.steps:
            return
        name = gr.compile_name or ""
        if not name and gr.compiled_as and gr.compiled_as.split(".")[-1] not in GENERIC_NAMES:
            name = gr.compiled_as.split(".")[-1]
        if not name and gr.scope:
            name = _nice(gr.scope[-1])
        if not name and gr.var.split(".")[-1] not in GENERIC_NAMES:
            name = gr.var.split(".")[-1]
        if not name:
            name = posixpath.splitext(posixpath.basename(f.rel))[0]
        aid = f"AGENT:{f.rel}:{gr.var}@{gr.line}"
        gr.node_id = aid
        entry = sorted(d for (s, d) in gr.edges if s == "__start__" and d in gr.steps)
        if not entry and gr.steps:
            entry = [min(gr.steps.values(), key=lambda s: s.order).name]
        flow = []
        for st in sorted(gr.steps.values(), key=lambda s: s.order):
            outs = [(d, p["kind"]) for (s, d), p in gr.edges.items() if s == st.name]
            if outs:
                flow.append(f"{st.name} → " + ", ".join(
                    "decided at run time" if d == "?" else ("END" if d == "__end__" else d) + (" (if)" if k == "conditional" else "")
                    for d, k in sorted(outs)))
        self.g.add_node(Node(aid, G.AGENT, name, f.rel, {
            "framework": gr.framework, "var": gr.var, "line": gr.line, "file_path": f.rel,
            "steps": len(gr.steps), "entry": entry, "flow": flow[:30],
            **({"compiled_as": gr.compiled_as} if gr.compiled_as else {}),
            **({"unresolved": gr.unresolved[:10]} if gr.unresolved else {}),
        }))
        self.top.append(aid)
        self.counts["agents"] += 1
        for st in gr.steps.values():
            sid = f"AGENTSTEP:{f.rel}:{gr.var}@{gr.line}:{st.name}"
            can_end = any(s == st.name and d == "__end__" for (s, d) in gr.edges)
            self.g.add_node(Node(sid, G.AGENT_STEP, st.name, f.rel, {
                "order": st.order, "line": st.line, "kind": st.kind, "runs": st.runs,
                **({"entry": True} if st.name in entry else {}), **({"can_end": True} if can_end else {}),
                **({"tools_unresolved": st.tools_text[:12]} if st.tools_text else {}),
            }))
            self.g.add_edge(sid, aid, G.BELONGS_TO)
            for t in st.targets:
                self.g.add_edge(sid, t, G.RUNS, file=f.rel, line=st.line, how=f"add_node({st.name!r})", basis="declared")
            for t in st.tools:
                self.g.add_edge(sid, t, G.USES_TOOL, file=f.rel, line=st.line, how="ToolNode", basis="declared")
                self.mark_tool(t, self.g.nodes[t].props.get("ai_tool") or "agent tool", None)
            self.counts["steps"] += 1
        for (s, d), p in gr.edges.items():
            if s in gr.steps and d in gr.steps:
                self.g.add_edge(f"AGENTSTEP:{f.rel}:{gr.var}@{gr.line}:{s}", f"AGENTSTEP:{f.rel}:{gr.var}@{gr.line}:{d}",
                                G.NEXT, file=f.rel, line=p.get("line", 0), kind=p["kind"],
                                how=p.get("how", p["kind"]), basis="declared",
                                **({"router": p["router"]} if p.get("router") else {}))
            if s in gr.steps and p.get("router_ids"):
                for r in p["router_ids"]:
                    self.g.add_edge(f"AGENTSTEP:{f.rel}:{gr.var}@{gr.line}:{s}", r, G.ROUTES, file=f.rel,
                                    line=p.get("line", 0), how="conditional edge", basis="declared")

    def finish(self) -> None:
        g = self.g
        for name, info in self.env.items():
            n = g.nodes[f"ENV:{name}"]
            secretish = bool(SECRETISH.search(name))
            browser = name.startswith(BROWSER_PREFIXES)
            mode = next(m for m in ("required", "optional", "default") if m in info["modes"])
            n.props.update({
                "reads": info["reads"], "files": sorted(info["files"])[:12], "mode": mode,
                **({"required": True} if mode == "required" else {}),
                "how": sorted(info["how"]), **({"secret_like": True} if secretish else {}),
                **({"browser_exposed": True} if browser else {}),
            })
            if browser and secretish and not PUBLIC_BY_DESIGN.search(name):
                n.props["warning"] = ("Looks like a secret, but the name prefix makes the build embed it in the "
                                      "JavaScript sent to browsers.")
                self.counts["exposed_secrets"] += 1
        tools = sum(1 for n in g.nodes.values() if n.props.get("ai_tool"))
        self.b.genai_stats = {k: v for k, v in {
            "agents": self.counts["agents"], "agent_steps": self.counts["steps"], "tools": tools,
            "mcp_servers": sum(1 for n in g.nodes.values() if n.label == G.MCP_SERVER),
            "services": sum(1 for n in g.nodes.values() if n.label == G.SERVICE),
            "env_vars": len(self.env), "exposed_secrets": self.counts["exposed_secrets"],
            "files_skipped_nonprod": self.counts["nonprod_files"], "env_dynamic": self.counts["env_dynamic"],
        }.items() if v}
        if not (self.top or self.env):
            return
        root = self.b.g.nodes.get("FOLDER:.")
        g.add_node(Node(HUB_ID, G.GENAI, "GenAI & services", "", {
            "summary": "AI agents, MCP servers, external services and environment variables found in the code."}))
        if root is not None:
            g.add_edge(HUB_ID, root.id, G.BELONGS_TO)
        for nid in self.top:
            g.add_edge(nid, HUB_ID, G.BELONGS_TO)
        if self.env:
            g.add_node(Node(ENV_GROUP_ID, G.ENV_GROUP, "Environment variables", "", {
                "count": len(self.env), "required": sum(1 for i in self.env.values() if "required" in i["modes"]),
                "exposed_secrets": self.counts["exposed_secrets"]}))
            g.add_edge(ENV_GROUP_ID, HUB_ID, G.BELONGS_TO)
            for name in sorted(self.env):
                g.add_edge(f"ENV:{name}", ENV_GROUP_ID, G.BELONGS_TO)


# ── Python ─────────────────────────────────────────────────────────────────────

class _PyScan:
    def __init__(self, layer: Layer, f: "FileInfo", tree: ast.Module):
        self.L, self.f, self.tree, self.b = layer, f, tree, layer.b
        self.rel = f.rel
        self.origin: dict[str, str] = {}  # local name -> fully dotted origin
        self.clients: dict[tuple[str, str], tuple[tuple[str, str], str]] = {}  # (scope key, text) -> (service, sdk)
        self.mcp: dict[str, str] = {}  # var -> server node id
        self.agents: dict[str, str] = {}  # var -> agent node id (framework agents with @agent.tool)
        self.used: set[str] = set()  # services with a call site in this file
        self.scopes: dict[int, tuple[tuple[str, ...], str | None]] = {}

    # scope of every node: (enclosing def/class names, enclosing class qualname)
    def _annotate(self) -> None:
        stack: list[tuple[ast.AST, tuple[str, ...], str | None]] = [(self.tree, (), None)]
        while stack:
            node, scope, cls = stack.pop()
            self.scopes[id(node)] = (scope, cls)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = scope + (node.name,)
                icls = ".".join(inner) if isinstance(node, ast.ClassDef) else cls
                outer = list(node.decorator_list)
                if isinstance(node, ast.ClassDef):
                    outer += list(node.bases) + [k.value for k in node.keywords]
                else:
                    outer += [d for d in [*node.args.defaults, *node.args.kw_defaults] if d is not None]
                stack.extend((c, scope, cls) for c in outer)
                stack.extend((c, inner, icls) for c in node.body)
                continue
            stack.extend((c, scope, cls) for c in ast.iter_child_nodes(node))

    def scope(self, node: ast.AST) -> tuple[str, ...]:
        return self.scopes.get(id(node), ((), None))[0]

    def ids(self, node: ast.AST) -> list[str]:
        q = ".".join(self.scope(node))
        return self.b._caller_ids(self.f, q or None)

    def full(self, d: str) -> str | None:
        """Fully qualified name of a dotted expression, if its head was imported."""
        if not d:
            return None
        head = d.split(".")[0]
        if head not in self.origin:
            return None
        return self.origin[head] + d[len(head):]

    def module_level_clients(self):
        for (scope, text), hit in self.clients.items():
            if scope == "" and "." not in text:
                yield text, hit

    def prepare(self) -> None:
        self._annotate()
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.asname:
                        self.origin[a.asname] = a.name
                    else:
                        self.origin.setdefault(a.name.split(".")[0], a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                for a in n.names:
                    if a.name != "*":
                        self.origin[a.asname or a.name] = f"{n.module}.{a.name}"
        self._clients()

    def scan(self) -> None:
        mc = self.L.module_clients
        for local, tgt in self.f.imported.items():
            if tgt in mc:
                self.clients.setdefault(("", local), mc[tgt])
        for local, rel in self.f.module_aliases.items():  # import app.core.db as db ; db.engine
            for (r, text), hit in mc.items():
                if r == rel:
                    self.clients.setdefault(("", f"{local}.{text}"), hit)
        self._clients()
        self._uses()
        self._env()
        self._settings()
        self._agents()
        self._tools()

    # ── services ──
    def _ckey(self, text: str, node: ast.AST) -> tuple[str, str]:
        scope, cls = self.scopes.get(id(node), ((), None))
        if text.startswith(("self.", "cls.")):
            return (cls or "", "self." + text.split(".", 1)[1])
        return (".".join(scope), text)

    def client_of(self, text: str, node: ast.AST) -> tuple[tuple[str, str], str] | None:
        if not text:
            return None
        scope, cls = self.scopes.get(id(node), ((), None))
        parts = text.split(".")
        if parts[0] in ("self", "cls"):
            for i in range(len(parts), 1, -1):
                hit = self.clients.get((cls or "", "self." + ".".join(parts[1:i])))
                if hit:
                    return hit
            return None
        for i in range(len(scope), -1, -1):
            key = ".".join(scope[:i])
            for k in range(min(len(parts), 3), 0, -1):
                hit = self.clients.get((key, ".".join(parts[:k])))
                if hit:
                    return hit
        return None

    def sdk_of(self, d: str) -> tuple[tuple[str, str], str] | None:
        full = self.full(d)
        if not full:
            return None
        svc = _service_for(full, PY_SERVICES)
        if svc is None:
            return None
        key = max((k for k in PY_SERVICES if full == k or full.startswith(k + ".")), key=len, default=full)
        return svc, key

    def _value_service(self, expr: ast.AST | None, depth: int = 0) -> tuple[tuple[str, str], str] | None:
        """Service behind a value: an SDK constructor/factory, or a call on (or alias of) a known client.
        Arguments aren't looked into: `Store(embeddings=OpenAIEmbeddings())` is a Store, not OpenAI."""
        if expr is None or depth > 6:
            return None
        if isinstance(expr, (ast.Await, ast.Starred)):
            return self._value_service(expr.value, depth + 1)
        if isinstance(expr, ast.Call):
            d = dotted(expr.func)
            hit = self.sdk_of(d) or self.client_of(d, expr)
            if hit:
                return self._aws(hit, expr)
            if isinstance(expr.func, ast.Attribute):  # Pinecone(...).Index("x")
                return self._value_service(expr.func.value, depth + 1)
            return None
        if isinstance(expr, (ast.Name, ast.Attribute)):
            hit = self.client_of(dotted(expr), expr)
            if hit:
                return hit
            if isinstance(expr, ast.Attribute):
                return self._value_service(expr.value, depth + 1)
        if isinstance(expr, ast.Subscript):
            return self._value_service(expr.value, depth + 1)
        return None

    def _aws(self, hit: tuple[tuple[str, str], str], call: ast.Call) -> tuple[tuple[str, str], str]:
        svc, sdk = hit
        if svc[0] == "AWS" and isinstance(call.func, ast.Attribute) and call.func.attr in ("client", "resource"):
            name = lit(call.args[0]) if call.args else None
            if name:
                return (f"AWS {name.upper() if len(name) <= 3 else name.capitalize()}", CLOUD), sdk
        return hit

    def _clients(self) -> None:
        assigns: list[tuple[list[ast.AST], ast.AST, ast.AST]] = []
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Assign):
                assigns.append((n.targets, n.value, n))
            elif isinstance(n, ast.AnnAssign) and n.value is not None:
                assigns.append(([n.target], n.value, n))
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None:
                        assigns.append(([item.optional_vars], item.context_expr, n))
        for _ in range(3):  # chains: pc = Pinecone(); self.index = pc.Index(...)
            changed = False
            for targets, value, node in assigns:
                hit = self._value_service(value)
                if not hit:
                    continue
                for t in targets:
                    if isinstance(t, (ast.Name, ast.Attribute)):
                        k = self._ckey(dotted(t), node)
                        if k[1] and k not in self.clients:
                            self.clients[k] = hit
                            changed = True
            if not changed:
                break

    def _uses(self) -> None:
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Call):
                for a in n.args[:2]:
                    self._model_string(lit(a), n)
                for k in n.keywords:
                    if k.arg and ("model" in k.arg or k.arg in ("llm", "provider")):
                        self._model_string(lit(k.value), n, slash=True)
                d = dotted(n.func)
                hit = self.sdk_of(d)
                how, basis = "SDK call", "declared"
                if not hit:
                    hit = self.client_of(d, n)
                    how, basis = "client call", "inferred"
                if not hit:
                    continue
                if d.split(".")[-1] in SCHEMA_CALLS:
                    continue
                (svc, sdk) = self._aws(hit, n)
                self.used.add(svc[0])
                self.L.use(self.ids(n), svc, self.rel, n.lineno, f"{how}: {d}()"[:120], basis, sdk)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith(("https://", "http://")):
                host = re.match(r"^https?://([^/:?#]+)", n.value)
                svc = API_HOSTS.get(host.group(1).lower()) if host else None
                if svc:
                    self.used.add(svc[0])
                    self.L.use(self.ids(n), svc, self.rel, n.lineno, f"URL {n.value[:60]}", "declared")
            elif isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                if any("model" in dotted(t).split(".")[-1].lower() for t in targets):
                    v = n.value
                    if isinstance(v, ast.Call):  # field(default=...), Field("...")
                        v = next((k.value for k in v.keywords if k.arg == "default"), v.args[0] if v.args else None)
                    self._model_string(lit(v), n, slash=True)
        self._imported_only()

    def _model_string(self, value: str | None, node: ast.AST, slash: bool = False) -> None:
        """`"openai:gpt-4.1"` passed to a call or set as a model field; `"anthropic/claude…"` only as a model field."""
        if not value or len(value) > 90:
            return
        m = PROVIDER_RE.match(value) or (PROVIDER_SLASH_RE.match(value) if slash else None)
        if m:
            svc = PROVIDERS[m.group(1)]
            self.used.add(svc[0])
            self.L.use(self.ids(node), svc, self.rel, node.lineno, f"model {value!r}", "declared")

    def _imported_only(self) -> None:
        # imported but never called here: still a dependency, recorded on the file
        for local, origin in self.origin.items():
            svc = _service_for(origin, PY_SERVICES)
            if svc and svc[0] not in self.used and not origin.startswith("boto3"):
                line = next((n.lineno for n in ast.walk(self.tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                             and any((a.asname or a.name).split(".")[0] == local for a in n.names)), 0)
                self.L.use([self.f.node_id], svc, self.rel, line, "imports the SDK", "declared", origin.split(".")[0])
                self.used.add(svc[0])

    # ── env vars ──
    def _env(self) -> None:
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Call):
                full = self.full(dotted(n.func))
                if full not in ENV_CALLS:
                    continue
                key = lit(n.args[0]) if n.args else lit(next((k.value for k in n.keywords if k.arg in ("key", "option")), None))
                has_default = len(n.args) > 1 or any(k.arg == "default" for k in n.keywords) or full.endswith("setdefault")
                mode = "default" if has_default else ("required" if full == "decouple.config" else "optional")
                self.L.env_read(self.ids(n), key or "", self.rel, n.lineno, full.replace("decouple.", ""), mode)
            elif isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and self.full(dotted(n.value)) == "os.environ":
                key = lit(n.slice)
                self.L.env_read(self.ids(n), key or "", self.rel, n.lineno, "os.environ[]", "required")

    def _settings(self) -> None:
        for n in ast.walk(self.tree):
            if not isinstance(n, ast.ClassDef):
                continue
            if not any((self.full(dotted(b)) or dotted(b)).endswith("BaseSettings") for b in n.bases):
                continue
            prefix = ""
            for st in n.body:
                if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "model_config" for t in st.targets) \
                        and isinstance(st.value, ast.Call):
                    prefix = lit(next((k.value for k in st.value.keywords if k.arg == "env_prefix"), None)) or prefix
                if isinstance(st, ast.ClassDef) and st.name == "Config":
                    for s2 in st.body:
                        if isinstance(s2, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "env_prefix" for t in s2.targets):
                            prefix = lit(s2.value) or prefix
            cls_q = ".".join(self.scope(n) + (n.name,))
            ids = self.f.sym_ids.get(cls_q) or [self.f.node_id]
            for st in n.body:
                if not isinstance(st, ast.AnnAssign) or not isinstance(st.target, ast.Name):
                    continue
                fname = st.target.id
                if fname.startswith("_") or fname == "model_config" or "ClassVar" in dotted(
                        st.annotation.value if isinstance(st.annotation, ast.Subscript) else st.annotation):
                    continue
                alias, has_default = None, st.value is not None
                if isinstance(st.value, ast.Call) and dotted(st.value.func).split(".")[-1] == "Field":
                    kw = {k.arg: k.value for k in st.value.keywords}
                    alias = lit(kw.get("alias")) or lit(kw.get("validation_alias")) or lit(kw.get("env"))
                    first = st.value.args[0] if st.value.args else kw.get("default")
                    has_default = (first is not None and not (isinstance(first, ast.Constant) and first.value is Ellipsis)) \
                        or "default_factory" in kw
                self.L.env_read(ids, alias or (prefix + fname).upper(), self.rel, st.lineno, f"settings {n.name}",
                                "default" if has_default else "required")

    # ── resolving references to code ──
    def lookup(self, name: str, scope: tuple[str, ...]) -> list[str]:
        f = self.f
        for i in range(len(scope), -1, -1):
            ids = f.sym_ids.get(".".join(scope[:i] + (name,)))
            if ids:
                return [x for x in ids if x != f.node_id]
        return self.b._resolve_name(f, name, None)

    def ref(self, expr: ast.AST | None, scope: tuple[str, ...]) -> tuple[list[str], str]:
        """Code a reference points at: (node ids, display text)."""
        if expr is None:
            return [], ""
        if isinstance(expr, ast.Name):
            return self.lookup(expr.id, scope), expr.id
        if isinstance(expr, ast.Attribute):
            obj, attr = dotted(expr.value), expr.attr
            if obj in ("self", "cls"):
                return self.b._resolve_call(self.f, attr, "self", ".".join(scope) or None), f"{obj}.{attr}"
            return (self.b._resolve_name(self.f, attr, obj) if obj else []), dotted(expr) or attr
        if isinstance(expr, ast.Call):
            fn = dotted(expr.func)
            if fn.split(".")[-1] in TOOL_WRAPPERS and expr.args:
                return self.ref(expr.args[0], scope)
            ids, text = self.ref(expr.func, scope)
            return ids, f"{text}(…)"
        if isinstance(expr, ast.Lambda):
            return [], "lambda"
        return [], ast.unparse(expr)[:60] if hasattr(ast, "unparse") else ""

    def ref_list(self, expr: ast.AST | None, scope: tuple[str, ...], depth: int = 0) -> tuple[list[str], list[str]]:
        """Tool lists: `[search, fetch]`, a name bound to a list, or `a + b`."""
        ids: list[str] = []
        unresolved: list[str] = []
        if expr is None or depth > 3:
            return ids, unresolved
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            for e in expr.elts:
                if isinstance(e, ast.Starred):
                    a, b = self.ref_list(e.value, scope, depth + 1)
                    ids += a
                    unresolved += b
                    continue
                r, text = self.ref(e, scope)
                fns = [x for x in r if self.L.g.nodes.get(x) is not None and self.L.g.nodes[x].label in (G.FUNCTION, G.ENDPOINT)]
                if fns:
                    ids += fns
                elif text:
                    unresolved.append(text)
        elif isinstance(expr, ast.BinOp):
            for side in (expr.left, expr.right):
                a, b = self.ref_list(side, scope, depth + 1)
                ids += a
                unresolved += b
        elif isinstance(expr, (ast.Name, ast.Attribute)):
            target = dotted(expr)
            for n in ast.walk(self.tree):
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None and n.lineno <= getattr(expr, "lineno", 10**9) \
                        and any(dotted(t) == target for t in (n.targets if isinstance(n, ast.Assign) else [n.target])):
                    a, b = self.ref_list(n.value, scope, depth + 1)
                    ids += a
                    unresolved += b
            if not ids and not unresolved and isinstance(expr, ast.Name) and expr.id in self.f.imported:
                ids = self._list_in(*self.f.imported[expr.id])  # from .tools import TOOLS
            if not ids and not unresolved:
                unresolved.append(target)
        elif isinstance(expr, ast.Call):
            unresolved.append(dotted(expr.func) + "(…)")
        return list(dict.fromkeys(ids)), list(dict.fromkeys(unresolved))

    def _list_in(self, rel: str, name: str) -> list[str]:
        """Functions listed in a module-level list in another file: `TOOLS = [search, fetch]`."""
        tree, fi = self.L.tree(rel), self.b.files.get(rel)
        if tree is None or fi is None:
            return []
        out: list[str] = []
        for st in tree.body:
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None and any(
                    isinstance(t, ast.Name) and t.id == name for t in (st.targets if isinstance(st, ast.Assign) else [st.target])):
                if isinstance(st.value, (ast.List, ast.Tuple)):
                    for e in st.value.elts:
                        if isinstance(e, ast.Name):
                            out += [x for x in (fi.sym_ids.get(e.id) or self.b._resolve_name(fi, e.id, None))
                                    if self.L.g.nodes.get(x) is not None and self.L.g.nodes[x].label in (G.FUNCTION, G.ENDPOINT)]
        return list(dict.fromkeys(out))

    # ── agents ──
    def _agents(self) -> None:
        events: list[tuple[int, int, str, Any]] = []
        for n in ast.walk(self.tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Call):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                tgt = next((dotted(t) for t in targets if isinstance(t, (ast.Name, ast.Attribute))), "")
                if tgt:
                    events.append((n.lineno, n.col_offset, "assign", (tgt, n.value)))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in GRAPH_METHODS:
                events.append((n.lineno, n.col_offset + 1, "method", n))
        events.sort(key=lambda e: (e[0], e[1]))
        graphs: list[_Graph] = []
        by_var: dict[str, _Graph] = {}
        compiled: dict[str, _Graph] = {}
        for line, _, kind, data in events:
            if kind == "assign":
                tgt, call = data
                fn = dotted(call.func)
                full = self.full(fn) or ""
                last = fn.split(".")[-1]
                if last in GRAPH_CTORS and full.startswith("langgraph"):
                    gr = _Graph(self.rel, tgt, line, self.scope(call))
                    graphs.append(gr)
                    by_var[tgt] = gr
                elif last == "compile" and isinstance(call.func, ast.Attribute) and dotted(call.func.value) in by_var:
                    gr = by_var[dotted(call.func.value)]
                    gr.compiled_as = tgt
                    gr.compile_name = lit(next((k.value for k in call.keywords if k.arg == "name"), None)) or gr.compile_name
                    compiled[tgt] = gr
                elif last in PREBUILT and full.split(".")[0] in ("langgraph", "langchain", "deepagents", "langgraph_supervisor",
                                                                 "langgraph_swarm"):
                    self._prebuilt(tgt, call, full, line)
                elif last in AGENT_CTORS and full and any(full == m or full.startswith(m + ".") for m in AGENT_MODULES):
                    self._framework_agent(tgt, call, full, line)
                elif last in MCP_CTORS and full.split(".")[0] in ("mcp", "fastmcp"):
                    self._mcp_server(tgt, call, line)
                continue
            call: ast.Call = data
            gr = by_var.get(dotted(call.func.value))
            if gr is None:
                continue
            self._graph_call(gr, call, compiled, by_var)
        for gr in graphs:
            self._goto(gr)
            self.L.emit_graph(gr, self.f)
        for gr in graphs:  # subgraph steps point at the sub-agent once both exist
            for st in gr.steps.values():
                if st.subgraph is not None and st.subgraph.node_id and gr.node_id:
                    self.L.g.add_edge(f"AGENTSTEP:{self.rel}:{gr.var}@{gr.line}:{st.name}", st.subgraph.node_id,
                                      G.RUNS, file=self.rel, line=st.line, how="subgraph", basis="declared")

    def _graph_call(self, gr: _Graph, call: ast.Call, compiled: dict[str, _Graph], by_var: dict[str, _Graph]) -> None:
        m = call.func.attr
        a = call.args
        kw = {k.arg: k.value for k in call.keywords}
        scope = self.scope(call)

        def add_step(name: str, target: ast.AST | None) -> None:
            st = gr.steps.get(name) or _Step(name, call.lineno, len(gr.steps) + 1, scope=scope)
            gr.steps[name] = st
            if target is None:
                ids, text = self.lookup(name, scope), name
            elif isinstance(target, ast.Name) and target.id in compiled:
                st.subgraph, st.kind, st.runs = compiled[target.id], "subgraph", target.id
                return
            elif isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute) and target.func.attr == "compile" \
                    and dotted(target.func.value) in by_var:
                st.subgraph, st.kind, st.runs = by_var[dotted(target.func.value)], "subgraph", dotted(target.func.value)
                return
            elif isinstance(target, ast.Call) and dotted(target.func).split(".")[-1] == "ToolNode":
                st.kind, st.runs = "tools", "ToolNode"
                st.tools, st.tools_text = self.ref_list(target.args[0] if target.args else kw.get("tools"), scope)
                return
            else:
                ids, text = self.ref(target, scope)
            st.runs = text
            st.targets = [x for x in ids if self.L.g.nodes.get(x) is not None]
            if not st.targets and text:
                gr.unresolved.append(f"step {name!r} runs {text}")

        def edge(s: str | None, d: str | None, kind: str, **extra: Any) -> None:
            if s and d:
                gr.edges.setdefault((s, d), {"kind": kind, "line": call.lineno, **extra})

        if m == "add_node":
            if a and lit(a[0]) is not None:
                add_step(lit(a[0]), a[1] if len(a) > 1 else kw.get("action") or kw.get("node"))
            elif a:
                name = dotted(a[0]).split(".")[-1] or "node"
                add_step(name, a[0])
        elif m == "add_edge":
            srcs = a[0] if a else kw.get("start_key")
            dst = lit(a[1]) if len(a) > 1 else lit(kw.get("end_key"))
            for s in (srcs.elts if isinstance(srcs, (ast.List, ast.Tuple)) else [srcs]):
                edge(lit(s), dst, "edge")
        elif m == "set_entry_point":
            edge("__start__", lit(a[0]) if a else lit(kw.get("key")), "edge")
        elif m == "set_finish_point":
            edge(lit(a[0]) if a else lit(kw.get("key")), "__end__", "edge")
        elif m == "add_sequence" and a and isinstance(a[0], (ast.List, ast.Tuple)):
            names = []
            for e in a[0].elts:
                if isinstance(e, ast.Tuple) and len(e.elts) == 2 and lit(e.elts[0]):
                    add_step(lit(e.elts[0]), e.elts[1])
                    names.append(lit(e.elts[0]))
                else:
                    nm = dotted(e).split(".")[-1]
                    if nm:
                        add_step(nm, e)
                        names.append(nm)
            for x, y in zip(names, names[1:]):
                edge(x, y, "edge")
        elif m in ("add_conditional_edges", "set_conditional_entry_point"):
            if m == "set_conditional_entry_point":
                src, router = "__start__", a[0] if a else kw.get("path")
                pmap = a[1] if len(a) > 1 else kw.get("path_map")
            else:
                src = lit(a[0]) if a else lit(kw.get("source"))
                router = a[1] if len(a) > 1 else kw.get("path")
                pmap = a[2] if len(a) > 2 else kw.get("path_map")
            rtext = dotted(router) if router is not None else ""
            rids = [x for x in self.ref(router, scope)[0] if self.L.g.nodes.get(x) is not None] if router is not None else []
            targets = self._map_targets(pmap) if pmap is not None else []
            if not targets:
                targets = self._router_targets(rtext, rids)
            if not targets:
                gr.unresolved.append(f"where {src!r} goes next is decided at run time by {rtext or 'a router'}")
            for t in targets:
                edge(src, t, "conditional", router=rtext, router_ids=rids, how=f"via {rtext}" if rtext else "conditional")
            if not targets and rids:  # still show which function decides
                gr.edges.setdefault((src or "", "?"), {"kind": "conditional", "line": call.lineno, "router_ids": rids})

    def _map_targets(self, pm: ast.AST) -> list[str]:
        if isinstance(pm, ast.Dict):
            return [v for v in (lit(x) for x in pm.values) if v]
        if isinstance(pm, (ast.List, ast.Tuple)):
            return [v for v in (lit(x) for x in pm.elts) if v]
        return []

    def _router_targets(self, rtext: str, rids: list[str]) -> list[str]:
        if rtext.split(".")[-1] == "tools_condition":
            return ["tools", "__end__"]
        out: list[str] = []
        for rid in rids:
            fn = self.L.def_of(rid)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out += _returns_of(fn)
        return sorted(set(out))

    def _goto(self, gr: _Graph) -> None:
        """`return Command(goto="next")` / `-> Command[Literal["a", "b"]]` inside step functions."""
        for st in gr.steps.values():
            for tid in st.targets:
                fn = self.L.def_of(tid)
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                targets = set(_literal_annotation(fn.returns)) if fn.returns is not None and "Command" in dotted(
                    fn.returns.value if isinstance(fn.returns, ast.Subscript) else fn.returns) else set()
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and dotted(n.func).split(".")[-1] == "Command":
                        for k in n.keywords:
                            if k.arg == "goto":
                                for e in ast.walk(k.value):
                                    v = lit(e)
                                    if v:
                                        targets.add(v)
                                    elif isinstance(e, ast.Call) and dotted(e.func).split(".")[-1] == "Send" and e.args and lit(e.args[0]):
                                        targets.add(lit(e.args[0]))
                for t in targets:
                    if t in gr.steps or t == "__end__":
                        gr.edges.setdefault((st.name, t), {"kind": "goto", "line": fn.lineno, "how": "Command(goto=…)"})

    def _prebuilt(self, tgt: str, call: ast.Call, full: str, line: int) -> None:
        kw = {k.arg: k.value for k in call.keywords}
        scope = self.scope(call)
        last = full.split(".")[-1]
        tools_expr = kw.get("tools") if "tools" in kw else (call.args[1] if len(call.args) > 1 and last != "AgentExecutor" else None)
        tools, unresolved = self.ref_list(tools_expr, scope)
        name = lit(kw.get("name")) or (tgt.split(".")[-1] if tgt.split(".")[-1] not in GENERIC_NAMES else "") \
            or (_nice(scope[-1]) if scope else "") or tgt
        aid = f"AGENT:{self.rel}:{tgt}@{line}"
        self.L.g.add_node(Node(aid, G.AGENT, name, self.rel, {
            "framework": f"{AGENT_MODULES.get(full.split('.')[0], 'LangGraph')} ({last})", "var": tgt, "line": line,
            "file_path": self.rel, **({"tools_unresolved": unresolved[:12]} if unresolved else {})}))
        self.L.top.append(aid)
        self.L.counts["agents"] += 1
        for t in tools:
            self.L.g.add_edge(aid, t, G.USES_TOOL, file=self.rel, line=line, how=f"{last}(tools=…)", basis="declared")
            self.L.mark_tool(t, self.L.g.nodes[t].props.get("ai_tool") or "agent tool", None)
        self._agent_model(aid, kw.get("model") or (call.args[0] if call.args else None), line)
        for k in ("prompt", "state_modifier", "pre_model_hook", "post_model_hook"):
            ids, _ = self.ref(kw.get(k), scope) if isinstance(kw.get(k), (ast.Name, ast.Attribute)) else ([], "")
            for t in ids:
                self.L.g.add_edge(aid, t, G.RUNS, file=self.rel, line=line, how=k, basis="declared")
        self.agents[tgt] = aid

    def _framework_agent(self, tgt: str, call: ast.Call, full: str, line: int) -> None:
        kw = {k.arg: k.value for k in call.keywords}
        scope = self.scope(call)
        module = next(m for m in sorted(AGENT_MODULES, key=len, reverse=True) if full == m or full.startswith(m + "."))
        name = lit(kw.get("name")) or lit(kw.get("role")) or (
            tgt.split(".")[-1] if tgt.split(".")[-1] not in GENERIC_NAMES else "") or (
            _nice(scope[-1]) if scope else posixpath.splitext(posixpath.basename(self.rel))[0])
        aid = f"AGENT:{self.rel}:{tgt}@{line}"
        tools, unresolved = self.ref_list(kw.get("tools"), scope)
        self.L.g.add_node(Node(aid, G.AGENT, name, self.rel, {
            "framework": f"{AGENT_MODULES[module]} ({full.split('.')[-1]})", "var": tgt, "line": line, "file_path": self.rel,
            **({"summary": (lit(kw.get("instructions")) or lit(kw.get("goal")) or lit(kw.get("system_prompt")) or "")[:160]}
               if any(k in kw for k in ("instructions", "goal", "system_prompt")) else {}),
            **({"tools_unresolved": unresolved[:12]} if unresolved else {})}))
        self.L.top.append(aid)
        self.L.counts["agents"] += 1
        for t in tools:
            self.L.g.add_edge(aid, t, G.USES_TOOL, file=self.rel, line=line, how="tools=[…]", basis="declared")
            self.L.mark_tool(t, self.L.g.nodes[t].props.get("ai_tool") or f"{AGENT_MODULES[module]} tool", None)
        self._agent_model(aid, kw.get("model") or kw.get("llm") or (call.args[0] if call.args else None), line)
        handoffs, _ = self.ref_list(kw.get("handoffs"), scope)
        self.agents[tgt] = aid

    def _agent_model(self, aid: str, expr: ast.AST | None, line: int) -> None:
        if expr is None:
            return
        s = lit(expr)
        if s and PROVIDER_RE.match(s):
            svc = PROVIDERS[PROVIDER_RE.match(s).group(1)]
            self.L.use([aid], svc, self.rel, line, f"model {s!r}", "declared")
            return
        hit = self._value_service(expr)
        if hit:
            self.L.use([aid], hit[0], self.rel, line, f"model {dotted(expr) or 'client'}", "inferred", hit[1])

    def _mcp_server(self, tgt: str, call: ast.Call, line: int) -> None:
        kw = {k.arg: k.value for k in call.keywords}
        name = lit(call.args[0]) if call.args else None
        name = name or lit(kw.get("name")) or tgt.split(".")[-1]
        sid = f"MCPSERVER:{self.rel}:{tgt}"
        self.L.g.add_node(Node(sid, G.MCP_SERVER, name, self.rel, {
            "var": tgt, "line": line, "file_path": self.rel, "framework": dotted(call.func).split(".")[-1]}))
        if sid not in self.L.top:
            self.L.top.append(sid)
        self.mcp[tgt] = sid

    # ── tools ──
    def _tools(self) -> None:
        for n in ast.walk(self.tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join(self.scope(n) + (n.name,))
                fids = [x for x in self.f.sym_ids.get(qual, []) if x != self.f.node_id]
                if not fids:
                    continue
                for d in n.decorator_list:
                    dc = d.func if isinstance(d, ast.Call) else d
                    ds = dotted(dc)
                    head, _, attr = ds.rpartition(".")
                    tname = (lit(d.args[0]) if isinstance(d, ast.Call) and d.args else None) or (
                        lit(next((k.value for k in d.keywords if k.arg == "name"), None)) if isinstance(d, ast.Call) else None)
                    if head in self.mcp and attr in MCP_KINDS:
                        kind = MCP_KINDS[attr]
                        for fid in fids:
                            self.L.g.add_edge(self.mcp[head], fid, G.EXPOSES, file=self.rel, line=d.lineno,
                                              how=f"@{ds}", basis="declared", kind=kind)
                            self.L.mark_tool(fid, f"MCP {kind}", tname if attr in ("tool", "prompt") else None)
                    elif head in self.agents and attr in ("tool", "tool_plain"):
                        for fid in fids:
                            self.L.g.add_edge(self.agents[head], fid, G.USES_TOOL, file=self.rel, line=d.lineno,
                                              how=f"@{ds}", basis="declared")
                            self.L.mark_tool(fid, "agent tool", tname)
                    else:
                        full = self.full(ds) or ""
                        last = full.split(".")[-1]
                        if last in ("tool", "function_tool") and full.split(".")[0] in (
                                "langchain", "langchain_core", "crewai", "agents", "smolagents", "strands", "llama_index",
                                "langgraph", "google"):
                            fw = AGENT_MODULES.get(full.split(".")[0], "LangChain")
                            for fid in fids:
                                self.L.mark_tool(fid, f"{fw} tool", tname)
            elif isinstance(n, ast.Call):
                d = dotted(n.func)
                head, _, attr = d.rpartition(".")
                full = self.full(d) or ""
                scope = self.scope(n)
                if attr == "bind_tools" and n.args:
                    tools, _ = self.ref_list(n.args[0], scope)
                    for src in self.ids(n):
                        for t in tools:
                            self.L.g.add_edge(src, t, G.USES_TOOL, file=self.rel, line=n.lineno, how="bind_tools", basis="declared")
                            self.L.mark_tool(t, self.L.g.nodes[t].props.get("ai_tool") or "LangChain tool", None)
                elif head in self.mcp and attr in ("add_tool", "add_resource", "add_prompt") and n.args:
                    ids, _ = self.ref(n.args[0], scope)
                    kind = attr.split("_")[1]
                    for fid in ids:
                        self.L.g.add_edge(self.mcp[head], fid, G.EXPOSES, file=self.rel, line=n.lineno, how=f"{d}()",
                                          basis="declared", kind=kind)
                        self.L.mark_tool(fid, f"MCP {kind}", None)
                elif head in self.mcp and attr == "run":
                    tr = lit(next((k.value for k in n.keywords if k.arg == "transport"), None)) or (lit(n.args[0]) if n.args else None)
                    if tr:
                        self.L.g.nodes[self.mcp[head]].props["transport"] = tr
                elif full.startswith(("langchain", "langchain_core")) and full.split(".")[-1] in ("Tool", "StructuredTool", "from_function"):
                    fexpr = next((k.value for k in n.keywords if k.arg in ("func", "coroutine")), n.args[0] if n.args and attr == "from_function" else None)
                    ids, _ = self.ref(fexpr, scope)
                    for fid in ids:
                        self.L.mark_tool(fid, "LangChain tool", lit(next((k.value for k in n.keywords if k.arg == "name"), None)))
        for sid in list(self.mcp.values()):
            node = self.L.g.nodes[sid]
            if not any(r == G.EXPOSES for _, r in self.L.g.out_adj.get(sid, [])) and not node.props.get("transport"):
                # a server object nothing is registered on here (library internals, factories): leave it out
                del self.L.g.nodes[sid]
                if sid in self.L.top:
                    self.L.top.remove(sid)
                continue
            kinds = [p.get("kind") for t, r in self.L.g.out_adj.get(sid, []) if r == G.EXPOSES
                     for p in [self.L.g._edges.get((sid, t, r), {})]]
            node.props["tools"] = sum(1 for k in kinds if k in ("tool", "tool handler"))
            node.props["resources"] = sum(1 for k in kinds if k and k.startswith("resource"))
            node.props["prompts"] = sum(1 for k in kinds if k and k.startswith("prompt"))


def _literal_annotation(ann: ast.AST | None) -> list[str]:
    out: list[str] = []
    if ann is None:
        return out
    for n in ast.walk(ann):
        if isinstance(n, ast.Subscript) and dotted(n.value).split(".")[-1] == "Literal":
            for e in ast.walk(n.slice):
                v = lit(e)
                if v:
                    out.append(v)
    return out


def _returns_of(fn: ast.AST) -> list[str]:
    out = set(_literal_annotation(getattr(fn, "returns", None)))
    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and n.value is not None:
            v = lit(n.value)
            if v:
                out.add(v)
            for c in ast.walk(n.value):
                if isinstance(c, ast.Call) and dotted(c.func).split(".")[-1] == "Send" and c.args and lit(c.args[0]):
                    out.add(lit(c.args[0]))
                elif isinstance(c, ast.IfExp):
                    for side in (c.body, c.orelse):
                        if lit(side):
                            out.add(lit(side))
    return sorted(out)


# ── JavaScript / TypeScript ────────────────────────────────────────────────────

JS_ENV = re.compile(r"""process\.env\.([A-Za-z_][A-Za-z0-9_]*)|process\.env\[\s*['"]([^'"]+)['"]\s*\]|import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)""")
JS_ENV_DEFAULT = re.compile(r"""^\s*(\?\?|\|\|)""")
JS_ENV_DESTRUCT = re.compile(r"\{([^{}]+)\}\s*=\s*process\.env\b")
JS_GRAPH = re.compile(r"(?:(?:const|let|var)\s+([\w$]+)\s*=\s*)?new\s+StateGraph\b")
JS_ADD_NODE = re.compile(r"""\.addNode\(\s*['"]([\w$.-]+)['"]\s*,\s*([\w$.]+)""")
JS_ADD_EDGE = re.compile(r"""\.addEdge\(\s*(['"][\w$.-]+['"]|START|END)\s*,\s*(['"][\w$.-]+['"]|START|END)""")
JS_COND = re.compile(r"""\.addConditionalEdges\(\s*(['"][\w$.-]+['"]|START)\s*,\s*([\w$.]+)(?:\s*,\s*(\{[^{}]*\}|\[[^\[\]]*\]))?""")
JS_COMPILE = re.compile(r"(?:const|let|var)\s+([\w$]+)\s*=\s*([\w$]+)\.compile\(")
JS_MCP = re.compile(r"""new\s+(McpServer|Server)\s*\(\s*\{[^{}]*?name\s*:\s*['"]([^'"]+)['"]""")
JS_MCP_TOOL = re.compile(r"""\.(tool|registerTool|resource|registerResource|prompt|registerPrompt)\(\s*['"]([^'"]+)['"]""")


def _strip_js_comments(src: str) -> str:
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    # strings first so `//` inside URLs survives
    return re.sub(r"""("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)|(//[^\n]*|/\*.*?\*/)""",
                  lambda m: m.group(1) if m.group(1) else blank(m), src, flags=re.S)


class _JsScan:
    def __init__(self, layer: Layer, f: "FileInfo"):
        self.L, self.f, self.b = layer, f, layer.b
        self.rel = f.rel
        self.ranges = sorted(((s.line, s.end_line, s.qualname) for s in f.parse.symbols), key=lambda r: (r[0], -r[1]))

    def ids_at(self, line: int) -> list[str]:
        best: tuple[int, int, str] | None = None
        for r in self.ranges:
            if r[0] <= line <= r[1] and (best is None or r[1] - r[0] <= best[1] - best[0]):
                best = r
        return self.b._caller_ids(self.f, best[2] if best else None)

    def prepare(self, force: bool = False) -> bool:
        self.mods = [i.module for i in self.f.parse.imports]
        self.sdk_imports = [(i, s) for i in self.f.parse.imports for s in [_service_for(i.module, JS_SERVICES)] if s]
        text_needed = force or self.sdk_imports or any(m.startswith(("@langchain/langgraph", "@modelcontextprotocol")) for m in self.mods)
        try:
            raw = (self.b.root / self.rel).read_text("utf8", "replace")
        except OSError:
            return False
        if not text_needed and "process.env" not in raw and "import.meta.env" not in raw and "https://" not in raw:
            return False
        self.src = _strip_js_comments(raw)
        starts = [0]
        for m in re.finditer(r"\n", self.src):
            starts.append(m.end())
        self.line_of = lambda pos: _bisect(starts, pos)  # noqa: E731
        self.locals: dict[str, tuple[tuple[str, str], str]] = {}
        for imp, svc in self.sdk_imports:
            pkg = imp.module
            if svc[0] == "AWS" and "/client-" in pkg:
                tail = pkg.split("/client-")[1].replace("-", " ")
                svc = (f"AWS {tail.upper() if len(tail) <= 3 else tail.title()}", CLOUD)
            for local in [*imp.names.keys(), *([imp.alias] if imp.alias else [])]:
                if local and local != "*":
                    self.locals[local] = (svc, pkg)
        self.clients: dict[str, tuple[tuple[str, str], str]] = {}
        self._find_clients(self.locals)
        return True

    def _find_clients(self, sources: dict[str, tuple[tuple[str, str], str]]) -> None:
        """`const supabase = createClient(...)`, `this.db = new Pool()`, `const idx = pc.index("x")`."""
        for _ in range(2):
            for local, hit in list({**sources, **self.clients}.items()):
                if local.startswith("this."):
                    continue
                pat = re.compile(r"(?:const|let|var)\s+([\w$]+)\s*(?::[^=;]+)?=\s*(?:await\s+)?(?:new\s+)?" + re.escape(local) + r"\b"
                                 r"|this\.([\w$]+)\s*=\s*(?:await\s+)?(?:new\s+)?" + re.escape(local) + r"\b")
                for m in pat.finditer(self.src):
                    self.clients.setdefault(m.group(1) or "this." + m.group(2), hit)

    def module_level_clients(self):
        for var, hit in self.clients.items():
            if not var.startswith("this."):
                yield var, hit

    def scan(self) -> None:
        imported = {local: self.L.module_clients[tgt] for local, tgt in self.f.imported.items() if tgt in self.L.module_clients}
        if imported:
            self.clients.update({k: v for k, v in imported.items() if k not in self.locals})
            self._find_clients(imported)
        src, line_of = self.src, self.line_of
        self._env(src, line_of)
        if self.sdk_imports or self.clients:
            self._services(src, line_of, self.sdk_imports)
        for m in re.finditer(r"""['"`]https?://([a-z0-9.-]+)""", src, re.I):
            svc = API_HOSTS.get(m.group(1).lower())
            if svc:
                line = line_of(m.start())
                self.L.use(self.ids_at(line), svc, self.rel, line, f"URL https://{m.group(1)}…", "declared")
        if "StateGraph" in src and any(m.startswith("@langchain/langgraph") for m in self.mods):
            self._graph(src, line_of)
        if any(m.startswith("@modelcontextprotocol") for m in self.mods):
            self._mcp(src, line_of)

    def _env(self, src: str, line_of) -> None:
        for m in JS_ENV.finditer(src):
            name = m.group(1) or m.group(2) or m.group(3)
            line = line_of(m.start())
            has_default = bool(JS_ENV_DEFAULT.match(src[m.end():m.end() + 6]))
            how = "import.meta.env" if m.group(3) else "process.env"
            self.L.env_read(self.ids_at(line), name, self.rel, line, how, "default" if has_default else "optional")
        for m in JS_ENV_DESTRUCT.finditer(src):
            line = line_of(m.start())
            for part in m.group(1).split(","):
                part = part.strip()
                if not part or part.startswith("..."):
                    continue
                key = part.split(":")[0].split("=")[0].strip()
                self.L.env_read(self.ids_at(line), key, self.rel, line, "process.env", "default" if "=" in part else "optional")

    def _services(self, src: str, line_of, sdk_imports) -> None:
        locals_, clients = self.locals, self.clients
        used: set[str] = set()
        targets = [(local, hit, "SDK call", "declared") for local, hit in locals_.items()] + \
                  [(var, hit, "client call", "inferred") for var, hit in clients.items()]
        for name, hit, how, basis in targets:
            pat = re.compile(r"(?<![\w$.])" + re.escape(name) + r"(?:\s*\.\s*[\w$]+)*\s*\(|new\s+" + re.escape(name) + r"\b")
            for m in pat.finditer(src):
                line = line_of(m.start())
                self.L.use(self.ids_at(line), hit[0], self.rel, line, f"{how}: {name}", basis, hit[1])
                used.add(hit[0][0])
        for imp, svc in sdk_imports:
            if svc[0] not in used and not imp.module.startswith("@aws-sdk/"):
                self.L.use([self.f.node_id], svc, self.rel, imp.line, "imports the SDK", "declared", imp.module)
                used.add(svc[0])

    def _graph(self, src: str, line_of) -> None:
        m0 = JS_GRAPH.search(src)
        if not m0:
            return
        var = m0.group(1) or "graph"
        gr = _Graph(self.rel, var, line_of(m0.start()), ())
        gr.framework = "LangGraph.js"
        cm = JS_COMPILE.search(src)
        if cm:
            gr.compiled_as = cm.group(1)
        for m in JS_ADD_NODE.finditer(src):
            name, fn = m.group(1), m.group(2)
            obj, _, nm = fn.rpartition(".")
            st = _Step(name, line_of(m.start()), len(gr.steps) + 1, runs=fn)
            st.targets = [x for x in self.b._resolve_name(self.f, nm, obj or None) if self.L.g.nodes.get(x) is not None]
            if not st.targets:
                gr.unresolved.append(f"step {name!r} runs {fn}")
            gr.steps.setdefault(name, st)
        q = lambda s: "__start__" if s == "START" else "__end__" if s == "END" else s.strip("'\"")  # noqa: E731
        for m in JS_ADD_EDGE.finditer(src):
            gr.edges.setdefault((q(m.group(1)), q(m.group(2))), {"kind": "edge", "line": line_of(m.start())})
        for m in JS_COND.finditer(src):
            src_step, router, mapping = q(m.group(1)), m.group(2), m.group(3) or ""
            obj, _, nm = router.rpartition(".")
            rids = [x for x in self.b._resolve_name(self.f, nm, obj or None) if self.L.g.nodes.get(x) is not None]
            targets = re.findall(r"""['"]([\w$.-]+)['"]\s*(?:,|\]|$)|:\s*['"]([\w$.-]+)['"]|:\s*(END)\b""", mapping)
            names = {q(a or b or c) for a, b, c in targets if (a or b or c)}
            if not names:
                gr.unresolved.append(f"where {src_step!r} goes next is decided at run time by {router}")
            for t in names:
                gr.edges.setdefault((src_step, t), {"kind": "conditional", "line": line_of(m.start()), "router": router,
                                                     "router_ids": rids, "how": f"via {router}"})
        self.L.emit_graph(gr, self.f)

    def _mcp(self, src: str, line_of) -> None:
        servers = list(JS_MCP.finditer(src))
        if not servers:
            return
        m = servers[0]
        sid = f"MCPSERVER:{self.rel}:{m.group(2)}"
        tools = [(k, n, line_of(t.start())) for t in JS_MCP_TOOL.finditer(src) for k, n in [(t.group(1), t.group(2))]]
        self.L.g.add_node(Node(sid, G.MCP_SERVER, m.group(2), self.rel, {
            "line": line_of(m.start()), "file_path": self.rel, "framework": m.group(1),
            "tools": sum(1 for k, _, _ in tools if "ool" in k), "resources": sum(1 for k, _, _ in tools if "esource" in k),
            "prompts": sum(1 for k, _, _ in tools if "rompt" in k),
            "tool_names": [n for k, n, _ in tools][:30]}))
        if sid not in self.L.top:
            self.L.top.append(sid)


def _bisect(starts: list[int], pos: int) -> int:
    lo, hi = 0, len(starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= pos:
            lo = mid + 1
        else:
            hi = mid
    return lo


def add_genai_layer(b: "Builder") -> None:
    Layer(b).run()
