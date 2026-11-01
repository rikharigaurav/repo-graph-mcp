from .db import Store


class BaseService:
    def log(self, msg: str) -> None:
        print(msg)


class UserService(BaseService):
    """Business logic for users."""

    def __init__(self, db: Store):
        self.db = db

    def fetch(self, user_id: int):
        self.log("fetch")
        return self.db.get(user_id)

    def create(self, data: dict):
        self.log("create")
        return self.db.put(data)


def normalize(data: dict) -> dict:
    return {k.lower(): v for k, v in data.items()}
