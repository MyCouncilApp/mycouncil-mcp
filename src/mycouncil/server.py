"""myCouncil MCP server — exposes the public API as MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .client import MyCouncilClient, MyCouncilError
from .rag import fetch_rag_prelude, with_rag_prelude
from .tier import fill_models_by_tier

POLL_INTERVAL_SECONDS = 6
DEFAULT_TIMEOUT_MINUTES = 20
TERMINAL_STATUSES = {"complete", "failed"}

# stateless_http=True makes the streamable-http transport create a fresh
# transport per request (no session affinity) — the right model for a thin
# stateless relay and the cleanest path for concurrent debates. It is a no-op
# for the default stdio transport.
mcp = FastMCP("mycouncil", stateless_http=True)

# Auth mode, set by main(). "shared": every call uses the single
# MYCOUNCIL_API_KEY from the environment (pre-0.4.0 behaviour, default).
# "per-request" (streamable-http only): each HTTP call must carry its own
# myCouncil key in the X-MyCouncil-Key or Authorization: Bearer header.
_AUTH_MODE = "shared"


# Orientation guide for agents. Returned by mycouncil_info. Kept as a
# module constant so it's easy to update in one place.
_INFO_GUIDE = """\
# myCouncil — quick guide for agents

Run multi-LLM debates: 2-5 expert models discuss a user's question across
3 stages (individual takes -> anonymous peer review -> chairman synthesis).

## When to call it

Use myCouncil when the answer benefits from multiple perspectives:
decisions with trade-offs, strategy, design choices, risk analysis,
multi-factor comparisons. Skip for trivial Q&A or things you can answer
directly without external input.

## Tools at a glance

| Tool | When to use |
|---|---|
| `mycouncil_balance` | Quota check before starting — costs nothing. |
| `mycouncil_info` | This guide. Read once per session. |
| `mycouncil_list_roles` | Browse the curated expert-role catalogue when composing a custom council. |
| `mycouncil_auto_config` | Preview the planner's choice (roles + tier) before running. |
| `mycouncil_debate` | Blocking — one call, returns the finished result. The default path. |
| `mycouncil_debate_start` + `mycouncil_debate_status` | Async — fire and poll, useful for long debates. |
| `mycouncil_share` | Export an existing conversation to a public link or a PDF. |

## Three flows

**1. Default (zero-config).** Simplest, use it 90% of the time:
```
mycouncil_debate(content="<the user's question>")
```
Server picks tier, roles, and models. Returns a PDF path by default
(override with `return_as="transcript"` if you want the raw JSON).

**2. Preview + tweak.** When the user wants to see what'll be discussed
or you want to escalate the tier:
```
result = mycouncil_auto_config(content="<question>")
# inspect result["config"]["tier"], result["config"]["experts"],
# result["observation"]. Optionally edit config["tier"] or roles.
mycouncil_debate(content="<same>", config=result["config"])
```

**3. Custom council.** When the user explicitly names roles they want:
```
roles = mycouncil_list_roles()
# pick role IDs from roles["roles"][i]["id"] (e.g. "technology", "legal")
config = {
    "session_type": 1,
    "tier": "balanced",
    "experts": [
        {"role_name": "Tech Lead", "role_preset": "technology", "temperature": 0.4},
        {"role_name": "Legal Reviewer", "role_preset": "legal", "temperature": 0.3},
    ],
    "chairman": {"temperature": 0.5},
}
mycouncil_debate(content="<question>", config=config)
```

For roles not in the catalogue, use `role_preset="custom"` plus a full
`role_description` (markdown). Don't write custom descriptions if a
preset fits — it wastes tokens and the curated descriptions are higher
quality.

## Tier (operating mode)

`fast` / `balanced` / `deep` — describes how much time the council
spends, not a quality rank. The planner picks one by default. **Do not
escalate to `deep` reflexively** — it's much more expensive and meant
for high-stakes / irreversible decisions. `deep` is only available in
`advanced` auto-config mode; check `mycouncil_balance`.

## Quota awareness

- 1 round per debate (refunded if the server fails before stage 1).
- `mycouncil_auto_config` is free in `standard` mode, **1 round in
  `advanced`**. If you're going to call `mycouncil_debate` right after
  with the same content, skip auto_config and let debate run it
  internally (avoids double-billing).
- Type-2 debates (adversarial, set by the planner) pre-reserve up to
  `max_rounds` rounds. Unused rounds are refunded after the debate.

