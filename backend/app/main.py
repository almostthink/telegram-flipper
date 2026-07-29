"""Сборка FastAPI-приложения: API, WebSocket и раздача собранного фронтенда."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import paths
from app.api import routes_system, ws
from app.config import API_PREFIX, APP_VERSION, settings
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    log.info("Telegram Gift Flipper %s", APP_VERSION)
    log.info("Папка данных: %s", paths.data_dir())
    log.info("Режим: %s", "PAPER (симуляция)" if settings.paper_mode else "LIVE")
    yield
    log.info("Остановка")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Telegram Gift Flipper",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    if settings.dev_mode:
        # Vite поднимается на своём порту, поэтому в разработке нужен CORS.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(routes_system.router, prefix=API_PREFIX)
    app.include_router(ws.router, prefix="/api")

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Отдаём SPA. Любой неизвестный путь ведёт на index.html — роутинг на клиенте."""
    static = paths.static_dir()
    index = static / "index.html"

    if not index.exists():
        @app.get("/{full_path:path}")
        async def frontend_missing(full_path: str):
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Фронтенд не собран",
                    "hint": "Выполните: cd frontend && npm install && npm run build",
                    "expected_at": str(static),
                },
            )
        return

    app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        # Отдаём реальный файл, если он есть (favicon и т.п.), иначе — index.html.
        candidate = (static / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(static.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
