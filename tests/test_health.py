"""The /health endpoint the platform service_health check polls."""

from fastapi.testclient import TestClient

from verimend import __version__
from verimend.app import create_app
from verimend.settings import Settings


def test_health_returns_200() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "verimend", "version": __version__}


def test_default_port_is_8118() -> None:
    """docs/design.md section 3 pins the service to :8118."""
    assert Settings().port == 8118


def test_port_is_overridable_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("VERIMEND_PORT", "9118")
    assert Settings().port == 9118


def test_unknown_route_is_404() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/nope").status_code == 404
