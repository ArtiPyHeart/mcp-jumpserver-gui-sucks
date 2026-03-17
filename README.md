# mcp-jumpserver-gui-sucks

**CAUTION: Operate production machines with extreme care. This MCP assumes no responsibility for production incidents caused by unsafe or incompetent model behavior.**

A JumpServer 443-only MCP bridge for coding agents such as Codex and Claude. The project exposes a CLI-first, MFA-compatible, audit-preserving path into JumpServer assets without depending on port 2222 or any GUI-driven workflow in normal use.

## Current Status

The main CLI and MCP chain is working against a real JumpServer instance:

- CLI-first login with terminal-entered MFA
- persisted durable `access_key` auth for REST discovery
- persisted authenticated web-session cookies for KoKo terminal flows
- asset, node, connect-method, and asset-access discovery
- KoKo 443 WebSocket probing
- one-shot remote command execution through KoKo
- managed multi-turn terminal sessions for MCP-driven shell interaction
- managed shell reuse for repeated command execution against the same asset/account target
- process-local terminal idle reaping and session-cap enforcement
- explicit cookie-session refresh probing before terminal work
- a line-oriented CLI shell for non-MCP interactive terminal use

The current implementation is usable, but it is not feature-complete yet. The most important known limitation is:

- terminal access still depends on a valid cookie-backed web session, so a fully expired terminal session still requires a fresh `login` run with MFA

Terminal-oriented entry points now accept either the concrete JumpServer account ID/alias required by the API or a user-facing account reference such as `root`, `test-root`, or the account username. The MCP resolves that reference to the concrete per-asset account ID before opening terminal sessions or creating connection tokens.

The project does not currently aim to provide file-manager or SFTP coverage.

## Tracked Project Docs

- [docs/live-instance-recon.md](docs/live-instance-recon.md)
- [docs/web-terminal-flow.md](docs/web-terminal-flow.md)
- [docs/auth-state-format.md](docs/auth-state-format.md)
- [docs/cli-login-flow.md](docs/cli-login-flow.md)

## Upstream Reference Repositories

The repository keeps several untracked upstream JumpServer codebases under `extern/` for protocol and behavior reference only. They are not runtime dependencies of this package.

- `extern/jumpserver`: backend API, authentication, and permission-model reference
- `extern/koko`: KoKo terminal gateway and WebSocket behavior reference
- `extern/luna`: legacy web-terminal frontend flow reference, especially around browser-driven terminal bootstrap behavior
- `extern/lina`: newer web UI and API usage-pattern reference
- `extern/client`: official client-side implementation reference for adjacent access workflows

## Authentication Model

The runtime intentionally uses two auth layers:

- `access_key` for durable REST access
- authenticated web-session cookies for KoKo terminal access

Do not put live session secrets, cookies, or MFA values into MCP client config files. The intended flow is:

1. Run the CLI login command once.
2. Complete MFA in the terminal.
3. Let the tool persist auth state into the user-scoped application state directory.
4. Start the MCP server from Codex or Claude.

When the live JumpServer deployment enables a login captcha challenge, the CLI login command saves the captcha image under `/private/tmp/` and opens it with the system image viewer before prompting for the captcha value in the terminal.

By default, persisted auth state lives under the platform-specific user application state directory:

- macOS example: `~/Library/Application Support/mcp-jumpserver-gui-sucks/auth-state.json`

Advanced users can override the location with:

- `MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR`
- `MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE`

## Install

Use the published package directly:

```bash
uvx mcp-jumpserver-gui-sucks --help
```

## Login Before Starting MCP

```bash
uvx mcp-jumpserver-gui-sucks login \
  --base-url https://jumpserver.example.com \
  --username alice
```

Useful verification commands:

```bash
uvx mcp-jumpserver-gui-sucks doctor
uvx mcp-jumpserver-gui-sucks refresh-session --force
```

The login command persists state outside the repository. MCP client config should only describe how to find that state, not embed the secrets themselves.

## MCP Configuration

The MCP server entrypoint is:

```bash
uvx mcp-jumpserver-gui-sucks serve
```

`serve` defaults to `stdio`, which is the correct transport for Codex and Claude desktop-style MCP clients.

## Recommended Agent Terminal Workflow

When a coding agent plans to work on one machine for more than one command, the recommended workflow is:

1. Call `jms_terminal_usage_guide`.
2. Call `jms_acquire_terminal_session` with `asset_ref` and `account_ref`.
3. Reuse the returned `session_handle` through repeated `jms_execute_in_terminal_session` calls.
4. Call `jms_close_terminal_session` when the task is complete.

This keeps one KoKo shell open per target and avoids leaving many short-lived web-shell records behind in JumpServer.

### Codex (`~/.codex/config.toml`)

This matches the `mcp_servers.*` structure already used in your local `~/.codex/config.toml`:

