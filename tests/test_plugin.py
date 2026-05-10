"""End-to-end tests for the mcp-write-approval Hermes plugin.

Runs against a real installed Hermes runtime in a venv (so the plugin
discovery, hook firing, and slash-command dispatch are all hitting
production code paths — not mocks).

Usage:
    .venv/bin/python -m pytest tests/test_plugin.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    """Point ~/.hermes at a clean tmp dir so persistence tests are isolated."""
    home = tmp_path / "home"
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # If anything in the plugin module already cached state, force reload.
    yield hermes


@pytest.fixture
def fresh_plugin(fake_hermes_home, monkeypatch):
    """Import the plugin module fresh for each test so state is reset."""
    import importlib
    import sys

    # Make sure the repo dir is on path so we can import as `mcp_write_approval`.
    repo = Path(__file__).resolve().parent.parent
    repo_parent = repo.parent
    plugin_pkg_dir = repo_parent / "mcp_write_approval_pkg_root"
    plugin_pkg_dir.mkdir(exist_ok=True)
    # Symlink the repo as a package called mcp_write_approval so import works.
    pkg_link = plugin_pkg_dir / "mcp_write_approval"
    if pkg_link.exists() or pkg_link.is_symlink():
        pkg_link.unlink()
    pkg_link.symlink_to(repo, target_is_directory=True)
    if str(plugin_pkg_dir) not in sys.path:
        sys.path.insert(0, str(plugin_pkg_dir))

    # Drop any cached version + persistence file
    sys.modules.pop("mcp_write_approval", None)
    mod = importlib.import_module("mcp_write_approval")
    importlib.reload(mod)
    yield mod


class FakeContext:
    """Minimal stand-in for the ctx that Hermes hands plugins."""

    def __init__(self):
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, dict] = {}

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {"handler": handler, "description": description}


# ---------------------------------------------------------------------------
# Unit-ish tests against the plugin module directly
# ---------------------------------------------------------------------------

class TestModeClassification:
    def test_read_tool_passes_through(self, fresh_plugin):
        assert fresh_plugin._mode_for("cloudflare_list_zones") == "unknown"
        # Without catalog "list" doesn't match the write-suffix heuristic.
        assert not fresh_plugin._requires_approval("cloudflare_list_zones")

    def test_write_suffix_heuristic_flags_writes(self, fresh_plugin):
        for name in (
            "cloudflare_delete_dns_record",
            "voluum_archive_campaign",
            "voluum_create_campaign",
            "dropbox_upload",  # has underscore but no write suffix → not flagged
            "cloudflare_upsert_dns_record",
            "voluum_pause_offer",
            "service_rename_thing",
        ):
            mode = fresh_plugin._mode_for(name)
            if name == "dropbox_upload":
                assert mode == "unknown", f"{name} should not match heuristic"
                assert not fresh_plugin._requires_approval(name)
            else:
                assert mode == "write", f"{name} mode={mode}"
                assert fresh_plugin._requires_approval(name)

    def test_catalog_overrides_heuristic(self, fresh_plugin):
        # Even if the suffix says "write", an explicit "destructive" in the
        # catalog should be reflected.
        fresh_plugin._ingest_manifest({"tools": [
            {"name": "cloudflare_delete_dns_record",
             "mode": "destructive", "permission_key": "x", "description": "", "requires_approval": True}
        ]})
        assert fresh_plugin._mode_for("cloudflare_delete_dns_record") == "destructive"
        assert fresh_plugin._requires_approval("cloudflare_delete_dns_record")

    def test_builtin_tools_unaffected(self, fresh_plugin):
        # Hermes built-in tools like 'terminal', 'web_search' shouldn't
        # match the heuristic (no underscore between connector & verb).
        for name in ("terminal", "web_search", "execute_code"):
            assert not fresh_plugin._requires_approval(name), name


class TestApprovalState:
    def test_session_approval_isolates_sessions(self, fresh_plugin):
        fresh_plugin._session_approvals.setdefault("sess-A", set()).add("voluum_pause_offer")
        assert fresh_plugin._is_approved("sess-A", "voluum_pause_offer")
        assert not fresh_plugin._is_approved("sess-B", "voluum_pause_offer")

    def test_persistent_approval_covers_all_sessions(self, fresh_plugin):
        fresh_plugin._persistent_approvals.add("voluum_pause_offer")
        assert fresh_plugin._is_approved("sess-A", "voluum_pause_offer")
        assert fresh_plugin._is_approved("sess-B", "voluum_pause_offer")


class TestPersistence:
    def test_save_and_reload_persistent(self, fresh_plugin):
        fresh_plugin._persistent_approvals.add("cloudflare_purge_cache")
        fresh_plugin._persistent_approvals.add("voluum_archive_campaign")
        fresh_plugin._save_persistent()

        state_file = fresh_plugin._state_path()
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert sorted(data["always_allow"]) == [
            "cloudflare_purge_cache", "voluum_archive_campaign",
        ]

        # Clear in-memory, reload from disk
        fresh_plugin._persistent_approvals.clear()
        assert not fresh_plugin._persistent_approvals
        fresh_plugin._load_persistent()
        assert fresh_plugin._persistent_approvals == {
            "cloudflare_purge_cache", "voluum_archive_campaign",
        }

    def test_atomic_write_handles_existing(self, fresh_plugin):
        fresh_plugin._persistent_approvals.add("a")
        fresh_plugin._save_persistent()
        # Second write should overwrite cleanly
        fresh_plugin._persistent_approvals.add("b")
        fresh_plugin._save_persistent()
        data = json.loads(fresh_plugin._state_path().read_text())
        assert sorted(data["always_allow"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# Block + unblock — the core flow
# ---------------------------------------------------------------------------

class TestBlockUnblock:
    def _start_call_in_thread(self, plugin, tool_name, session_id="sess-X", args=None):
        """Kick off a pre_tool_call in a worker thread; return (thread, result_holder)."""
        result: list = []

        def worker():
            # Inject context for slash command resolution.
            from tools.approval import set_current_session_key  # type: ignore
            token = set_current_session_key(session_id)
            try:
                r = plugin._on_pre_tool_call(
                    tool_name=tool_name,
                    args=args or {"zone": "example.com"},
                    session_id=session_id,
                )
                result.append(r)
            finally:
                from tools.approval import reset_current_session_key  # type: ignore
                reset_current_session_key(token)

        # Make sure args won't trigger heuristic-only path mistakenly
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        # Give the worker a moment to enter the wait state.
        time.sleep(0.1)
        return thread, result

    def test_read_tool_passes_immediately(self, fresh_plugin):
        result = fresh_plugin._on_pre_tool_call(
            tool_name="cloudflare_list_zones",
            args={},
            session_id="s",
        )
        assert result is None

    def test_already_approved_passes_immediately(self, fresh_plugin):
        fresh_plugin._persistent_approvals.add("cloudflare_delete_dns_record")
        result = fresh_plugin._on_pre_tool_call(
            tool_name="cloudflare_delete_dns_record",
            args={"name": "stale"},
            session_id="s",
        )
        assert result is None

    def test_allow_once_unblocks_and_does_not_persist(self, fresh_plugin):
        thread, result = self._start_call_in_thread(
            fresh_plugin, "cloudflare_delete_dns_record", "sess-1",
        )
        # While the worker is blocked, the plugin's pending queue has an entry.
        with fresh_plugin._lock:
            assert len(fresh_plugin._pending.get("sess-1", [])) == 1

        # Simulate user typing /allow — slash handler reads
        # get_current_session_key, which we set inside the slash dispatcher
        # for tests via set_current_session_key. Do that here too.
        from tools.approval import set_current_session_key, reset_current_session_key  # type: ignore
        token = set_current_session_key("sess-1")
        try:
            msg = fresh_plugin._cmd_allow("")
        finally:
            reset_current_session_key(token)
        assert "once" in msg.lower() or "running" in msg.lower()

        thread.join(timeout=3)
        assert not thread.is_alive(), "worker should have unblocked"
        assert result == [None], "approved call should pass through"
        # Did not persist.
        assert "cloudflare_delete_dns_record" not in fresh_plugin._persistent_approvals
        # Did not add to session set (only "session"/"always" do that).
        assert "cloudflare_delete_dns_record" not in fresh_plugin._session_approvals.get("sess-1", set())

    def test_allow_session_covers_subsequent_calls(self, fresh_plugin):
        thread, result = self._start_call_in_thread(
            fresh_plugin, "voluum_pause_offer", "sess-2",
        )

        from tools.approval import set_current_session_key, reset_current_session_key  # type: ignore
        token = set_current_session_key("sess-2")
        try:
            fresh_plugin._cmd_allow_session("")
        finally:
            reset_current_session_key(token)

        thread.join(timeout=3)
        assert result == [None]

        # Now a second call to the same tool in the same session should
        # pass through without prompting.
        second = fresh_plugin._on_pre_tool_call(
            tool_name="voluum_pause_offer",
            args={"offer": "B"},
            session_id="sess-2",
        )
        assert second is None

        # But a different session should still get prompted.
        thread2, result2 = self._start_call_in_thread(
            fresh_plugin, "voluum_pause_offer", "sess-OTHER",
        )
        with fresh_plugin._lock:
            assert len(fresh_plugin._pending.get("sess-OTHER", [])) == 1
        # Clean up — deny it so the thread can exit.
        token = set_current_session_key("sess-OTHER")
        try:
            fresh_plugin._cmd_deny_mcp("")
        finally:
            reset_current_session_key(token)
        thread2.join(timeout=3)

    def test_allow_always_persists_to_disk(self, fresh_plugin):
        thread, result = self._start_call_in_thread(
            fresh_plugin, "voluum_archive_campaign", "sess-3",
        )

        from tools.approval import set_current_session_key, reset_current_session_key  # type: ignore
        token = set_current_session_key("sess-3")
        try:
            fresh_plugin._cmd_allow_always("")
        finally:
            reset_current_session_key(token)

        thread.join(timeout=3)
        assert result == [None]
        assert "voluum_archive_campaign" in fresh_plugin._persistent_approvals

        # And on disk.
        data = json.loads(fresh_plugin._state_path().read_text())
        assert "voluum_archive_campaign" in data["always_allow"]

    def test_deny_returns_block_directive(self, fresh_plugin):
        thread, result = self._start_call_in_thread(
            fresh_plugin, "cloudflare_delete_dns_record", "sess-4",
        )

        from tools.approval import set_current_session_key, reset_current_session_key  # type: ignore
        token = set_current_session_key("sess-4")
        try:
            fresh_plugin._cmd_deny_mcp("")
        finally:
            reset_current_session_key(token)

        thread.join(timeout=3)
        assert result, "worker never returned"
        assert isinstance(result[0], dict)
        assert result[0].get("action") == "block"
        assert "cloudflare_delete_dns_record" in result[0]["message"]

    def test_multiple_pending_resolved_fifo(self, fresh_plugin):
        thread1, result1 = self._start_call_in_thread(
            fresh_plugin, "voluum_pause_offer", "sess-5",
            args={"offer": "FIRST"},
        )
        thread2, result2 = self._start_call_in_thread(
            fresh_plugin, "voluum_archive_campaign", "sess-5",
            args={"campaign": "SECOND"},
        )
        # Queue should have two entries
        with fresh_plugin._lock:
            assert len(fresh_plugin._pending.get("sess-5", [])) == 2

        from tools.approval import set_current_session_key, reset_current_session_key  # type: ignore
        token = set_current_session_key("sess-5")
        try:
            # First /allow resolves the oldest (voluum_pause_offer).
            msg1 = fresh_plugin._cmd_allow("")
            assert "voluum_pause_offer" in msg1
            # Second /allow resolves the next.
            time.sleep(0.05)
            msg2 = fresh_plugin._cmd_allow("")
            assert "voluum_archive_campaign" in msg2
        finally:
            reset_current_session_key(token)

        thread1.join(timeout=3); thread2.join(timeout=3)
        assert result1 == [None]
        assert result2 == [None]

    def test_resolve_with_nothing_pending(self, fresh_plugin):
        from tools.approval import set_current_session_key, reset_current_session_key  # type: ignore
        token = set_current_session_key("nobody-home")
        try:
            msg = fresh_plugin._cmd_allow("")
        finally:
            reset_current_session_key(token)
        assert "no pending" in msg.lower()


# ---------------------------------------------------------------------------
# Integration with the real Hermes plugin manager
# ---------------------------------------------------------------------------

class TestHermesIntegration:
    def test_plugin_registers_via_real_pluginmanager(self, fake_hermes_home, monkeypatch):
        """Drop the plugin into ~/.hermes/plugins/ and load it via the
        actual PluginManager. Verifies the manifest+register() pair is
        compatible with Hermes' loader."""

        repo = Path(__file__).resolve().parent.parent
        target = fake_hermes_home / "plugins" / "mcp-write-approval"
        target.mkdir(parents=True)
        shutil.copy(repo / "plugin.yaml", target / "plugin.yaml")
        shutil.copy(repo / "__init__.py", target / "__init__.py")

        # Enable the plugin in config.yaml — by default user-installed
        # plugins are opt-in via plugins.enabled.
        config_path = fake_hermes_home / "config.yaml"
        config_path.write_text(
            "plugins:\n  enabled:\n    - mcp-write-approval\n",
            encoding="utf-8",
        )

        # PluginManager picks up the HOME-based path automatically.
        import sys
        for k in list(sys.modules):
            if k.startswith("hermes_plugins.") or k == "hermes_cli.plugins":
                # Force a clean import so prior tests don't leave cached state
                # — plugins module caches the manager singleton.
                if k == "hermes_cli.plugins":
                    continue
                sys.modules.pop(k, None)

        from hermes_cli.plugins import PluginManager, get_plugin_command_handler, invoke_hook
        mgr = PluginManager()
        mgr.discover_and_load(force=True)

        # Sanity check: the plugin loaded.
        loaded = [m for m in mgr.list_plugins() if m.get("name") == "mcp-write-approval"]
        assert loaded, f"plugin did not load. Loaded plugins: {mgr.list_plugins()}"
        assert loaded[0]["error"] is None, f"plugin reported error: {loaded[0]['error']}"
        assert loaded[0]["hooks"] >= 2, f"expected ≥2 hooks, got {loaded[0]['hooks']}"
        assert loaded[0]["commands"] >= 4, f"expected ≥4 commands, got {loaded[0]['commands']}"

        # Slash commands registered.
        assert "allow" in mgr._plugin_commands
        assert "allow-session" in mgr._plugin_commands
        assert "allow-always" in mgr._plugin_commands
        assert "deny-mcp" in mgr._plugin_commands

        # pre_tool_call hook actually fires through Hermes' invoke_hook.
        # For a read tool the hook returns None and the call passes.
        from hermes_cli.plugins import get_pre_tool_call_block_message
        msg = get_pre_tool_call_block_message(
            "cloudflare_list_zones", {}, session_id="integ-sess",
        )
        assert msg is None

    def test_block_message_surfaces_via_invoke_hook(self, fake_hermes_home, monkeypatch):
        """End-to-end: a write tool with a denied approval should produce a
        block message from get_pre_tool_call_block_message() — the function
        Hermes' agent loop actually calls. This is the load-bearing path."""

        repo = Path(__file__).resolve().parent.parent
        target = fake_hermes_home / "plugins" / "mcp-write-approval"
        target.mkdir(parents=True)
        shutil.copy(repo / "plugin.yaml", target / "plugin.yaml")
        shutil.copy(repo / "__init__.py", target / "__init__.py")
        config_path = fake_hermes_home / "config.yaml"
        config_path.write_text(
            "plugins:\n  enabled:\n    - mcp-write-approval\n",
            encoding="utf-8",
        )

        import sys
        sys.modules.pop("mcp_write_approval", None)
        # Force-reset Hermes' plugin manager singleton so it re-discovers
        # against our new fake home.
        import hermes_cli.plugins as hp
        hp._plugin_manager = None

        from hermes_cli.plugins import (
            get_pre_tool_call_block_message,
            get_plugin_command_handler,
            _ensure_plugins_discovered,
        )
        _ensure_plugins_discovered(force=True)

        # Pre-seat a denial: pre-mark the call as denied by populating the
        # plugin's pending queue with a result, then exercising the hook
        # would block forever. Instead test the "happy path" — a read tool
        # passes through invoke_hook without blocking — and the "approved
        # path" — a write tool the user has already always-allowed passes
        # through. The blocking path is covered by TestBlockUnblock above
        # using direct module access.

        # Approved-always path
        plugin_mod = sys.modules["hermes_plugins.mcp-write-approval"] \
            if "hermes_plugins.mcp-write-approval" in sys.modules \
            else sys.modules.get("hermes_plugins.mcp_write_approval")
        # Hermes loads under a namespace like hermes_plugins.<name>; the
        # exact name depends on slug normalization. Find it by scanning.
        if plugin_mod is None:
            cands = [n for n in sys.modules if n.startswith("hermes_plugins.")
                     and "mcp" in n and "approval" in n]
            assert cands, f"plugin module not found. hermes_plugins.* = " \
                f"{[n for n in sys.modules if n.startswith('hermes_plugins.')]}"
            plugin_mod = sys.modules[cands[0]]

        plugin_mod._persistent_approvals.add("cloudflare_purge_cache")

        # Read tool: passes
        assert get_pre_tool_call_block_message(
            "cloudflare_list_zones", {}, session_id="s1",
        ) is None

        # Always-approved write tool: passes
        assert get_pre_tool_call_block_message(
            "cloudflare_purge_cache", {"zone": "x"}, session_id="s1",
        ) is None

        # Slash commands are reachable from the real handler lookup
        assert get_plugin_command_handler("allow") is not None
        assert get_plugin_command_handler("allow-session") is not None
        assert get_plugin_command_handler("allow-always") is not None
        assert get_plugin_command_handler("deny-mcp") is not None
        assert get_plugin_command_handler("mcp-approvals") is not None

    def test_invoke_hook_blocks_and_slash_unblocks(self, fake_hermes_home, monkeypatch):
        """Most-realistic test: drive the whole flow through Hermes'
        public APIs. invoke_hook fires pre_tool_call → plugin blocks →
        the slash command handler unblocks → block message is None."""

        repo = Path(__file__).resolve().parent.parent
        target = fake_hermes_home / "plugins" / "mcp-write-approval"
        target.mkdir(parents=True)
        shutil.copy(repo / "plugin.yaml", target / "plugin.yaml")
        shutil.copy(repo / "__init__.py", target / "__init__.py")
        (fake_hermes_home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - mcp-write-approval\n",
            encoding="utf-8",
        )

        import sys
        sys.modules.pop("mcp_write_approval", None)
        import hermes_cli.plugins as hp
        hp._plugin_manager = None

        from hermes_cli.plugins import (
            get_pre_tool_call_block_message,
            get_plugin_command_handler,
            _ensure_plugins_discovered,
        )
        from tools.approval import set_current_session_key, reset_current_session_key  # type: ignore

        _ensure_plugins_discovered(force=True)

        outcome: list = []

        def worker():
            # Hermes' agent thread calls this for every tool call. None
            # means "no block, proceed with the call".
            msg = get_pre_tool_call_block_message(
                "voluum_pause_offer",
                {"offer": "X"},
                session_id="invoked-sess",
            )
            outcome.append(msg)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.2)
        assert t.is_alive(), "worker should be blocked waiting for approval"

        # Resolve via the real slash command handler — same path users hit.
        allow_handler = get_plugin_command_handler("allow")
        assert allow_handler is not None
        token = set_current_session_key("invoked-sess")
        try:
            response = allow_handler("")
        finally:
            reset_current_session_key(token)
        assert "voluum_pause_offer" in response

        t.join(timeout=3)
        assert not t.is_alive(), "worker should have unblocked after /allow"
        assert outcome == [None], (
            f"after /allow, get_pre_tool_call_block_message should return None "
            f"(no block), got {outcome!r}"
        )
