"""myCouncil MCP server — exposes the public API as MCP tools."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .client import MyCouncilClient, MyCouncilError
from .tier import fill_models_by_tier

POLL_INTERVAL_SECONDS = 6
DEFAULT_TIMEOUT_MINUTES = 20
TERMINAL_STATUSES = {"complete", "failed"}

mcp = FastMCP("mycouncil")


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


@mcp.tool()
async def mycouncil_balance() -> dict[str, Any]:
    """Return remaining rounds (quota) and the account's current
    auto-config mode (`standard` is free, `advanced` costs 1 round per
    auto-config call). Source of truth for what is left on the account.
    """
    try:
        async with MyCouncilClient() as client:
            return await client.balance()
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
        async with MyCouncilClient() as client:
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
        async with MyCouncilClient() as client:
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
        async with MyCouncilClient() as client:
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
        async with MyCouncilClient() as client:
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
        async with MyCouncilClient() as client:
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
