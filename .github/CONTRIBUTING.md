# Contributing to mcp-write-approval

## Overview

`mcp-write-approval` is a single-file Hermes plugin (~360 lines, `__init__.py`). Changes are intentionally small and focused. The plugin must never reach into MCP-side state, modify agent-platform code, or extend Hermes core — it plugs into the public hook + slash-command interfaces only.

## Branch workflow

All changes land on a feature branch first, then merge to `main` via pull request. Direct pushes to `main` are blocked by org ruleset.

```
git checkout -b your-name/short-description
# make changes
git push -u origin your-name/short-description
# open a PR against main
```

## Development setup

A virtual environment with Hermes installed is required for the tests.

```bash
python -m venv .venv
source .venv/bin/activate
pip install hermes-agent   # or your org's Hermes distribution
```

## Running tests

```bash
python -m pytest tests/test_plugin.py -v
```

All 19 tests must pass before a PR is merged. The integration tests (`TestHermesIntegration`) run against the real Hermes plugin manager — no mocking — so a working Hermes install is required.

## What to test

| Area | What to cover |
| --- | --- |
| New tool classification logic | Add a `TestModeClassification` case |
| Changes to approval state | Add a `TestApprovalState` case |
| Changes to persistence | Add a `TestPersistence` case |
| Changes to block/unblock flow | Add a `TestBlockUnblock` case |
| Changes to `register()` or hook wiring | Add or update a `TestHermesIntegration` case |

## Code style

- Python 3.9+, no runtime dependencies beyond the standard library and Hermes.
- `from __future__ import annotations` at the top of every module.
- Keep the module-level state block (`_lock`, `_tool_modes`, `_session_approvals`, `_persistent_approvals`, `_pending`) minimal and clearly documented.
- All locking must go through `_lock`; never hold `_lock` while blocking on I/O or network.

## Env vars and configuration

Any new env var must be:

1. Added to `.env.template` with a descriptive comment.
2. Documented in the `README.md` configuration table.
3. Covered by at least one test that exercises the code path it enables.

## PR checklist

- [ ] `python -m pytest tests/test_plugin.py -v` passes locally.
- [ ] No source code, Dockerfiles, CI workflows, or dependency manifests modified (doc/config PRs only touch `README.md` and the standard config files).
- [ ] PR description fills out the template.

## Sensitive files

Never commit `.env`, `*.pem`, `*.key`, or `*.p12`. The org ruleset blocks these. `.env.template` (placeholder values only) is fine.
