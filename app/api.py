from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

import zendriver as zd
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import VERSION, settings
from .models import ProxyConfig, V1Request, V1Response
from .sessions import _base_browser_args, session_manager
from .solver import solve_request

logger = logging.getLogger(__name__)

_cached_user_agent: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await session_manager.startup()
    logger.info("TurnstileSolver v%s listening on %s:%d", VERSION, settings.HOST, settings.PORT)
    yield
    await session_manager.shutdown()


app = FastAPI(title="TurnstileSolver", version=VERSION, lifespan=lifespan)


def _build_proxy_url(proxy: Optional[ProxyConfig]) -> Optional[str]:
    if proxy is None:
        return None
    if proxy.username and proxy.password:
        parsed = urlparse(proxy.url)
        netloc = f"{proxy.username}:{proxy.password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))
    return proxy.url


async def _get_user_agent() -> str:
    global _cached_user_agent
    if _cached_user_agent:
        return _cached_user_agent
    try:
        browser = await zd.start(
            headless=settings.HEADLESS,
            browser_executable_path=settings.BROWSER_EXECUTABLE_PATH,
            browser_args=_base_browser_args(),
            sandbox=False,
        )
        tab = await browser.get("about:blank")
        ua = await tab.evaluate("navigator.userAgent")
        await browser.stop()
        _cached_user_agent = ua or ""
    except Exception as e:
        logger.warning("Could not detect user-agent: %s", e)
        _cached_user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    return _cached_user_agent


@app.get("/")
async def root():
    ua = await _get_user_agent()
    return {"msg": "FlareSolverr is ready!", "version": VERSION, "userAgent": ua}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1")
async def v1(req: V1Request) -> JSONResponse:
    start_ts = int(time.time() * 1000)
    try:
        data = await _dispatch(req, start_ts)
    except Exception as e:
        end_ts = int(time.time() * 1000)
        logger.exception("Error handling /v1 cmd=%r: %s", req.cmd, e)
        data = V1Response(
            status="error",
            message=str(e),
            startTimestamp=start_ts,
            endTimestamp=end_ts,
            version=VERSION,
        ).model_dump(exclude_none=True)
    return JSONResponse(content=data)


async def _dispatch(req: V1Request, start_ts: int) -> Dict[str, Any]:
    proxy_url = _build_proxy_url(req.proxy)

    if req.cmd == "sessions.create":
        sid = await session_manager.create(
            session_id=req.session,
            proxy=proxy_url,
            ttl_minutes=req.session_ttl_minutes,
        )
        return {
            "status": "ok",
            "message": f"Session {sid} created successfully.",
            "session": sid,
            "startTimestamp": start_ts,
            "endTimestamp": int(time.time() * 1000),
            "version": VERSION,
        }

    if req.cmd == "sessions.list":
        ids = await session_manager.list_ids()
        return {
            "status": "ok",
            "message": "",
            "sessions": ids,
            "startTimestamp": start_ts,
            "endTimestamp": int(time.time() * 1000),
            "version": VERSION,
        }

    if req.cmd == "sessions.destroy":
        if not req.session:
            raise ValueError("session field is required for sessions.destroy")
        destroyed = await session_manager.destroy(req.session)
        if not destroyed:
            raise ValueError("The session doesn't exist.")
        return {
            "status": "ok",
            "message": f"Session {req.session} destroyed successfully.",
            "startTimestamp": start_ts,
            "endTimestamp": int(time.time() * 1000),
            "version": VERSION,
        }

    if req.cmd in ("request.get", "request.post"):
        if not req.url:
            raise ValueError(f"url field is required for {req.cmd}")
        if req.cmd == "request.post" and not req.postData:
            raise ValueError("postData field is required for request.post")

        timeout_ms = min(req.maxTimeout, settings.MAX_TIMEOUT)
        solution = await solve_request(
            url=req.url,
            method="POST" if req.cmd == "request.post" else "GET",
            post_data=req.postData,
            cookies=req.cookies,
            proxy=proxy_url,
            timeout_ms=timeout_ms,
            return_only_cookies=req.returnOnlyCookies,
            return_screenshot=req.returnScreenshot,
            disable_media=req.disableMedia,
            session_id=req.session,
        )
        end_ts = int(time.time() * 1000)
        return V1Response(
            status="ok",
            message="",
            startTimestamp=start_ts,
            endTimestamp=end_ts,
            version=VERSION,
            solution=solution,
        ).model_dump(exclude_none=True)

    raise ValueError(
        f"cmd={req.cmd!r} is invalid. "
        "Valid: sessions.create, sessions.list, sessions.destroy, request.get, request.post"
    )
