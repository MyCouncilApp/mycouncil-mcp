"""Thin async HTTP client over the myCouncil public API."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://app.mycouncil.xyz"

# Models metadata cache TTL in seconds. The whitelist changes rarely (manual
# server-side edits), so caching for 10 minutes is generous.
MODELS_CACHE_TTL = 600.0


def get_base_url() -> str:
    return os.environ.get("MYCOUNCIL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def get_api_key() -> str:
    key = os.environ.get("MYCOUNCIL_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "MYCOUNCIL_API_KEY is not set. Generate a key at "
            "https://app.mycouncil.xyz under Account → API and pass it via "
            "`--env MYCOUNCIL_API_KEY=mc_...` when registering this MCP server."
        )
    return key


class MyCouncilError(RuntimeError):
    """Raised when the myCouncil API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"myCouncil API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _raise_for_status(r: httpx.Response) -> None:
    if r.is_success:
        return
    try:
        detail = r.json().get("detail", r.text)
    except Exception:
        detail = r.text or r.reason_phrase
    raise MyCouncilError(r.status_code, str(detail))


class MyCouncilClient:
    """Async client wrapping `/api/v1/*` and conversation utility endpoints."""

    # Process-wide cache for /api/available-models. Shared across instances
    # because MCP-server creates a fresh client per tool call.
    _models_cache: list[dict] | None = None
    _models_cache_ts: float = 0.0

    def __init__(self, timeout_seconds: float = 60.0):
        self._client = httpx.AsyncClient(
            base_url=get_base_url(),
            headers={"Authorization": f"Bearer {get_api_key()}"},
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
        )

    async def __aenter__(self) -> "MyCouncilClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def balance(self) -> dict:
        r = await self._client.get("/api/v1/balance")
        _raise_for_status(r)
        return r.json()

    async def list_models(self) -> list[dict]:
        """Fetch the available-models whitelist with tier metadata.

        Cached in-process for MODELS_CACHE_TTL seconds because the whitelist
        is small and changes very rarely (manual edits to server-side TOML).
        """
        now = time.monotonic()
        if (
            MyCouncilClient._models_cache is not None
            and now - MyCouncilClient._models_cache_ts < MODELS_CACHE_TTL
        ):
            return MyCouncilClient._models_cache

        r = await self._client.get("/api/available-models")
        _raise_for_status(r)
        models = r.json()
        MyCouncilClient._models_cache = models
        MyCouncilClient._models_cache_ts = now
        return models

    async def auto_config(
        self, content: str, file_names: list[str] | None = None
    ) -> dict:
        body: dict[str, Any] = {"content": content}
        if file_names:
            body["file_names"] = file_names
        r = await self._client.post("/api/v1/auto-config", json=body)
        _raise_for_status(r)
        return r.json()

    async def debate_start(
        self,
        content: str,
        config: dict | None = None,
        file_paths: list[str] | None = None,
    ) -> dict:
        data: dict[str, str] = {"content": content}
        if config is not None:
            data["config"] = json.dumps(config)

        files_payload: list[tuple[str, tuple[str, bytes, str]]] = []
        if file_paths:
            for raw in file_paths:
                p = Path(raw).expanduser().resolve()
                if not p.is_file():
                    raise FileNotFoundError(f"File not found: {p}")
                files_payload.append(
                    ("files", (p.name, p.read_bytes(), "application/octet-stream"))
                )

        # Multipart uploads may take a while; bump per-call timeout.
        r = await self._client.post(
            "/api/v1/debate",
            data=data,
            files=files_payload or None,
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        _raise_for_status(r)
        return r.json()

    async def job(self, job_id: str) -> dict:
        r = await self._client.get(f"/api/v1/jobs/{job_id}")
        _raise_for_status(r)
        return r.json()

    async def share_enable(self, conversation_id: str) -> dict:
        r = await self._client.post(
            f"/api/conversations/{conversation_id}/share"
        )
        _raise_for_status(r)
        return r.json()

    async def export_pdf(self, conversation_id: str, destination: Path) -> Path:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self._client.stream(
            "GET",
            f"/api/conversations/{conversation_id}/export/pdf",
            timeout=httpx.Timeout(120.0, connect=10.0),
        ) as r:
            if not r.is_success:
                # Need to read the body for the error message.
                body = await r.aread()
                try:
                    detail = json.loads(body).get("detail", body.decode("utf-8", "replace"))
                except Exception:
                    detail = body.decode("utf-8", "replace") or r.reason_phrase
                raise MyCouncilError(r.status_code, str(detail))
            with destination.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
        return destination
