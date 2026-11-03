import os

from neo4j import GraphDatabase

driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=("neo4j", os.getenv("NEO4J_PASSWORD")))


def run(query: str):
    with driver.session() as session:
        return session.run(query)
