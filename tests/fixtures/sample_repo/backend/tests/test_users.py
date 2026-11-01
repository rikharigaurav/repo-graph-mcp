from fastapi.testclient import TestClient

from app.main import app
from app.services import normalize

client = TestClient(app)


def test_health():
    assert client.get("/health").status_code == 200


def test_get_user():
    assert client.get("/users/42").status_code == 200
    assert client.get("/nowhere").status_code == 404


def test_normalize():
    assert normalize({"A": 1}) == {"a": 1}
