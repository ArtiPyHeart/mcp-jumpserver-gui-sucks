# mcp-jumpserver-gui-sucks

A JumpServer 443-only MCP client for coding agents such as Codex. The project aims to reuse JumpServer's Web, API, and WebSocket capabilities without relying on port 2222, and expose them as an auditable, authorization-aware, and extensible entry point for LLM-driven operations on assets behind the bastion host.

## Current Constraints

- Use only the JumpServer 443 path and do not depend on port 2222.
- Prefer REST first and use WebSocket only where interactive terminal behavior is required.
- Start with CLI-first login, CLI-entered MFA, and durable access-key persistence whenever possible.
- Do not bypass JumpServer authorization, auditing, command filtering, or approval workflows.

## Repository Notes

- The detailed development plan lives in `mcp-jumpserver-gui-sucks-development-plan.md`.
- Live instance recon notes live in `docs/live-instance-recon.md`.
- Web terminal flow notes live in `docs/web-terminal-flow.md`.
- Auth-state file notes live in `docs/auth-state-format.md`.
- CLI login notes live in `docs/cli-login-flow.md`.
- `extern/` stores upstream JumpServer source code for reference and is not tracked by Git.
- The development workflow uses `uv`, and the current interpreter is `.venv/bin/python`.
- The release target is a package that can be invoked directly with `uvx`.

## Current Status

The repository now has an initial CLI-first authentication and discovery surface:

- CLI login with terminal-entered MFA
- durable JumpServer access-key persistence in the user-scoped state directory
- a web-session-first login implementation path for KoKo-oriented work
- terminal-session cookie keepalive and stale-session recovery for KoKo-oriented work
- a verified CLI-side KoKo websocket probe
- a verified one-shot KoKo terminal command execution path
- a verified MCP-side managed KoKo terminal session flow for multi-turn interaction
- runtime probing through `paths` and `doctor`
- MCP tools for profile, nodes, asset discovery, and asset access summaries

The current login work is intentionally split into two auth layers:

- authenticated web-session cookies for KoKo and other browser-session-protected flows
- durable access keys for REST discovery and later API calls

The current terminal work is intentionally split into two execution modes:

- one-shot command execution for simple remote checks and automation
- process-local managed terminal sessions for MCP-driven multi-turn shell interaction

## Near-Term Priorities

1. Keep extending the instance-specific map from `/api/docs/` and verified live traffic.
2. Expand the REST surface for permissions, connection tokens, and non-interactive command execution.
3. Add KoKo WebSocket coverage for interactive terminal workflows that cannot stay REST-only.

## Current CLI Surface

- `uv run mcp-jumpserver-gui-sucks login --base-url https://jumpserver.example.com --username alice`
- `uv run mcp-jumpserver-gui-sucks paths`
- `uv run mcp-jumpserver-gui-sucks doctor`
- `uv run mcp-jumpserver-gui-sucks refresh-session --force`
- `uv run mcp-jumpserver-gui-sucks resolve-target --asset-ref 192.168.15.70 --account-ref root`
- `uv run mcp-jumpserver-gui-sucks koko-probe --asset-id ... --account ...`
- `uv run mcp-jumpserver-gui-sucks terminal-exec --asset-ref 192.168.15.70 --account-ref root --command ...`
- `uv run mcp-jumpserver-gui-sucks terminal-shell --asset-ref 192.168.15.70 --account-ref root`
- `uv run mcp-jumpserver-gui-sucks save-state --base-url ... --cookie ... --header ... --access-key-id ... --access-key-secret ...`
- `uv run mcp-jumpserver-gui-sucks clear-state`

## Current MCP Tools

- `jms_paths`
- `jms_status`
- `jms_profile`
- `jms_list_nodes`
- `jms_list_assets`
- `jms_get_asset`
- `jms_list_connect_methods`
- `jms_get_asset_access`
- `jms_resolve_terminal_target`
- `jms_list_connection_tokens`
- `jms_create_connection_token`
- `jms_expire_connection_token`
- `jms_refresh_terminal_auth`
- `jms_probe_koko_terminal`
- `jms_execute_koko_command`
- `jms_list_terminal_sessions`
- `jms_open_terminal_session`
- `jms_write_terminal_session`
- `jms_read_terminal_session`
- `jms_resize_terminal_session`
- `jms_close_terminal_session`

## Managed Terminal Session Notes

- Managed terminal sessions are process-local and intended for the MCP server lifetime, not for repeated standalone CLI invocations.
- The CLI also now exposes a line-oriented `terminal-shell` command for single-process interactive shell work without requiring UUID-only input.
- Managed terminal sessions now have automatic idle reaping and a configurable session cap.
- The relevant runtime environment variables are:
  - `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS`
  - `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS`
  - `MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS`
- KoKo-oriented terminal entrypoints now preflight the cookie-backed web session through `GET /api/v1/authentication/user-session/`.
- If that cookie session is still valid, the runtime refreshes and re-persists the cookie expiry before opening the terminal.
- If that cookie session is no longer valid, the runtime raises an explicit re-login error instead of deferring the failure to a lower-level websocket handshake.
- `jms_read_terminal_session` returns both:
  - `output_text`: a fuller terminal transcript with prompt and echoed input preserved
  - `stdout_text`: a cleaned view with ANSI noise, shell prompt noise, and leading echoed input reduced for agent consumption
- The verified lifecycle is:
  - open session
  - write terminal data
  - read terminal output
  - resize terminal
  - close terminal or let the remote shell close it
- When a session is reaped for idleness, later reads and writes will fail with an explicit expired-session error instead of silently keeping a stale handle alive.
