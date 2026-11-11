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
- non-blocking buffered terminal output reads for managed sessions
- explicit managed-session command interruption with `Ctrl-C`
- verified effective-user tracking and managed `sudo`/`su` login-shell transitions
- process-local terminal idle reaping and session-cap enforcement
- explicit cookie-session refresh probing before terminal work
- a line-oriented CLI shell for non-MCP interactive terminal use
- managed KoKo elFinder SFTP sessions over port 443
- remote directory listing, metadata, directory creation, upload, download, rename, and deletion
- explicit overwrite and recursive-delete safety gates for file operations

The current implementation is usable, but it is not feature-complete yet. The most important known limitation is:

- terminal access still depends on a valid cookie-backed web session, so a fully expired terminal session still requires a fresh `login` run with MFA

Terminal-oriented entry points now accept either the concrete JumpServer account ID/alias required by the API or a user-facing account reference such as `root`, `test-root`, or the account username. The MCP resolves that reference to the concrete per-asset account ID before opening terminal sessions or creating connection tokens.

## Tracked Project Docs

- [docs/live-instance-recon.md](docs/live-instance-recon.md)
- [docs/web-terminal-flow.md](docs/web-terminal-flow.md)
- [docs/auth-state-format.md](docs/auth-state-format.md)
- [docs/cli-login-flow.md](docs/cli-login-flow.md)
- [docs/sftp-flow.md](docs/sftp-flow.md)

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

To supply a password from a script, use `login --password-stdin` and pipe one
password line into the command. MFA remains interactive. The optional
[login script](examples/login.sh) first checks the persisted session and starts a
fresh login only when `refresh-session --force` exits with `3`; network and
service errors stop the script. See the [CLI login guide](docs/cli-login-flow.md)
for plaintext password handling and exit-status details.

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
3. Call `jms_get_terminal_identity` before work where the effective operating-system user matters.
4. Call `jms_switch_terminal_user` to enter another user's login shell through `sudo` or `su`.
5. Use `jms_run_terminal_command` for short command-style work. Each result includes the verified current identity.
6. Use `jms_send_terminal_input` plus `jms_read_terminal_output` for shell-style interaction.
7. Call `jms_interrupt_terminal_session` when a command needs to be stopped.
8. Call `jms_exit_terminal_user` to leave one MCP-managed user shell and restore the previous user.
9. Call `jms_close_terminal_session` when the task is complete.

This keeps one KoKo shell open per target and avoids leaving many short-lived web-shell records behind in JumpServer.

`jms_switch_terminal_user` defaults to passwordless `sudo`. Set `method="su"` for
target-account authentication, or provide the optional `password` argument when either
method requires one. The MCP sends the password only after the remote terminal presents
an authentication prompt, and it excludes the password from command text, results, and
application logs. MCP clients may retain tool arguments in their own conversation or trace
history, so treat a supplied password as exposed to the configured MCP client and model.

The managed session records the effective UID, username, home directory, shell, and user-switch
depth. Prefer the explicit switch and exit tools over manually typing `sudo su`; ordinary command
execution still refreshes the effective identity so manually initiated transitions remain visible.

## Recommended Agent SFTP Workflow

1. Call `jms_sftp_usage_guide`.
2. Call `jms_acquire_sftp_session` with `asset_ref` and `account_ref`.
3. Reuse the returned `session_handle` for all file operations on that target.
4. Call `jms_sftp_list` or `jms_sftp_stat` before modifying remote paths.
5. Use `jms_sftp_mkdir`, `jms_sftp_upload`, `jms_sftp_download`, or `jms_sftp_rename`.
6. Call `jms_close_sftp_session` when the task is complete.

SFTP paths are absolute within the virtual root exposed by JumpServer, not necessarily the
server's operating-system root. For example, if JumpServer maps an account's SFTP root to
`/tmp`, the MCP path `/project/file.txt` refers to the server path `/tmp/project/file.txt`.