```toml
[mcp_servers.mcp-jumpserver-gui-sucks]
command = "uvx"
args = ["mcp-jumpserver-gui-sucks", "serve"]
startup_timeout_sec = 60.0

[mcp_servers.mcp-jumpserver-gui-sucks.env]
MCP_JUMPSERVER_GUI_SUCKS_BASE_URL = "https://jumpserver.example.com"
MCP_JUMPSERVER_GUI_SUCKS_VERIFY_TLS = "true"
MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS = "3600"
MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS = "30"
MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS = "8"

# Optional when the default state directory is not desired.
# MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR = "/Users/alice/Library/Application Support/mcp-jumpserver-gui-sucks"
# MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE = "/Users/alice/Library/Application Support/mcp-jumpserver-gui-sucks/auth-state.json"
# MCP_JUMPSERVER_GUI_SUCKS_ORG_ID = "00000000-0000-0000-0000-000000000002"
```

### Claude (`~/.claude.json`)

This matches the `mcpServers` JSON shape already present in your local `~/.claude.json`:

```json
{
  "mcpServers": {
    "mcp-jumpserver-gui-sucks": {
      "command": "uvx",
      "args": ["mcp-jumpserver-gui-sucks", "serve"],
      "env": {
        "MCP_JUMPSERVER_GUI_SUCKS_BASE_URL": "https://jumpserver.example.com",
        "MCP_JUMPSERVER_GUI_SUCKS_VERIFY_TLS": "true",
        "MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS": "3600",
        "MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS": "30",
        "MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS": "8"
      }
    }
  }
}
```

## Supported Environment Variables

The current runtime reads these environment variables:

- `MCP_JUMPSERVER_GUI_SUCKS_BASE_URL`
- `MCP_JUMPSERVER_GUI_SUCKS_ORG_ID`
- `MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR`
- `MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE`
- `MCP_JUMPSERVER_GUI_SUCKS_VERIFY_TLS`
- `MCP_JUMPSERVER_GUI_SUCKS_LOG_LEVEL`
- `MCP_JUMPSERVER_GUI_SUCKS_REQUEST_TIMEOUT_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS`

The recommended minimum MCP config is usually:

- `MCP_JUMPSERVER_GUI_SUCKS_BASE_URL`
- optionally `MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR` or `MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE`

## PyPI Release Automation

The repository now includes [publish-pypi.yml](.github/workflows/publish-pypi.yml).

Its behavior is intentionally:

- every push to `main` inspects `pyproject.toml`
- if the package version changed and that version does not already exist on PyPI, GitHub Actions builds and publishes it
- if the version did not change, the workflow skips publishing
- if the version already exists on PyPI, the workflow skips publishing
- `workflow_dispatch` can be used to publish the current version manually when it is not yet on PyPI

The publish job uses PyPI Trusted Publishing through GitHub OIDC. Configure PyPI to trust this repository and workflow before expecting the publish step to succeed.

Recommended PyPI trusted publisher settings:

- owner: `ArtiPyHeart`
- repository: `mcp-jumpserver-gui-sucks`
- workflow file: `.github/workflows/publish-pypi.yml`
- environment name: `pypi`

After Trusted Publishing is configured once, later pushes to `main` that bump `project.version` in `pyproject.toml` will publish automatically.

## Current CLI Surface

- `mcp-jumpserver-gui-sucks login`
- `mcp-jumpserver-gui-sucks paths`
- `mcp-jumpserver-gui-sucks doctor`
- `mcp-jumpserver-gui-sucks refresh-session`
- `mcp-jumpserver-gui-sucks resolve-target`
- `mcp-jumpserver-gui-sucks koko-probe`
- `mcp-jumpserver-gui-sucks terminal-exec`
- `mcp-jumpserver-gui-sucks terminal-shell`
- `mcp-jumpserver-gui-sucks save-state`
- `mcp-jumpserver-gui-sucks clear-state`
- `mcp-jumpserver-gui-sucks serve`

## Current MCP Tools

- `jms_paths`
- `jms_status`
- `jms_terminal_usage_guide`
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
- `jms_acquire_terminal_session`
- `jms_list_terminal_sessions`
- `jms_open_terminal_session`
- `jms_write_terminal_session`
- `jms_read_terminal_session`
- `jms_execute_in_terminal_session`
- `jms_resize_terminal_session`
- `jms_close_terminal_session`

## Operational Notes

- Managed terminal sessions are process-local and intended to live only for the MCP server process lifetime.
- `jms_terminal_usage_guide` returns the preferred terminal workflow for coding agents and should be consulted at the start of terminal-heavy work.
- `jms_acquire_terminal_session` is the preferred high-level entrypoint for repeated work on one machine because it resolves the target and reuses an existing shell when possible.
- `jms_execute_in_terminal_session` is the preferred way to run repeated commands after a `session_handle` has already been acquired.
- `jms_execute_koko_command` now reuses a matching managed shell for the same asset/account/protocol/connect-method target when one is already active in the current MCP server process.
- `jms_execute_koko_command` returns the managed `session_handle`, so callers can later use `jms_close_terminal_session` for an explicit manual shutdown.
- The default managed shell idle timeout is 1 hour. Override it with `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS` if a different retention window is required.
- When the MCP server process exits normally, it closes all managed KoKo shells before returning.
- `terminal-shell` is line-oriented, not a full raw TTY emulator.
- Terminal entrypoints preflight the cookie-backed web session before opening KoKo.
- If the cookie-backed session is already invalid, terminal calls fail early with an explicit re-login requirement instead of a low-level websocket failure.
- REST discovery can continue to work when the durable `access_key` remains valid, even if terminal access requires a fresh login.