## Result formats (mycouncil_debate)

- `return_as="pdf"` (default) — saves to `save_path` or
  `./mycouncil-<id>-<timestamp>.pdf`. The user can open the file.
- `return_as="transcript"` — full JSON: stage1 takes, stage2 peer
  reviews, stage3 synthesis. Use when you need to reason over the
  result, not just hand it to the user.
- `return_as="link"` — generates a public share URL. WARNING: the URL
  is publicly accessible to anyone who has it. Confirm with the user
  before using.

## Timeouts

Default 20 minutes for blocking `mycouncil_debate`. Most debates finish
in 5-15 min. Do not lower the timeout below 20 — large councils with
files / deep tier can push past 15 min.
"""


def _default_pdf_path(conversation_id: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    short = conversation_id[:8] if conversation_id else "debate"
    return Path.cwd() / f"mycouncil-{short}-{ts}.pdf"


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, MyCouncilError):
        return {
            "error": "api_error",
            "status_code": exc.status_code,
            "detail": exc.detail,
        }
    return {"error": type(exc).__name__, "detail": str(exc)}


def _request_api_key() -> str | None:
    """Per-call myCouncil API key, or None to use the shared env key.

    In per-request mode the key comes from the current HTTP request's
    headers. `X-MyCouncil-Key: mc_...` wins over `Authorization: Bearer
    mc_...` — the dedicated header survives proxies that overwrite
    Authorization with their own gateway credentials. A missing key is an
    error rather than a fallback: silently using the operator's env key
    would bill the wrong account.
    """
    if _AUTH_MODE != "per-request":
        return None

    request = None
    try:
        request = mcp.get_context().request_context.request
    except ValueError:
        pass
    headers = getattr(request, "headers", None)
    if headers is not None:
        key = (headers.get("x-mycouncil-key") or "").strip()
        if not key:
            auth = (headers.get("authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                key = auth[len("bearer ") :].strip()
        if key:
            return key
    raise MyCouncilError(
        401,
        "This server runs in per-request auth mode: pass your myCouncil API "
        "key on every HTTP call via the `X-MyCouncil-Key: mc_...` or "
        "`Authorization: Bearer mc_...` header.",
    )


def _strip_models_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """Remove concrete model IDs from a config returned by auto-config.

    The agent should think in terms of roles + tier, never in terms of
    specific provider model names. When the agent later passes the config
    back to `mycouncil_debate`, MCP fills the missing models locally based
    on `config["tier"]`.
    """
    cleaned = dict(config)
    cleaned["experts"] = [
        {k: v for k, v in e.items() if k != "model"}
        for e in config.get("experts") or []
    ]
    chairman = dict(config.get("chairman") or {})
    chairman.pop("model", None)
    cleaned["chairman"] = chairman
    return cleaned


def _needs_model_fill(config: dict[str, Any] | None) -> bool:
    """Check whether the config has at least one missing expert/chairman model.

    Agents typically receive a model-less config from `mycouncil_auto_config`,
    so on the way back into /debate we need to fill before submitting.
    """
    if not config:
        return False
    experts = config.get("experts") or []
    if any(not e.get("model") for e in experts):
        return True
    chairman = config.get("chairman") or {}
    return bool(experts) and not chairman.get("model")


async def _prepare_config_for_debate(
    client: MyCouncilClient, config: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Fill models from tier if the agent supplied a stripped config."""
    if not _needs_model_fill(config):
        return config
    models = await client.list_models()
    return fill_models_by_tier(config, models)


async def _maybe_rag_prelude(
    content: str, token: str | None, base_url: str | None
) -> tuple[str, str | None]:
    """Enrich debate content with RAG excerpts when both params are present.

    Returns (content, status): status is None when RAG wasn't requested,
    otherwise "applied" / "skipped (...)" — surfaced in the tool result so
    integrators can verify the prelude ran without us echoing the token.
    """
    if not (token and base_url):
        return content, None
    prelude = await fetch_rag_prelude(base_url, token, content)
    if prelude:
        return with_rag_prelude(content, prelude), "applied"
    return content, "skipped (RAG search failed or returned no excerpts)"


@mcp.tool()
async def mycouncil_info() -> dict[str, Any]:
    """Orientation for agents — read this once at the start of a session.

    Returns a short guide covering when to use myCouncil, the three
    typical flows (zero-config / preview-and-tweak / custom council),
    the tier system, quota awareness, and result formats. Self-contained;
    you do not need to read the per-tool descriptions if you read this.
    """
    return {"guide": _INFO_GUIDE}


