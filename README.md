# mcp-write-approval

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that intercepts MCP tool calls and asks for user approval before any write or destructive tool fires.

Hermes' built-in approval flow gates dangerous *shell* commands. MCP tool calls auto-execute by default. This plugin extends the same UX to MCP tools — read calls still pass through silently, but tools like `cloudflare_delete_dns_record`, `voluum_archive_campaign`, or `dropbox_delete_file` block until the user says yes.

## What it does

When a write or destructive MCP tool is about to run, the agent thread is parked and the user sees:

```
✋ This MCP call needs approval.
Tool: cloudflare_delete_dns_record (destructive)
Args: {"zone":"mediadevoted.com","name":"staging","type":"A"}
Reply /allow to run once, /allow-session to allow this tool for the rest
of the session, /allow-always to remember it, or /deny-mcp to cancel.
```

| Slash command | Effect |
| --- | --- |
| `/allow` | Run this one call. The next call to the same tool prompts again. |
| `/allow-session` | Run this call, plus any future call to the same tool *in this session*. |
| `/allow-always` | Run this call, plus any future call to this tool, in *any* session. Persisted to `~/.hermes/state/mcp_approvals.json`. |
| `/deny-mcp` | Block this call. The agent gets back a `BLOCKED: user denied ...` error. |
| `/mcp-approvals` | Show pending + granted approvals for this session. |

