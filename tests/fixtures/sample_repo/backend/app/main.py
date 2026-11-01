"""FastAPI entry point for the sample app."""

from fastapi import APIRouter, FastAPI

from .db import store
from .services import UserService, normalize

app = FastAPI()
router = APIRouter(prefix="/users")


@router.get("/{user_id}")
def get_user(user_id: int):
    """Return one user."""
    return UserService(store).fetch(user_id)


@router.post("/")
def create_user(payload: dict):
    return UserService(store).create(normalize(payload))


@app.get("/health")
def health():
    return {"ok": store.ping()}


app.include_router(router)