Uploads and downloads refuse destinations that already exist unless `overwrite=true`.
KoKo's elFinder upload API does not expose an atomic create-if-absent operation, so an
external client can still create the same remote path between the preflight check and upload.
Avoid concurrent writers to the same path when `overwrite=false` is relied on as a safety gate.
Remote deletion requires `confirm=true`. Because KoKo directory deletion is recursive,
directories additionally require `recursive=true`; it is never enabled by default.

### Codex (`~/.codex/config.toml`)

This matches the `mcp_servers.*` structure already used in your local `~/.codex/config.toml`:

```toml
[mcp_servers.mcp-jumpserver-gui-sucks]
command = "uvx"
args = ["mcp-jumpserver-gui-sucks", "serve"]
startup_timeout_sec = 60.0
tool_timeout_sec = 600.0

[mcp_servers.mcp-jumpserver-gui-sucks.env]
MCP_JUMPSERVER_GUI_SUCKS_BASE_URL = "https://jumpserver.example.com"
MCP_JUMPSERVER_GUI_SUCKS_VERIFY_TLS = "true"
MCP_JUMPSERVER_GUI_SUCKS_USE_ENV_PROXY = "false"
MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS = "3600"
MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS = "30"
MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS = "8"
MCP_JUMPSERVER_GUI_SUCKS_SFTP_IDLE_TIMEOUT_SECONDS = "3600"
MCP_JUMPSERVER_GUI_SUCKS_SFTP_REAP_INTERVAL_SECONDS = "30"
MCP_JUMPSERVER_GUI_SUCKS_MAX_SFTP_SESSIONS = "4"

# Optional when the default state directory is not desired.
# MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR = "/Users/alice/Library/Application Support/mcp-jumpserver-gui-sucks"
# MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE = "/Users/alice/Library/Application Support/mcp-jumpserver-gui-sucks/auth-state.json"
# MCP_JUMPSERVER_GUI_SUCKS_ORG_ID = "00000000-0000-0000-0000-000000000002"
```

`tool_timeout_sec` is a Codex-side MCP client setting. If it is omitted, Codex falls back to its own default per-tool timeout. Increase it when the agent may need to keep a single `jms_*` call open for longer-running terminal work. For terminal commands, pair it with a larger `total_timeout_seconds` on the specific `jms_run_terminal_command` or `jms_execute_in_terminal_session` call when needed.

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
        "MCP_JUMPSERVER_GUI_SUCKS_USE_ENV_PROXY": "false",
        "MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS": "3600",
        "MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS": "30",
        "MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS": "8",
        "MCP_JUMPSERVER_GUI_SUCKS_SFTP_IDLE_TIMEOUT_SECONDS": "3600",
        "MCP_JUMPSERVER_GUI_SUCKS_SFTP_REAP_INTERVAL_SECONDS": "30",
        "MCP_JUMPSERVER_GUI_SUCKS_MAX_SFTP_SESSIONS": "4"
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
- `MCP_JUMPSERVER_GUI_SUCKS_USE_ENV_PROXY`
- `MCP_JUMPSERVER_GUI_SUCKS_LOG_LEVEL`
- `MCP_JUMPSERVER_GUI_SUCKS_REQUEST_TIMEOUT_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS`
- `MCP_JUMPSERVER_GUI_SUCKS_SFTP_IDLE_TIMEOUT_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_SFTP_REAP_INTERVAL_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_MAX_SFTP_SESSIONS`

The recommended minimum MCP config is usually:

- `MCP_JUMPSERVER_GUI_SUCKS_BASE_URL`
- optionally `MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR` or `MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE`

Environment and operating-system proxy discovery is disabled by default so private JumpServer
hosts are contacted directly. Set `MCP_JUMPSERVER_GUI_SUCKS_USE_ENV_PROXY=true` only when the
JumpServer deployment is intentionally reached through the configured system proxy.

## PyPI Release Automation

The repository now includes [publish-pypi.yml](.github/workflows/publish-pypi.yml).

Its behavior is intentionally:

