"""Optional RAG prelude — pre-debate retrieval of stakeholder call excerpts.

Enabled by the --rag-prelude server flag. When a debate call carries both
`rag_access_token` and `rag_base_url`, the wrapper runs one hybrid search
over the call-transcript corpus and prepends the found excerpts to the
debate content, so the digital twins ground their takes in what people
actually said on calls.

The token is a short-lived bearer secret handed to us per call: it is never
logged, never stored, and never echoed into results. RAG is an enrichment,
not a dependency — every failure path degrades to a plain debate.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

SEARCH_TIMEOUT_SECONDS = 15.0
RETRY_BACKOFF_SECONDS = 1.0
# Hard limits on the RAG side.
MAX_QUERY_CHARS = 4096
TOP_K = 5
COLLECTION = "stt-calls"

_CYRILLIC = re.compile("[а-яА-ЯёЁ]")


def _format_chunks(chunks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for chunk in chunks:
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        notes: list[str] = []
        score = chunk.get("score")
        if isinstance(score, (int, float)):
            notes.append(f"score {score:.2f}")
        source = (chunk.get("metadata") or {}).get("source_uri")
        if source:
            notes.append(f"source: {source}")
        suffix = f" ({'; '.join(notes)})" if notes else ""
        lines.append(f"{len(lines) + 1}. {text}{suffix}")
    return "\n".join(lines)


async def fetch_rag_prelude(base_url: str, token: str, content: str) -> str | None:
    """One hybrid search over the call corpus; returns formatted excerpts.

    Error policy per the integration contract: 401 means the token is dead —
    no retry with it; network errors and 5xx get exactly one retry with a
    short backoff; any other non-2xx gives up immediately. Every failure
    (or an empty result) returns None so the debate runs without RAG.
    """
    body: dict[str, Any] = {
        "query": content.strip()[:MAX_QUERY_CHARS],
        "collection": COLLECTION,
        "top_k": TOP_K,
    }
    if _CYRILLIC.search(content):
        body["language"] = "ru"

    url = base_url.rstrip("/") + "/v1/search"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(SEARCH_TIMEOUT_SECONDS, connect=5.0)
        ) as client:
            try:
                r: httpx.Response | None = await client.post(
                    url, json=body, headers=headers
                )
            except httpx.HTTPError:
                r = None
            if r is None or r.status_code >= 500:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                r = await client.post(url, json=body, headers=headers)
            if not r.is_success:
                return None
            chunks = r.json().get("chunks") or []
    except Exception:
        return None
    return _format_chunks(chunks) or None


def with_rag_prelude(content: str, prelude: str) -> str:
    """Compose the enriched debate content."""
    return f"### Question\n{content}\n\n### Useful info\n{prelude}"
