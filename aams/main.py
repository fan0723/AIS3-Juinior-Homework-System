"""ASGI applications: main site + isolated preview origin."""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .db import init_db
from .deps import RedirectTo, templates

log = logging.getLogger("aams")

UPLOAD_PATHS = re.compile(r"^/(staff/assignments/\d+/similarity|assignments/\d+/submit|staff/courses/\d+/materials|admin/import)$")


class BodyLimitMiddleware:
    """在 multipart 解析前就以「實際收到的 bytes」限制 request body（不只信任 Content-Length）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if UPLOAD_PATHS.match(path):
            limit = settings.max_material_bytes + 1_000_000 if "/materials" in path else settings.max_upload_request_bytes
        else:
            limit = 1_000_000
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    if int(v) > limit:
                        return await _too_large(send)
                except ValueError:
                    return await _too_large(send)
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return msg

        async def tracking_send(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send):
    body = "上傳內容超過系統上限。".encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def _send(msg):
            if msg["type"] == "http.response.start":
                headers = [(k, v) for k, v in msg.get("headers", []) if k.lower() != b"server"]
                names = {k.lower() for k, _ in headers}
                frame_src = (settings.preview_url or "*").encode() if settings.preview_url else b"http: https:"
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"same-origin",
                    b"x-frame-options": b"DENY",
                    b"cross-origin-opener-policy": b"same-origin",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                    b"content-security-policy": (
                        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                        b"img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; "
                        b"frame-ancestors 'none'; frame-src " + frame_src
                    ),
                }
                if settings.cookie_secure:
                    extra[b"strict-transport-security"] = b"max-age=31536000"
                for k, v in extra.items():
                    if k not in names:
                        headers.append((k, v))
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    path = scope.get("path", "")
                    headers.append((b"cache-control", b"public, max-age=3600" if path.startswith("/static/") else b"no-store"))
                msg["headers"] = headers
            await send(msg)

        await self.app(scope, receive, _send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.secret_key or len(settings.secret_key) < 32:
        raise RuntimeError("AAMS_SECRET_KEY 未設定或太短，請先執行：python manage.py init")
    init_db()
    from .similarity import fail_interrupted_runs

    fail_interrupted_runs()
    if settings.run_worker:
        from .storage import start_worker, stop_worker

        start_worker()
        yield
        stop_worker()
    else:
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="AIS3 Junior 作業繳交系統", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    from .routes import (admin, announcements, appeals, auth, discord, downloads, files, grading, similarity, staff,
                         student)

    for mod in (auth, student, announcements, appeals, downloads, similarity, discord, staff, grading, files, admin):
        app.include_router(mod.router)

    @app.exception_handler(RedirectTo)
    async def _redirect(request: Request, exc: RedirectTo):
        return RedirectResponse(exc.url, status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        titles = {400: "請求錯誤", 403: "沒有權限", 404: "找不到頁面", 409: "狀態衝突", 413: "檔案太大", 429: "請求過於頻繁"}
        detail = exc.detail if isinstance(exc.detail, str) else ""
        if exc.status_code == 404 and detail in ("Not Found", ""):
            detail = "找不到此頁面或資源。"
        view = getattr(request.state, "view", None)
        return templates.TemplateResponse(
            request, "error.html",
            {"request": request, "ctx": view.ctx if view else None, "me": view.me if view else None,
             "csrf": view.csrf if view else "",
             "unread": 0, "ann_unread": 0, "flash": "", "code": exc.status_code, "title": titles.get(exc.status_code, "錯誤"),
             "detail": detail},
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # pragma: no cover
        from . import monitor

        monitor.record_error()
        log.exception("unhandled error on %s", request.url.path)
        return HTMLResponse("<h1>500 系統錯誤</h1><p>請聯絡工作人員。</p>", status_code=500)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok")

    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


app = create_app()


def create_preview_app() -> FastAPI:
    from .routes.files import preview_router

    papp = FastAPI(title="AAMS preview", docs_url=None, redoc_url=None, openapi_url=None)
    papp.include_router(preview_router)

    @papp.exception_handler(StarletteHTTPException)
    async def _err(request: Request, exc: StarletteHTTPException):
        return PlainTextResponse(f"{exc.status_code}", status_code=exc.status_code,
                                 headers={"Content-Security-Policy": "default-src 'none'; sandbox"})

    return papp


preview_app = create_preview_app()

__all__ = ["app", "preview_app", "HTTPException"]
