"""Optional: write a graph into Neo4j using the Read-Code-Structure schema.

Node ids are namespaced by repo (`owner/name::<id>`) and every node gets a
`repo` property, so several repos can live in one database and a re-push only
replaces that repo's nodes. Requires `pip install "repo-graph-mcp[neo4j]"`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .graph import CodeGraph


def _clean(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) for x in v):
        return list(v)
    return json.dumps(v, default=str)


def push_graph(g: CodeGraph, uri: str, user: str, password: str, database: str | None = None,
               replace: bool = True, batch: int = 1000) -> dict[str, int]:
    from neo4j import GraphDatabase  # imported lazily: optional dependency

    repo = g.repo.get("display") or g.repo.get("name") or "repo"
    ns = f"{repo}::"
    by_label: dict[str, list[dict]] = defaultdict(list)
    for n in g.nodes.values():
        props = {k: _clean(v) for k, v in n.props.items() if _clean(v) is not None}
        props.update({"name": n.name, "path": n.path, "repo": repo, "local_id": n.id})
        by_label[n.label].append({"id": ns + n.id, "props": props})
    by_rel: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for s, t, r, p in g.edges:
        key = (r, g.nodes[s].label, g.nodes[t].label)
        by_rel[key].append({"s": ns + s, "t": ns + t, "p": {k: _clean(v) for k, v in p.items()}})

    counts = {"nodes": 0, "relationships": 0, "deleted": 0}
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            for label in by_label:
                session.run(f"CREATE INDEX IF NOT EXISTS FOR (n:`{label}`) ON (n.id)").consume()
            if replace:
                res = session.run("MATCH (n {repo: $repo}) DETACH DELETE n RETURN count(n) AS c", repo=repo).single()
                counts["deleted"] = res["c"] if res else 0
            for label, rows in by_label.items():
                for i in range(0, len(rows), batch):
                    session.run(
                        f"UNWIND $rows AS row MERGE (n:`{label}` {{id: row.id}}) SET n += row.props",
                        rows=rows[i:i + batch],
                    ).consume()
                    counts["nodes"] += len(rows[i:i + batch])
            for (rel, sl, tl), rows in by_rel.items():
                for i in range(0, len(rows), batch):
                    session.run(
                        f"UNWIND $rows AS row MATCH (a:`{sl}` {{id: row.s}}) MATCH (b:`{tl}` {{id: row.t}}) "
                        f"MERGE (a)-[r:`{rel}`]->(b) SET r += row.p",
                        rows=rows[i:i + batch],
                    ).consume()
                    counts["relationships"] += len(rows[i:i + batch])
    return counts