- every push to `main` inspects `pyproject.toml`
- if the package version changed and that version does not already exist on PyPI, GitHub Actions builds and publishes it
- if the version did not change, the workflow skips publishing
- if the version already exists on PyPI, the workflow skips publishing
- `workflow_dispatch` can be used to publish the current version manually when it is not yet on PyPI
- after a successful PyPI publish, the workflow creates a matching `v<version>` GitHub Release with generated notes
- if PyPI already contains the current version but its GitHub Release is missing, the next workflow run backfills that Release without republishing the package

The publish job uses PyPI Trusted Publishing through GitHub OIDC. Configure PyPI to trust this repository and workflow before expecting the publish step to succeed.

Recommended PyPI trusted publisher settings:

- owner: `ArtiPyHeart`
- repository: `mcp-jumpserver-gui-sucks`
- workflow file: `.github/workflows/publish-pypi.yml`
- environment name: `pypi`

After Trusted Publishing is configured once, later pushes to `main` that bump `project.version` in `pyproject.toml` will publish to PyPI and create the matching GitHub Release automatically.

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
- `jms_sftp_usage_guide`
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
- `jms_acquire_terminal_session`
- `jms_list_terminal_sessions`
- `jms_send_terminal_input`
- `jms_read_terminal_output`
- `jms_run_terminal_command`
- `jms_get_terminal_identity`
- `jms_switch_terminal_user`
- `jms_exit_terminal_user`
- `jms_interrupt_terminal_session`
- `jms_resize_terminal_session`
- `jms_close_terminal_session`
- `jms_acquire_sftp_session`
- `jms_list_sftp_sessions`
- `jms_sftp_stat`
- `jms_sftp_list`
- `jms_sftp_mkdir`
- `jms_sftp_upload`
- `jms_sftp_download`
- `jms_sftp_rename`
- `jms_sftp_delete`
- `jms_close_sftp_session`

## Operational Notes

- Managed terminal sessions are process-local and intended to live only for the MCP server process lifetime.
- Managed SFTP sessions are process-local, reused by asset/account pair, idle-reaped, and closed when the MCP process exits.
- Active SFTP operations are excluded from idle reaping, including transfers longer than the configured idle timeout.
- Terminal output is bounded in memory. Large streams retain their beginning and end and report truncation metadata.
- `jms_sftp_upload` accepts regular files from any local path; there is no local source-directory allowlist.
- Never set SFTP deletion flags speculatively. `confirm=true` requires approval of the exact path, and `recursive=true` requires explicit approval of recursive directory deletion.
- `jms_terminal_usage_guide` returns the preferred terminal workflow for coding agents and should be consulted at the start of terminal-heavy work.
- `jms_acquire_terminal_session` is the preferred high-level entrypoint for repeated work on one machine because it resolves the target and reuses an existing shell when possible.
- `jms_run_terminal_command` is the preferred path for short command execution on an already acquired `session_handle`.
- `jms_get_terminal_identity` verifies the effective UID and username before privileged work.
- `jms_switch_terminal_user` supports passwordless and password-backed `sudo` or `su` transitions while preserving the same JumpServer session.
- `jms_exit_terminal_user` exits one MCP-managed user shell and verifies that the previous identity was restored.
- `jms_send_terminal_input` plus `jms_read_terminal_output` are the preferred path for shell-style interaction and incremental polling.
- `jms_interrupt_terminal_session` is the supported way to stop a running managed-session command without throwing away the shell immediately.
- `jms_interrupt_terminal_session` accepts `ctrl_c` and the common alias `SIGINT`; both normalize to the same Ctrl+C behavior.
- The default managed shell idle timeout is 1 hour. Override it with `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS` if a different retention window is required.
- When the MCP server process exits normally, it closes all managed KoKo shells before returning.
- `terminal-shell` is line-oriented, not a full raw TTY emulator.
- Terminal entrypoints preflight the cookie-backed web session before opening KoKo.
- If the cookie-backed session is already invalid, terminal calls fail early with an explicit re-login requirement instead of a low-level websocket failure.
- REST discovery can continue to work when the durable `access_key` remains valid, even if terminal access requires a fresh login.