The CLI surface only registers `/allow*` / `/deny-mcp` (Hermes' built-in `/approve` and `/deny` belong to the shell-command flow). Reads always pass through.

## Key features

- **Zero core changes** — plugs into Hermes' public `pre_tool_call` hook and slash-command interfaces only.
- **Gateway-aware** — reuses Hermes' existing notify callback so approval prompts are delivered via whichever gateway is active (Slack, Telegram, Discord, dashboard chat, or stdout for CLI).
- **Three approval tiers** — once, session-scoped, or persistent (written atomically to disk via tmp-file rename so a crash can't corrupt state).
- **Catalog-driven or heuristic** — modes come from each MCP's `/tools-manifest` endpoint when configured; falls back to a name-suffix heuristic out of the box.
- **FIFO queue** — multiple blocked calls are resolved in order; each `/allow` advances the queue by one.
- **Heartbeat-safe blocking** — the wait loop polls in 1-second slices so the gateway inactivity watchdog keeps ticking.
- **~360 lines, single file** — no dependencies beyond the Python standard library and Hermes itself.

## Architecture

```
Hermes agent loop
  └─ invoke_hook("pre_tool_call")
       └─ _on_pre_tool_call()          ← this plugin
            ├─ _requires_approval()    classify tool (catalog or heuristic)
            ├─ _is_approved()          check session / persistent allowlist
            └─ _queue_and_wait()       park call, notify user, block
                  ├─ hooks into tools.approval._gateway_queues  (shared path)
                  └─ falls back to plugin-local _pending dict   (CLI-only)

User types /allow (or /allow-session, /allow-always, /deny-mcp)
  └─ slash handler resolves oldest pending entry
       └─ entry.event.set() → hook returns None (allow) or block dict (deny)
```

Persistent state lives at `~/.hermes/state/mcp_approvals.json`:

```json
{
  "always_allow": ["cloudflare_purge_cache", "voluum_archive_campaign"]
}
```

## Install

```bash
mkdir -p ~/.hermes/plugins
git clone https://github.com/MediaDevoted/hermes-mcp-write-approval \
  ~/.hermes/plugins/mcp-write-approval
hermes plugins enable mcp-write-approval
```

Then restart Hermes (or fire `/reload-plugins` if your build supports hot reload).

## Configure

The plugin discovers which tools need approval in one of three ways, in order of precision:

### 1. Per-server manifests (recommended)

Set `HERMES_MCP_APPROVAL_MANIFESTS` to a comma-separated list of MCP `/tools-manifest` URLs:

```bash
export HERMES_MCP_APPROVAL_MANIFESTS="\
https://cloudflare-mcp-prod.mediadevoted.com/tools-manifest,\
https://voluum-mcp-prod.mediadevoted.com/tools-manifest,\
https://domain-bank-mcp-prod.mediadevoted.com/tools-manifest"
```

At startup the plugin fetches each URL and reads `tools[].mode` (`read` / `write` / `destructive`). Anything `write` or `destructive` triggers the approval prompt.

### 2. Flat catalog

Set `HERMES_MCP_APPROVAL_CATALOG` to one URL that returns a flat list:

```json
[{"name":"cloudflare_delete_dns_record","mode":"destructive"},
 {"name":"voluum_create_campaign","mode":"write"}]
```

### 3. Name-suffix heuristic (default fallback)

Set neither env var, and the plugin falls back to checking tool name suffixes. Any tool whose name ends in `_write`, `_create`, `_update`, `_delete`, `_destroy`, `_remove`, `_archive`, `_pause`, `_purge`, `_stop`, `_restart`, `_enable`, `_disable`, `_rename`, `_reset`, `_upsert`, or `_set` triggers the prompt. Coarser than option 1 (flags everything as `write`, never `destructive`), but works out of the box with no configuration.

### Additional env vars

| Variable | Default | Description |
| --- | --- | --- |
| `HERMES_MCP_APPROVAL_MANIFESTS` | — | Comma-separated list of `/tools-manifest` URLs |
| `HERMES_MCP_APPROVAL_CATALOG` | — | Single URL returning a flat tool list |
| `HERMES_MCP_APPROVAL_AUTH` | — | Bearer token for authenticated manifest endpoints |
| `HERMES_MCP_APPROVAL_TIMEOUT` | `300` | Seconds before an unanswered prompt is auto-denied |

See `.env.template` for a copy-paste starting point.

## How it works

The plugin registers a `pre_tool_call` hook. Hermes calls this for every tool the model invokes. When a write/destructive tool fires and is not yet approved:

1. A `_PendingEntry` (containing a `threading.Event`) is pushed onto Hermes' shared `_gateway_queues[session_key]` so that the gateway's `/approve` resolver can reach it. A plugin-local fallback queue is used in CLI-only mode where `tools.approval` isn't importable.
2. The plugin calls Hermes' existing gateway notify callback (the same one the built-in dangerous-command approval uses) so the prompt is delivered via whichever gateway adapter is active. If no gateway is registered, the prompt prints to stdout.
3. The hook's worker thread blocks on `entry.event.wait(timeout)` with a 1-second poll slice so Hermes' activity heartbeat keeps ticking and the gateway inactivity watchdog doesn't kill the agent mid-prompt.
4. When the user types `/allow` / `/allow-session` / `/allow-always` / `/deny-mcp`, the matching handler pops the oldest entry off the queue, sets `entry.result`, and fires the event. The hook returns `None` (allow) or `{"action": "block", "message": ...}` (deny / timeout).
5. `/allow-session` adds the tool name to `_session_approvals[session_id]`. `/allow-always` adds it to `_persistent_approvals` and writes to `~/.hermes/state/mcp_approvals.json` via a tmp-file rename so a crash mid-write can't corrupt it.

**Session key vs. session ID:** Hermes uses two distinct identifiers. The gateway registers its notify callback under a stable per-channel `session_key` (e.g. `agent:main:telegram:dm:6157749755`), while the hook receives a per-conversation `session_id`. The plugin bridges these via `tools.approval.get_current_session_key` — without this, gateway prompts would never arrive and calls would silently time out.

## Build / run

This is a Hermes plugin (no standalone build step). A virtual environment with Hermes installed is required for the tests.

```bash
# Create and activate a venv with Hermes
python -m venv .venv
source .venv/bin/activate
pip install hermes-agent   # or however your org distributes Hermes

# Run tests
python -m pytest tests/test_plugin.py -v
```

## Tests

19 tests covering:

- Mode classification (catalog override, suffix heuristic, built-in tool pass-through)
- Approval state isolation (session vs. persistent)
- Persistence round-trip and atomic write
- `pre_tool_call` block/unblock for all four resolutions (`once`, `session`, `always`, `deny`)
- FIFO ordering for queued calls
- End-to-end path through Hermes' real `invoke_hook` + slash handler dispatch (no mocking)

## Deployment

Drop the plugin directory into `~/.hermes/plugins/` on any machine running Hermes. No containers or daemons required — the plugin runs in-process with the Hermes agent.

For a fleet deployment, distribute the plugin via your internal package registry or a shared config layer, and set `HERMES_MCP_APPROVAL_MANIFESTS` in the agent's environment so every instance uses catalog-driven mode classification.

## License

MIT.
