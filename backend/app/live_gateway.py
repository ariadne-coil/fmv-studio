from __future__ import annotations

import asyncio
import contextlib
import os
import ssl

import certifi
import google.auth
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from google.auth.transport.requests import Request as GoogleAuthRequest

from app.genai_runtime import get_gcp_project, get_vertex_media_location


app = FastAPI(
    title="FMV Studio Live Director Gateway",
    description="Public WebSocket gateway that proxies browser Live Director sessions to Vertex AI Live API.",
)


def _live_director_service_url() -> str:
    location = get_vertex_media_location()
    return (
        f"wss://{location}-aiplatform.googleapis.com"
        "/ws/google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent"
    )


def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _allowed_origins() -> set[str]:
    configured = (os.getenv("FMV_LIVE_DIRECTOR_ALLOWED_ORIGINS") or "").strip()
    if not configured:
        return set()
    return {
        _normalize_origin(origin)
        for origin in configured.split(",")
        if origin.strip()
    }


def _is_origin_allowed(origin: str | None) -> bool:
    allowed = _allowed_origins()
    if not allowed:
        return True
    if not origin:
        return False
    return _normalize_origin(origin) in allowed


def _generate_access_token() -> str:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(GoogleAuthRequest())
    return credentials.token


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "project": get_gcp_project(),
            "service_url": _live_director_service_url(),
        }
    )


async def _relay_client_to_upstream(client: WebSocket, upstream: websockets.ClientConnection) -> None:
    try:
        while True:
            message = await client.receive_text()
            await upstream.send(message)
    except WebSocketDisconnect:
        pass
    finally:
        await upstream.close()


async def _relay_upstream_to_client(upstream: websockets.ClientConnection, client: WebSocket) -> None:
    try:
        async for message in upstream:
            if isinstance(message, bytes):
                await client.send_text(message.decode("utf-8"))
            else:
                await client.send_text(message)
    finally:
        await client.close()


@app.websocket("/ws/live-director")
async def live_director_proxy(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if not _is_origin_allowed(origin):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    try:
        bearer_token = await asyncio.to_thread(_generate_access_token)
    except Exception:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
    }

    try:
        async with websockets.connect(
            _live_director_service_url(),
            additional_headers=headers,
            ssl=ssl_context,
            max_size=None,
            open_timeout=30,
        ) as upstream:
            client_to_upstream = asyncio.create_task(_relay_client_to_upstream(websocket, upstream))
            upstream_to_client = asyncio.create_task(_relay_upstream_to_client(upstream, websocket))

            done, pending = await asyncio.wait(
                {client_to_upstream, upstream_to_client},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
