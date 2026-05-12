"""mcp-write-approval — per-call approval for MCP write/destructive tools.

The plugin sits in front of the agent's tool loop via `pre_tool_call`. When
a tool whose mode is "write" or "destructive" is about to fire, the call is
parked on a per-session queue and the user gets a prompt asking them to
`/allow`, `/allow-session`, `/allow-always`, or `/deny-mcp`. The agent
thread blocks (mirroring Hermes' own dangerous-command flow) until the
user responds or the timeout fires.

Modes come from each MCP's `/tools-manifest` endpoint. Discovery runs once
at plugin load and is configured by either:

  • $HERMES_MCP_APPROVAL_MANIFESTS — comma-separated list of manifest URLs
    (e.g. "https://cloudflare-mcp-prod.mediadevoted.com/tools-manifest,
           https://voluum-mcp-prod.mediadevoted.com/tools-manifest")

  • or $HERMES_MCP_APPROVAL_CATALOG — single URL returning a flat JSON
    list `[{"name": "...", "mode": "write|destructive|read"}, ...]`

If neither is set or fetch fails, the plugin falls back to a name-suffix
heuristic so it still does something useful out of the box.

"Always" grants persist to ~/.hermes/state/mcp_approvals.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("hermes_plugins.mcp_write_approval")

# ---------------------------------------------------------------------------
# State (module-level — one instance per Hermes process)
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# tool_name -> "read"|"write"|"destructive"|"unknown"
_tool_modes: Dict[str, str] = {}

# session_id -> {tool_name, ...}
_session_approvals: Dict[str, Set[str]] = {}

# tool_name set — survives restart via mcp_approvals.json
_persistent_approvals: Set[str] = set()


class _PendingEntry:
    __slots__ = ("event", "tool_name", "args", "result")

    def __init__(self, tool_name: str, args: Dict[str, Any]):
        self.event = threading.Event()
        self.tool_name = tool_name
        self.args = args
        # result: "once" | "session" | "always" | "deny" | None (timeout)
        self.result: Optional[str] = None


# session_id -> FIFO queue of pending entries
_pending: Dict[str, List[_PendingEntry]] = {}

# Bridge to gateway — populated by gateway_runner_for(session_id) below.
# We store the registered notify_cb from tools.approval directly so we can
# reuse Hermes' existing Slack/Telegram approval-message path.
_DEFAULT_TIMEOUT_SECS = 300

# Fallback heuristic when no manifest catalog is available.
_WRITE_SUFFIX_RE = re.compile(
    r"_(?:write|create|update|delete|destroy|remove|archive|pause|purge|"
    r"stop|restart|enable|disable|rename|reset|upsert|set)(?:[_$]|$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    base = Path(os.path.expanduser("~/.hermes/state"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "mcp_approvals.json"


def _load_persistent() -> None:
    p = _state_path()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        tools = data.get("always_allow") if isinstance(data, dict) else None
        if isinstance(tools, list):
            with _lock:
                _persistent_approvals.update(t for t in tools if isinstance(t, str))
            logger.info(
                "Loaded %d 'always' approvals from %s",
                len(_persistent_approvals), p,
            )
    except Exception as exc:
        logger.warning("Could not read %s: %s", p, exc)


def _save_persistent() -> None:
    p = _state_path()
    try:
        with _lock:
            data = {"always_allow": sorted(_persistent_approvals)}
        # Write+rename for atomicity so a crash mid-write can't leave a
        # truncated file that fails to parse next time.
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:
        logger.warning("Could not write %s: %s", p, exc)


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def _fetch_json(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "hermes-mcp-approval/0.1"}
    )
    token = os.environ.get("HERMES_MCP_APPROVAL_AUTH")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ingest_manifest(payload: Any) -> int:
    """Pull tool→mode pairs out of one manifest blob, return count added."""
    tools_list: Optional[List[Dict[str, Any]]] = None
    if isinstance(payload, dict) and isinstance(payload.get("tools"), list):
        tools_list = payload["tools"]
    elif isinstance(payload, list):
        tools_list = payload
    if not tools_list:
        return 0
    added = 0
    with _lock:
        for t in tools_list:
            if not isinstance(t, dict):
                continue
            name = t.get("name")
            mode = (t.get("mode") or "").lower()
            if isinstance(name, str) and mode in ("read", "write", "destructive"):
                _tool_modes[name] = mode
                added += 1
    return added


def _load_catalog() -> None:
    manifests_env = os.environ.get("HERMES_MCP_APPROVAL_MANIFESTS", "").strip()
    catalog_env = os.environ.get("HERMES_MCP_APPROVAL_CATALOG", "").strip()

    if manifests_env:
        urls = [u.strip() for u in manifests_env.split(",") if u.strip()]
    elif catalog_env:
        urls = [catalog_env]
    else:
        logger.info(
            "No MCP catalog URL configured. Falling back to name-suffix "
            "heuristic. Set HERMES_MCP_APPROVAL_MANIFESTS to a comma-"
            "separated list of /tools-manifest URLs for precise gating."
        )
        return

    for url in urls:
        try:
            payload = _fetch_json(url)
            n = _ingest_manifest(payload)
            logger.info("Loaded %d tool entries from %s", n, url)
        except Exception as exc:
            logger.warning("Failed to load %s: %s", url, exc)


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

def _mode_for(tool_name: str) -> str:
    """Return 'read'|'write'|'destructive'|'unknown'."""
    mode = _tool_modes.get(tool_name)
    if mode:
        return mode
    # Built-in Hermes tools (terminal, web_search, etc.) — not our concern.
    # Heuristic targets the connector-prefixed MCP names like
    # cloudflare_*, voluum_*, dropbox_*.  Anything without an underscore
    # prefix is left alone.
    if "_" not in tool_name:
        return "unknown"
    if _WRITE_SUFFIX_RE.search(tool_name):
        # Conservative: heuristic only flags as "write", never destructive,
        # because we can't tell archive vs create from the name alone.
        return "write"
    return "unknown"


def _requires_approval(tool_name: str) -> bool:
    return _mode_for(tool_name) in ("write", "destructive")


def _is_approved(session_id: str, tool_name: str) -> bool:
    with _lock:
        if tool_name in _persistent_approvals:
            return True
        return tool_name in _session_approvals.get(session_id, set())


# ---------------------------------------------------------------------------
# Approval queue
# ---------------------------------------------------------------------------

def _format_args(args: Dict[str, Any], limit: int = 320) -> str:
    """Compact one-line summary of args for the approval message."""
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        s = repr(args)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _send_prompt_to_user(session_id: str, tool_name: str, args: Dict[str, Any]) -> bool:
    """Try to push an approval prompt to whichever surface the user is on.

    Returns True if a prompt was sent (or printed for CLI), False if there
    is no way to reach the user — in that case the call is denied.
    """
    mode = _mode_for(tool_name)
    msg = (
        f"✋ This MCP call needs approval.\n"
        f"Tool: `{tool_name}` ({mode})\n"
        f"Args: `{_format_args(args)}`\n"
        f"Reply `/allow` to run once, `/allow-session` to allow this tool "
        f"for the rest of the session, `/allow-always` to remember it, "
        f"or `/deny-mcp` to cancel."
    )

    # Gateway path: piggy-back on Hermes' approval notify callback, which
    # the gateway adapter (Slack/Telegram/Discord) registers per session.
    #
    # IMPORTANT: the gateway registers under its `session_key` (a stable
    # per-channel identity like `agent:main:telegram:dm:6157749755`), NOT
    # the per-conversation `session_id` (like `20260512_212803_b4e0bbfe`)
    # that this hook receives as a kwarg. They are different identifiers.
    # The contextvar `_approval_session_key` in tools.approval is set by
    # the gateway runner around `agent.run_conversation(...)` to bridge
    # the two — we read it via `get_current_session_key`. Without this,
    # the lookup miss falls through to the CLI `print(msg)` path which is
    # invisible on Telegram/Slack and the call times out at 300s with the
    # user never seeing a prompt.
    try:
        from tools.approval import _gateway_notify_cbs, _lock as _approval_lock  # type: ignore
        try:
            from tools.approval import get_current_session_key  # type: ignore
            session_key = get_current_session_key(default=session_id)
        except Exception:
            session_key = session_id
    except Exception:
        _gateway_notify_cbs = {}
        _approval_lock = None
        session_key = session_id

    notify_cb = None
    if _approval_lock is not None:
        with _approval_lock:
            notify_cb = _gateway_notify_cbs.get(session_key)
            # Fallback for older Hermes builds that registered under the
            # task_id (= session_id). Try that next if session_key missed.
            if notify_cb is None and session_key != session_id:
                notify_cb = _gateway_notify_cbs.get(session_id)

    if notify_cb is not None:
        try:
            notify_cb({
                "command": f"mcp:{tool_name}",
                "description": msg,
                "pattern_key": f"mcp:{tool_name}",
                "pattern_keys": [f"mcp:{tool_name}"],
            })
            return True
        except Exception as exc:
            logger.warning("Gateway notify failed for %s: %s", tool_name, exc)

    # CLI fallback: print directly.  The agent thread blocks below; the
    # user sees this in their terminal and replies in the next turn.
    print(msg)
    return True


def _queue_and_wait(session_id: str, tool_name: str, args: Dict[str, Any]) -> str:
    """Park the call on this session's queue, prompt the user, block.

    Returns one of: "once", "session", "always", "deny", "timeout".
    """
    entry = _PendingEntry(tool_name, args)
    with _lock:
        _pending.setdefault(session_id, []).append(entry)

    if not _send_prompt_to_user(session_id, tool_name, args):
        with _lock:
            queue = _pending.get(session_id, [])
            if entry in queue:
                queue.remove(entry)
            if not queue:
                _pending.pop(session_id, None)
        return "deny"

    timeout_s = _DEFAULT_TIMEOUT_SECS
    try:
        timeout_s = int(os.environ.get("HERMES_MCP_APPROVAL_TIMEOUT", _DEFAULT_TIMEOUT_SECS))
    except (TypeError, ValueError):
        pass

    deadline = time.monotonic() + max(timeout_s, 1)
    resolved = False
    # Poll in 1s slices so the agent's inactivity heartbeat can still tick.
    try:
        from tools.environments.base import touch_activity_if_due  # type: ignore
    except Exception:  # pragma: no cover
        touch_activity_if_due = None  # type: ignore
    _activity_state = {"last_touch": time.monotonic(), "start": time.monotonic()}

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, remaining)):
            resolved = True
            break
        if touch_activity_if_due is not None:
            try:
                touch_activity_if_due(_activity_state, "waiting for MCP approval")
            except Exception:
                pass

    with _lock:
        queue = _pending.get(session_id, [])
        if entry in queue:
            queue.remove(entry)
        if not queue:
            _pending.pop(session_id, None)

    if not resolved:
        return "timeout"
    return entry.result or "deny"


# ---------------------------------------------------------------------------
# pre_tool_call hook
# ---------------------------------------------------------------------------

def _on_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    args = args or {}
    if not _requires_approval(tool_name):
        return None
    sid = session_id or "default"
    if _is_approved(sid, tool_name):
        return None

    choice = _queue_and_wait(sid, tool_name, args)

    if choice == "once":
        return None
    if choice == "session":
        with _lock:
            _session_approvals.setdefault(sid, set()).add(tool_name)
        return None
    if choice == "always":
        with _lock:
            _persistent_approvals.add(tool_name)
        _save_persistent()
        return None

    # deny / timeout
    if choice == "timeout":
        msg = (
            f"BLOCKED: approval for `{tool_name}` timed out after "
            f"{_DEFAULT_TIMEOUT_SECS}s. Ask the user to retry."
        )
    else:
        msg = f"BLOCKED: user denied `{tool_name}`."
    return {"action": "block", "message": msg}


def _on_session_end(session_id: str = "", **kwargs: Any) -> None:
    """Drop per-session approvals when the session finishes.

    Persistent approvals are untouched; only the in-memory session set goes.
    """
    if not session_id:
        return
    with _lock:
        _session_approvals.pop(session_id, None)
        # Don't drop _pending here — there's a small race where a tool call
        # is mid-block while the session ends.  The wait loop will see the
        # event never fires and return "timeout", which the agent surfaces
        # as a BLOCKED message.  That's fine.


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

def _resolve(choice: str) -> str:
    """Resolve the oldest pending entry for the current session.

    `choice` is the literal we'll feed back into the wait loop.
    """
    try:
        from tools.approval import get_current_session_key  # type: ignore
        sid = get_current_session_key("default")
    except Exception:
        sid = "default"

    with _lock:
        queue = _pending.get(sid)
        if not queue:
            return (
                "No pending MCP approval to resolve for this session. "
                "(If you meant a shell-command approval, use `/approve` / `/deny`.)"
            )
        entry = queue.pop(0)
        if not queue:
            _pending.pop(sid, None)

    entry.result = choice
    entry.event.set()

    tool = entry.tool_name
    if choice == "session":
        return f"OK — `{tool}` allowed for the rest of this session."
    if choice == "always":
        return f"OK — `{tool}` added to permanent allowlist."
    if choice == "deny":
        return f"Denied. Cancelled the pending call to `{tool}`."
    return f"OK — running `{tool}` once."


def _cmd_allow(_raw_args: str = "") -> str:
    return _resolve("once")


def _cmd_allow_session(_raw_args: str = "") -> str:
    return _resolve("session")


def _cmd_allow_always(_raw_args: str = "") -> str:
    return _resolve("always")


def _cmd_deny_mcp(_raw_args: str = "") -> str:
    return _resolve("deny")


def _cmd_mcp_approvals(_raw_args: str = "") -> str:
    """Status command — useful for debugging and for users to see state."""
    with _lock:
        persistent = sorted(_persistent_approvals)
        try:
            from tools.approval import get_current_session_key  # type: ignore
            sid = get_current_session_key("default")
        except Exception:
            sid = "default"
        session = sorted(_session_approvals.get(sid, set()))
        pending = [e.tool_name for e in _pending.get(sid, [])]

    lines = ["**MCP write-approval state**", ""]
    lines.append(f"Pending in this session: {', '.join(pending) if pending else '(none)'}")
    lines.append(f"Approved for this session: {', '.join(session) if session else '(none)'}")
    lines.append(f"Always-allow (persistent): {', '.join(persistent) if persistent else '(none)'}")
    lines.append("")
    lines.append(f"Tools known to plugin: {len(_tool_modes)} "
                 f"(write/destructive: "
                 f"{sum(1 for m in _tool_modes.values() if m in ('write','destructive'))})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# register() — entry point Hermes calls
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    _load_persistent()
    _load_catalog()

    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)

    ctx.register_command(
        "allow",
        _cmd_allow,
        description="Allow the pending MCP tool call once",
    )
    ctx.register_command(
        "allow-session",
        _cmd_allow_session,
        description="Allow the pending MCP tool for the rest of this session",
    )
    ctx.register_command(
        "allow-always",
        _cmd_allow_always,
        description="Allow the pending MCP tool permanently (saved to disk)",
    )
    ctx.register_command(
        "deny-mcp",
        _cmd_deny_mcp,
        description="Deny the pending MCP tool call",
    )
    ctx.register_command(
        "mcp-approvals",
        _cmd_mcp_approvals,
        description="Show pending + granted MCP approvals for this session",
    )

    logger.info(
        "mcp-write-approval registered. Known tools=%d (write/destructive=%d), "
        "always_allow=%d.",
        len(_tool_modes),
        sum(1 for m in _tool_modes.values() if m in ("write", "destructive")),
        len(_persistent_approvals),
    )
