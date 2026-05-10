# mcp-write-approval

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that asks for user approval before any MCP write or destructive tool fires.

Hermes' built-in approval flow gates dangerous *shell* commands. MCP tool calls auto-execute. This plugin extends the same UX to MCP tools — read calls still pass through silently, but `cloudflare_delete_dns_record`, `voluum_archive_campaign`, `dropbox_delete_file`, etc. block until the user says yes.

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

## Install

```bash
mkdir -p ~/.hermes/plugins
git clone https://github.com/MediaDevoted/hermes-mcp-write-approval \
  ~/.hermes/plugins/mcp-write-approval
hermes plugins enable mcp-write-approval
```

Then restart Hermes (or fire `/reload-mcp` if you only changed config).

## Configure

The plugin discovers which tools need approval in one of three ways, in order of precision:

1. **Set `HERMES_MCP_APPROVAL_MANIFESTS`** to a comma-separated list of MCP `/tools-manifest` URLs:

   ```bash
   export HERMES_MCP_APPROVAL_MANIFESTS="\
   https://cloudflare-mcp-prod.mediadevoted.com/tools-manifest,\
   https://voluum-mcp-prod.mediadevoted.com/tools-manifest,\
   https://domain-bank-mcp-prod.mediadevoted.com/tools-manifest"
   ```

   At startup the plugin fetches each one and reads `tools[].mode` (`read` / `write` / `destructive`). Anything `write` or `destructive` triggers the prompt.

2. **Set `HERMES_MCP_APPROVAL_CATALOG`** to one URL that returns a flat list:

   ```json
   [{"name":"cloudflare_delete_dns_record","mode":"destructive"},
    {"name":"voluum_create_campaign","mode":"write"}]
   ```

3. **Set neither**, and the plugin falls back to a name-suffix heuristic: any tool whose name ends in `_write`, `_create`, `_update`, `_delete`, `_destroy`, `_remove`, `_archive`, `_pause`, `_purge`, `_stop`, `_restart`, `_enable`, `_disable`, `_rename`, `_reset`, `_upsert`, `_set` triggers the prompt. Coarser than option 1 (won't distinguish `write` from `destructive`), but works out of the box.

Optional auth: set `HERMES_MCP_APPROVAL_AUTH` to a bearer token if your manifests live behind authentication.

Optional timeout: `HERMES_MCP_APPROVAL_TIMEOUT` (seconds, default 300). After this, the call is denied with a `BLOCKED: approval ... timed out` message.

## How it works

The plugin registers a `pre_tool_call` hook. Hermes calls this for every tool the model invokes. When a write/destructive tool fires and is not yet approved:

1. A `_PendingEntry` (containing a `threading.Event`) is pushed to the per-session queue.
2. The plugin calls Hermes' existing gateway notify callback (the same one the built-in dangerous-command approval uses) so the prompt is delivered via whichever gateway adapter is active — Slack, Telegram, Discord, dashboard chat, etc. If no gateway is registered (pure CLI), the prompt prints to stdout.
3. The hook's worker thread blocks on `entry.event.wait(timeout)` with a 1-second poll slice so Hermes' activity heartbeat keeps ticking and the gateway inactivity watchdog doesn't kill the agent mid-prompt.
4. When the user types `/allow` / `/allow-session` / `/allow-always` / `/deny-mcp`, the matching handler pops the oldest entry off the queue, sets `entry.result`, and fires the event. The hook returns `None` (allow) or `{"action": "block", "message": ...}` (deny / timeout) — exactly what `get_pre_tool_call_block_message()` is shaped to consume.
5. `/allow-session` adds the tool name to `_session_approvals[session_id]`. `/allow-always` adds it to `_persistent_approvals` and writes the set to `~/.hermes/state/mcp_approvals.json` via a tmp-file rename so a crash mid-write can't corrupt it.

The plugin never reaches into MCP-side state, never modifies agent-platform, never extends Hermes core. It plugs into the public hook + slash-command interfaces. Roughly 360 lines, single file.

## Tests

```bash
python -m pytest tests/test_plugin.py -v
```

19 tests covering mode classification, approval state, persistence (round-trip + atomic write), pre_tool_call block / unblock for all four resolutions, FIFO ordering for queued calls, and the end-to-end path through Hermes' real `invoke_hook` + slash handler dispatch (no mocking).

## License

MIT.