@mcp.tool()
async def mycouncil_balance() -> dict[str, Any]:
    """Return remaining rounds (quota) and the account's current
    auto-config mode (`standard` is free, `advanced` costs 1 round per
    auto-config call). Source of truth for what is left on the account.
    """
    try:
        async with MyCouncilClient(api_key=_request_api_key()) as client:
            return await client.balance()
    except Exception as exc:
        return _error_payload(exc)


@mcp.tool()
async def mycouncil_list_roles(
    scope: Literal["all", "system", "user", "team", "public"] = "all",
) -> dict[str, Any]:
    """List expert roles available for composing custom debate configs.

    The server returns curated system roles plus any custom roles the
    account or its team has saved. Use a role's `id` as `role_preset`
    (or `role_id`) when composing a config for
    `mycouncil_debate(_start)`.

    If no preset fits the user's question, use `role_preset="custom"`
    plus a full `role_description` markdown — but only when nothing in
    the catalogue is close, otherwise it wastes tokens.

    Args:
        scope: Filter by where the role comes from.
            "system" — curated public catalogue.
            "user" — the account's own saved roles.
            "team" — roles shared with the account's team.
            "public" — public roles published by other users.
            "all" — everything accessible (default).

    Returns a dict with `roles`: list of {id, name, description_markdown,
    category, scope, status, ...}.
    """
    try:
        async with MyCouncilClient(api_key=_request_api_key()) as client:
            roles = await client.list_roles(
                scope=None if scope == "all" else scope
            )
        return {"roles": roles, "count": len(roles), "scope": scope}
    except Exception as exc:
        return _error_payload(exc)


@mcp.tool()
async def mycouncil_auto_config(
    content: str,
    file_names: list[str] | None = None,
) -> dict[str, Any]:
    """Generate a council session config from a user query.

    Useful when you want to inspect or tweak the config before starting a
    debate. The returned `config` can be passed straight to
    `mycouncil_debate` or `mycouncil_debate_start` — MCP will fill the
    underlying models locally based on `config["tier"]` before sending
    the request to the server.

    The returned `config` contains roles + temperatures + tier
    (`fast` / `balanced` / `deep`), but NOT concrete model IDs. The agent
    works with the role-and-tier abstraction; model selection is hidden.
    The agent MAY edit `config["tier"]` (e.g. escalate to `deep` for a
    genuinely high-stakes question) or modify roles before sending it
    back — but do not escalate to `deep` by default; balanced is the
    intended baseline.

    QUOTA: in `advanced` mode this call deducts 1 round per invocation
    (refunded only if the LLM provider itself fails). In `standard` mode
    it is free. Check the user's current mode with `mycouncil_balance`
    before calling this repeatedly.

    Args:
        content: The user query the council should debate.
        file_names: Optional list of filenames (just basenames) that will
            be attached to the debate later. Helps the planner LLM pick
            appropriate roles. Files are NOT uploaded by this call.
    """
    try:
        async with MyCouncilClient(api_key=_request_api_key()) as client:
            raw = await client.auto_config(content=content, file_names=file_names)
        # Hide concrete model IDs from the agent — replace the config payload
        # with a stripped version. Top-level fields like observation,
        # roles_summary, mode_used, rounds_charged, questions_remaining,
        # tier are kept as-is so the agent still sees the rationale and
        # cost accounting.
        if isinstance(raw, dict) and isinstance(raw.get("config"), dict):
            raw["config"] = _strip_models_from_config(raw["config"])
        return raw
    except Exception as exc:
        return _error_payload(exc)


