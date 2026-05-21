# mycouncil — MCP server

Run multi-LLM **myCouncil** debates from Claude Code, Claude Desktop, Cursor, and
any other [MCP](https://modelcontextprotocol.io)-aware client.

Thin wrapper over the public [myCouncil API](https://app.mycouncil.xyz/api/v1/docs).
Rounds, billing, and auto-config mode live on your account — the MCP server
just relays calls.

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

## Tools

| Tool | What it does |
|---|---|
| `mycouncil_balance` | Remaining rounds + current auto-config mode. |
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
