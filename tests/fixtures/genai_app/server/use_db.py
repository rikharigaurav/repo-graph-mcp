from server.db import driver


def count_nodes() -> int:
    return driver.execute_query("MATCH (n) RETURN count(n)")
