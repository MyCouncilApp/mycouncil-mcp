# mycouncil — MCP server

Run multi-LLM **myCouncil** debates from Claude Code, Claude Desktop, Cursor, and
any other [MCP](https://modelcontextprotocol.io)-aware client.

Thin wrapper over the public [myCouncil API](https://app.mycouncil.xyz/api/v1/docs).
Rounds, billing, and auto-config mode live on your account — the MCP server
just relays calls.

## What's new

**0.3.0** — optional **streamable-http** transport
(`--transport streamable-http`). Run the server as one long-lived HTTP
service instead of a per-client stdio process — the async server handles
concurrent debates natively (no stdio→HTTP bridge in front). Identity is
unchanged: a single `MYCOUNCIL_API_KEY` from the environment. stdio stays
the default. See [Running over streamable HTTP](#running-over-streamable-http).

**0.2.0** — added `mycouncil_info` (orientation guide agents can call once
per session) and `mycouncil_list_roles` (browse the curated expert-role
catalogue when composing a custom council). 8 tools total now.

**0.1.0** — initial release: 6 tools, tier abstraction (`fast` / `balanced`
/ `deep`), model IDs hidden from the agent.

## Setup

1. Sign up at <https://app.mycouncil.xyz> (10 free rounds).
2. Pick your auto-config mode under **Account → Auto-config Settings**
   (`standard` is free; `advanced` costs 1 round per call).
3. Create an API key under **Account → API**. The key is shown once — save it.
4. Register the MCP server in your client (see below).

Nothing to install — `uvx` fetches the package on first use. Requires
[`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

### Claude Code

```bash
claude mcp add mycouncil \
  --env MYCOUNCIL_API_KEY=mc_your_key_here \
  -- uvx mycouncil
```

### Claude Desktop / Cursor

In `claude_desktop_config.json` or `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "mycouncil": {
      "command": "uvx",
      "args": ["mycouncil"],
      "env": { "MYCOUNCIL_API_KEY": "mc_your_key_here" }
    }
  }
}
```

## Running over streamable HTTP

By default the server runs over **stdio** — one process per client, spawned
by the MCP client. You can instead run it as a single long-lived
**streamable-http** service:

```bash
MYCOUNCIL_API_KEY=mc_your_key_here \
  uvx mycouncil --transport streamable-http --host 127.0.0.1 --port 8000
```

The endpoint is then `http://<host>:<port>/mcp`. Point any streamable-http
MCP client at it:

```json
{
  "mcpServers": {
    "mycouncil": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Or, with the Claude Code CLI (no env var on the client — the key lives with
the running service):

```bash
claude mcp add --transport http mycouncil http://127.0.0.1:8000/mcp
```

This is **single-identity**: every request uses the one `MYCOUNCIL_API_KEY`
the process was started with — all callers share that account's rounds and
balance. It is not multi-tenant; it just lets one process speak HTTP
natively so concurrent debates run on the async event loop without a
stdio→HTTP bridge funnelling them through a single pipe. The transport runs
in stateless mode (a fresh transport per request), so there is no session
affinity to manage.

Notes:

- **Binding beyond localhost.** The default bind is `127.0.0.1`. If you set
  `--host 0.0.0.0` (e.g. behind a reverse proxy), the localhost-only
  DNS-rebinding guard is relaxed automatically — put the service behind your
  own proxy / network controls, since anyone who can reach the port spends
  the configured key's quota.
- **Long-running debates.** Blocking `mycouncil_debate` holds the HTTP
  response open while it polls (up to `timeout_minutes`, default 20) with no
  bytes flowing. Raise idle timeouts on any intermediary proxy, or prefer
  the async pair `mycouncil_debate_start` + `mycouncil_debate_status` over
  HTTP.

All flags have environment-variable equivalents (`MYCOUNCIL_TRANSPORT`,
`MYCOUNCIL_HTTP_HOST`, `MYCOUNCIL_HTTP_PORT`, `MYCOUNCIL_HTTP_PATH`) — see
[Environment variables](#environment-variables).

## Tools

| Tool | What it does |
|---|---|
| `mycouncil_info` | One-call agent orientation: flows, tier semantics, quota notes. Call once at the start of a session. |
| `mycouncil_balance` | Remaining rounds + current auto-config mode. |
| `mycouncil_list_roles` | List curated system + own + team expert roles. Use their `id` as `role_preset` when composing a custom council. |
| `mycouncil_auto_config` | Generate a session config from a query. Returns roles + temperatures + `tier`. **Concrete model IDs are not exposed** — the planner and the server pick them based on the tier. |
| `mycouncil_debate_start` | Start a debate, return `job_id`. Accepts a config returned by `mycouncil_auto_config` (with or without edits). |
| `mycouncil_debate_status` | Poll a debate by `job_id`. |
| `mycouncil_debate` | Blocking: start, poll, return the finished result. `return_as`: `pdf` (default) / `transcript` / `link`. |
| `mycouncil_share` | Share or export an existing conversation: `format=link` (public URL) or `format=pdf` (file on disk). |

## How tiers work

The planner LLM picks one of three operating tiers based on the question:

- **fast** — quick + cheap models, for simple / casual questions.
- **balanced** — sweet spot. Default for typical analytical questions.
- **deep** — slow + reasoning-heavy models, for high-stakes / complex /
  irreversible decisions. Only available in **advanced** auto-config mode.

Tiers are an operating mode, not a quality rank — `deep` does not always
beat `balanced` in absolute terms, it just takes more time and money. The
planner is instructed to lean toward `balanced` and not escalate by default.

After `mycouncil_auto_config`, you may edit `config["tier"]` and roles
before passing it to `mycouncil_debate(_start)`. MCP fills concrete models
from the tier locally; the agent never sees specific provider names.

## Quotas

- Three-stage council: **1 round** per debate, deducted at start.
- `mycouncil_auto_config` is **free** in `standard` mode, **1 round** in
  `advanced` mode (refunded if the planner LLM fails).
- `mycouncil_debate` auto-configures internally if you don't pass a config —
  calling `mycouncil_auto_config` first only makes sense to preview / tweak
  the config. In `advanced` mode that bills you twice.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MYCOUNCIL_API_KEY` | yes | — | Your `mc_*` key from Account → API. |
| `MYCOUNCIL_BASE_URL` | no | `https://app.mycouncil.xyz` | Override for staging / self-hosted. |
| `MYCOUNCIL_TRANSPORT` | no | `stdio` | `stdio` or `streamable-http`. Overridden by `--transport`. |
| `MYCOUNCIL_HTTP_HOST` | no | `127.0.0.1` | Bind host for streamable-http. Overridden by `--host`. |
| `MYCOUNCIL_HTTP_PORT` | no | `8000` | Bind port for streamable-http. Overridden by `--port`. |
| `MYCOUNCIL_HTTP_PATH` | no | `/mcp` | Endpoint path for streamable-http. Overridden by `--path`. |

## Examples

Simplest path — let myCouncil pick everything:

> Run `mycouncil_debate` on "Should we migrate our backend from FastAPI to Go?"
> and return the result as a PDF in `./review.pdf`.

Preview and escalate the tier before running (advanced mode):

> Use `mycouncil_auto_config` for "Replace our entire ML infra with a custom RAG
> system, $3M budget". If the planner returns `tier: balanced`, change it to
> `deep` and run `mycouncil_debate` with the edited config.

Async polling:

> Start the debate with `mycouncil_debate_start`, then poll
> `mycouncil_debate_status` every minute until status is `complete` or `failed`.

## License

[Apache 2.0](./LICENSE).
