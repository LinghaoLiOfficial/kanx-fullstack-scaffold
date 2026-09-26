from fastapi.testclient import TestClient

from {{PACKAGE_NAME}}.app import app


def test_live() -> None:
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