@mcp.tool()
async def mycouncil_debate_start(
    content: str,
    config: dict[str, Any] | None = None,
    file_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Start a council debate and return a `job_id` for polling.

    Use this for the async flow when you don't want to block. Pair with
    `mycouncil_debate_status` to poll progress. If you just want the
    finished result, use `mycouncil_debate` instead.

    QUOTA: deducts 1 round on start for a three-stage council
    (`session_type=1`). Adversarial debates (`session_type=2`) pre-reserve
    up to `max_rounds` and refund unused rounds after completion.

    Args:
        content: The user query.
        config: Optional session config (e.g. from `mycouncil_auto_config`).
            If omitted, the server auto-configures inline using the
            account's current mode.
        file_paths: Optional list of local file paths (PDF, DOCX, TXT)
            to attach. Read from disk and uploaded as multipart.
    """
    try:
        async with MyCouncilClient(api_key=_request_api_key()) as client:
            prepared = await _prepare_config_for_debate(client, config)
            return await client.debate_start(
                content=content, config=prepared, file_paths=file_paths
            )
    except Exception as exc:
        return _error_payload(exc)


@mcp.tool()
async def mycouncil_debate_status(job_id: str) -> dict[str, Any]:
    """Get the current status of a debate job.

    Fields:
      - status: `ocr_in_progress` / `stage1_in_progress` /
        `stage2_step1_in_progress` / `stage2_step2_in_progress` /
        `aggregation_in_progress` / `stage3_in_progress` / `adf_*` /
        `complete` / `failed`
      - progress: 0-100
      - stage1, stage2, stage3, metadata: populated as stages complete
      - llm_cost: only when status=complete
      - error: only when status=failed
    """
    try:
        async with MyCouncilClient(api_key=_request_api_key()) as client:
            return await client.job(job_id)
    except Exception as exc:
        return _error_payload(exc)


@mcp.tool()
async def mycouncil_debate(
    content: str,
    return_as: Literal["pdf", "transcript", "link"] = "pdf",
    config: dict[str, Any] | None = None,
    file_paths: list[str] | None = None,
    save_path: str | None = None,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> dict[str, Any]:
    """Start a debate, poll it to completion, and return the result.

    Blocking: this tool polls the job every 6 seconds and only returns
    when the debate finishes, fails, or hits the timeout.

    `return_as` controls the result shape:
      - `pdf` (default): exports the finished transcript to a PDF file
        on disk and returns its path. The path is `save_path` if given,
        otherwise `./mycouncil-<short_id>-<timestamp>.pdf`.
      - `transcript`: returns the full JSON transcript (stage1, stage2,
        stage3, metadata, llm_cost).
      - `link`: enables a public share link for the conversation and
        returns the URL. WARNING: this makes the debate publicly viewable
        by anyone who has the URL.

    QUOTA: same as `mycouncil_debate_start` (1 round upfront for
    three-stage; refundable reserve for adversarial).

    Timeout: do not set `timeout_minutes` below 20 — a typical debate
    runs 5-15 minutes, OCR or large councils can push it longer. On
    timeout the tool returns `{status: "still_running", job_id, ...}`
    so you can keep polling with `mycouncil_debate_status`.

    Args:
        content: The user query.
        return_as: Result format. Default `pdf`.
        config: Optional pre-built session config (from
            `mycouncil_auto_config`).
        file_paths: Local file paths to attach.
        save_path: Where to save the PDF when `return_as=pdf`. Ignored
            otherwise.
        timeout_minutes: Max wait before returning a `still_running`
            result. Default 20.
    """
    try:
        async with MyCouncilClient(api_key=_request_api_key()) as client:
            prepared = await _prepare_config_for_debate(client, config)
            started = await client.debate_start(
                content=content, config=prepared, file_paths=file_paths
            )
            job_id = started["job_id"]
            conv_id = started.get("conversation_id", "")

            deadline = asyncio.get_event_loop().time() + timeout_minutes * 60
            final: dict[str, Any] | None = None
            while True:
                status = await client.job(job_id)
                if status.get("status") in TERMINAL_STATUSES:
                    final = status
                    break
                if asyncio.get_event_loop().time() >= deadline:
                    return {
                        "status": "still_running",
                        "job_id": job_id,
                        "conversation_id": conv_id,
                        "last_progress": status.get("progress"),
                        "last_status": status.get("status"),
                        "message": (
                            f"Debate did not finish within {timeout_minutes} "
                            "minutes. Poll `mycouncil_debate_status` with the "
                            "job_id to keep waiting."
                        ),
                    }
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

            assert final is not None
            if final.get("status") == "failed":
                return {
                    "status": "failed",
                    "job_id": job_id,
                    "conversation_id": conv_id,
                    "error": final.get("error", "unknown error"),
                }

            if return_as == "transcript":
                return {
                    "status": "complete",
                    "job_id": job_id,
                    "conversation_id": conv_id,
                    "stage1": final.get("stage1"),
                    "stage2": final.get("stage2"),
                    "stage3": final.get("stage3"),
                    "metadata": final.get("metadata"),
                    "llm_cost": final.get("llm_cost"),
                }

            if return_as == "link":
                share = await client.share_enable(conv_id)
                return {
                    "status": "complete",
                    "job_id": job_id,
                    "conversation_id": conv_id,
                    "share_url": share.get("share_url"),
                    "is_public": share.get("is_public", True),
                    "note": (
                        "Share link is public — anyone with the URL can "
                        "view this debate."
                    ),
                }

            # return_as == "pdf"
            destination = (
                Path(save_path).expanduser().resolve()
                if save_path
                else _default_pdf_path(conv_id)
            )
            saved = await client.export_pdf(conv_id, destination)
            return {
                "status": "complete",
                "job_id": job_id,
                "conversation_id": conv_id,
                "pdf_path": str(saved),
                "llm_cost": final.get("llm_cost"),
            }
    except Exception as exc:
        return _error_payload(exc)


@mcp.tool()
async def mycouncil_share(
    conversation_id: str,
    format: Literal["link", "pdf"] = "link",
    save_path: str | None = None,
) -> dict[str, Any]:
    """Share or export an existing conversation by id.

    Use this in the async flow after `mycouncil_debate_status` reports
    `complete`. Two formats:

      - `link`: enables a public share URL for the conversation and
        returns it. WARNING: the URL is publicly accessible to anyone
        who has it.
      - `pdf`: exports the transcript to a PDF file on disk. Path is
        `save_path` if given, otherwise
        `./mycouncil-<short_id>-<timestamp>.pdf`.

    Args:
        conversation_id: The id returned by `mycouncil_debate_start`.
        format: `link` or `pdf`. Default `link`.
        save_path: Where to save when `format=pdf`. Ignored otherwise.
    """
    try:
        async with MyCouncilClient(api_key=_request_api_key()) as client:
            if format == "link":
                share = await client.share_enable(conversation_id)
                return {
                    "share_url": share.get("share_url"),
                    "is_public": share.get("is_public", True),
                    "note": (
                        "Share link is public — anyone with the URL can "
                        "view this debate."
                    ),
                }
            destination = (
                Path(save_path).expanduser().resolve()
                if save_path
                else _default_pdf_path(conversation_id)
            )
            saved = await client.export_pdf(conversation_id, destination)
            return {"pdf_path": str(saved)}
    except Exception as exc:
        return _error_payload(exc)


# --- RAG prelude tool variants ---------------------------------------------
# Registered by main() in place of the plain debate tools when --rag-prelude
# is on. Same behaviour plus two optional per-call parameters: when a call
# carries both, the wrapper searches the stakeholder-call corpus once and
# prepends the excerpts to the debate content before it reaches the server.

_RAG_DOC_ADDENDUM = """

    RAG prelude (enabled on this server): optionally pass BOTH
    `rag_access_token` (short-lived bearer token, ~30 min) and
    `rag_base_url` (e.g. https://rag.example.com). The wrapper then runs
    one hybrid search over the stakeholder-call corpus and prepends the
    found excerpts to the debate content as:

        ### Question
        <content>

        ### Useful info
        <excerpts>

    Omit both to run a plain debate. The token is used only for this call
    and is never logged, stored, or echoed back; the result carries
    `rag_prelude: "applied" | "skipped (...)"` so integrators can verify
    the prelude ran.
    """


def _with_rag_status(
    result: dict[str, Any], rag_status: str | None
) -> dict[str, Any]:
    if rag_status and isinstance(result, dict) and "error" not in result:
        result["rag_prelude"] = rag_status
    return result


async def _mycouncil_debate_rag(
    content: str,
    return_as: Literal["pdf", "transcript", "link"] = "pdf",
    config: dict[str, Any] | None = None,
    file_paths: list[str] | None = None,
    save_path: str | None = None,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
    rag_access_token: str | None = None,
    rag_base_url: str | None = None,
) -> dict[str, Any]:
    content, rag_status = await _maybe_rag_prelude(
        content, rag_access_token, rag_base_url
    )
    result = await mycouncil_debate(
        content, return_as, config, file_paths, save_path, timeout_minutes
    )
    return _with_rag_status(result, rag_status)


async def _mycouncil_debate_start_rag(
    content: str,
    config: dict[str, Any] | None = None,
    file_paths: list[str] | None = None,
    rag_access_token: str | None = None,
    rag_base_url: str | None = None,
) -> dict[str, Any]:
    content, rag_status = await _maybe_rag_prelude(
        content, rag_access_token, rag_base_url
    )
    result = await mycouncil_debate_start(content, config, file_paths)
    return _with_rag_status(result, rag_status)


_mycouncil_debate_rag.__doc__ = (mycouncil_debate.__doc__ or "") + _RAG_DOC_ADDENDUM
_mycouncil_debate_start_rag.__doc__ = (
    mycouncil_debate_start.__doc__ or ""
) + _RAG_DOC_ADDENDUM


def _enable_rag_prelude() -> None:
    """Swap the debate tools for the variants exposing rag_* parameters."""
    for name, fn in (
        ("mycouncil_debate", _mycouncil_debate_rag),
        ("mycouncil_debate_start", _mycouncil_debate_start_rag),
    ):
        mcp.remove_tool(name)
        mcp.add_tool(fn, name=name)


def main() -> None:
    """Entry point. Serves stdio by default; --transport streamable-http runs
    the server as a long-lived HTTP service.

    Auth: by default every call uses the single MYCOUNCIL_API_KEY from the
    environment, regardless of transport. `--auth per-request`
    (streamable-http only) flips that: each HTTP call must carry its own
    myCouncil key in the X-MyCouncil-Key or Authorization: Bearer header —
    one hosted wrapper serving many myCouncil accounts.
    """
    parser = argparse.ArgumentParser(
        prog="mycouncil",
        description="myCouncil MCP server — stdio (default) or streamable-http.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("MYCOUNCIL_TRANSPORT", "stdio"),
        help="Transport to serve on. Default: stdio (env: MYCOUNCIL_TRANSPORT).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MYCOUNCIL_HTTP_HOST", "127.0.0.1"),
        help="streamable-http bind host. Default: 127.0.0.1 "
        "(env: MYCOUNCIL_HTTP_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MYCOUNCIL_HTTP_PORT", "8000")),
        help="streamable-http bind port. Default: 8000 (env: MYCOUNCIL_HTTP_PORT).",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("MYCOUNCIL_HTTP_PATH", "/mcp"),
        help="streamable-http endpoint path. Default: /mcp "
        "(env: MYCOUNCIL_HTTP_PATH).",
    )
    parser.add_argument(
        "--auth",
        choices=["shared", "per-request"],
        default=os.environ.get("MYCOUNCIL_HTTP_AUTH", "shared"),
        help="streamable-http auth mode: 'shared' uses MYCOUNCIL_API_KEY from "
        "the environment for every call; 'per-request' requires each HTTP "
        "call to carry its own key in the X-MyCouncil-Key or "
        "Authorization: Bearer header. Default: shared "
        "(env: MYCOUNCIL_HTTP_AUTH).",
    )
    parser.add_argument(
        "--rag-prelude",
        action="store_true",
        default=os.environ.get("MYCOUNCIL_RAG_PRELUDE", "").strip().lower()
        in ("1", "true", "yes", "on"),
        help="Expose optional rag_access_token / rag_base_url parameters on "
        "the debate tools; when a call carries both, the wrapper searches "
        "the stakeholder-call RAG once and prepends the excerpts to the "
        "debate content. Default: off (env: MYCOUNCIL_RAG_PRELUDE).",
    )
    args = parser.parse_args()

    # argparse validates `choices` only for values given on the command line;
    # env-sourced defaults bypass it. A typo'd auth mode must fail loudly, not
    # silently run as shared.
    if args.auth not in ("shared", "per-request"):
        parser.error(
            f"invalid auth mode {args.auth!r} (check MYCOUNCIL_HTTP_AUTH): "
            "expected 'shared' or 'per-request'"
        )
    if args.auth == "per-request" and args.transport == "stdio":
        parser.error("--auth per-request requires --transport streamable-http")

    global _AUTH_MODE
    _AUTH_MODE = args.auth

    if args.rag_prelude:
        _enable_rag_prelude()

    if args.transport == "stdio":
        mcp.run("stdio")
        return

    # streamable-http: apply bind settings before run() — streamable_http_app()
    # reads them lazily when the server starts.
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.streamable_http_path = args.path
    # FastMCP auto-enables a localhost-only DNS-rebinding guard. Binding to a
    # non-loopback interface (e.g. 0.0.0.0 behind a reverse proxy) would then
    # reject forwarded Host/Origin headers, so relax it for non-loopback binds.
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        mcp.settings.transport_security = None
    mcp.run("streamable-http")


if __name__ == "__main__":
    main()
