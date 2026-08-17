import os
import uuid

os.environ["SENSEN_DATABASE_URL"] = "sqlite:///./test_saas.db"

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def api_key(client):
    email = f"pytest-{uuid.uuid4().hex[:8]}@sensen.dev"
    resp = client.post("/register", json={"email": email})
    assert resp.status_code == 200, resp.text
    return resp.json()["api_key"]


@pytest.fixture
def scan(client, api_key):
    def _scan(text, **kwargs):
        payload = {"text": text, **kwargs}
        return client.post("/api/v1/scan", json=payload, headers={"X-API-Key": api_key})

    return _scan
