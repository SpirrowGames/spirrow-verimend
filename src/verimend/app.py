"""FastAPI application for the Verimend service (docs/design.md section 7)."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from verimend import __version__
from verimend.settings import Settings, get_settings


class HealthResponse(BaseModel):
    """Payload of ``GET /health``, polled by the platform service_health check."""

    status: str
    service: str
    version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    settings = settings or get_settings()
    app = FastAPI(title="Verimend", version=__version__)
    app.state.settings = settings

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service="verimend", version=__version__)

    return app


app = create_app()
