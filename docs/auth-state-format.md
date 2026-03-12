# Auth State Format

Date: 2026-03-12

## Purpose

This project does not persist authenticated runtime state in `.env`.

Instead, the MCP runtime loads a JSON auth-state file from a user-scoped state directory. This makes the setup compatible with `uvx`, MFA-capable login helpers, and local development without storing live session secrets inside the repository.

## Resolution Rules

The runtime currently resolves state using these environment variables:

- `MCP_JUMPSERVER_GUI_SUCKS_BASE_URL`
- `MCP_JUMPSERVER_GUI_SUCKS_ORG_ID`
- `MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR`
- `MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE`
- `MCP_JUMPSERVER_GUI_SUCKS_LOG_LEVEL`
- `MCP_JUMPSERVER_GUI_SUCKS_VERIFY_TLS`
- `MCP_JUMPSERVER_GUI_SUCKS_REQUEST_TIMEOUT_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_IDLE_TIMEOUT_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_TERMINAL_REAP_INTERVAL_SECONDS`
- `MCP_JUMPSERVER_GUI_SUCKS_MAX_TERMINAL_SESSIONS`

If neither `MCP_JUMPSERVER_GUI_SUCKS_STATE_DIR` nor `MCP_JUMPSERVER_GUI_SUCKS_STATE_FILE` is set, the default state file path is:

```text
<platformdirs user state dir>/mcp-jumpserver-gui-sucks/auth-state.json
```

On the current macOS development machine this resolves to:

```text
/Users/rabyte/Library/Application Support/mcp-jumpserver-gui-sucks/auth-state.json
```

## JSON Shape

The current schema supports both short-lived and durable materials:

```json
{
  "schema_version": 2,
  "base_url": "https://jumpserver.example.com",
  "login_source": "cli-mfa-access-key",
  "headers": {
    "X-JMS-ORG": "<redacted>"
  },
  "cookies": [],
  "bearer_token": "",
  "bearer_keyword": "Bearer",
  "bearer_expires_at": "",
  "access_key_id": "<redacted>",
  "access_key_secret": "<redacted>",
  "metadata": {
    "saved_by": "login",
    "durable_auth": true
  },
  "created_at": "2026-03-12T03:00:00+00:00",
  "updated_at": "2026-03-12T03:00:00+00:00"
}
```

## Preferred Persistence Strategy

The preferred persisted material is:

- `X-JMS-ORG`
- `access_key_id`
- `access_key_secret`

This avoids depending on short-lived browser-like session cookies after login has completed.

However, the current live deployment still requires a valid cookie-backed web session for KoKo terminal WebSocket access, so the auth-state file intentionally persists both:

- a durable access key for REST discovery
- a cookie-backed web session for terminal entrypoints

## Ephemeral Fallback Material

When durable access-key setup is not possible and the operator explicitly accepts a short-lived state, the runtime can also persist:

- `jms_sessionid`
- `jms_csrftoken`
- `bearer_token`
- `bearer_expires_at`

The runtime automatically mirrors `jms_csrftoken` into the `X-CSRFToken` request header when cookie-mode requests are used.

The runtime also tracks the `jms_sessionid` cookie expiry when the server returns one, so terminal-oriented commands can:

- estimate whether the web session is nearing expiry
- refresh the cookie-backed session through `GET /api/v1/authentication/user-session/` while it is still valid
- report an explicit re-login requirement when the persisted terminal session is no longer usable

## Current Producers

Today the repository provides one explicit producer:

- `uv run mcp-jumpserver-gui-sucks login --base-url ...`
- `uv run mcp-jumpserver-gui-sucks save-state ...`

`save-state` is mainly a bootstrap and debugging aid. The long-term MFA-aware path should prefer `login`.

## CLI Login Expectations

The CLI login helper should:

1. bootstrap a fresh JumpServer login session through `/core/auth/login/`
2. authenticate against `/api/v1/authentication/auth/` with the normal password flow expected by that endpoint
3. complete MFA in the terminal when the server requires it
4. obtain a short-lived bearer token for follow-up confirmation calls
5. encrypt the password like Luna only for later endpoints that use encrypted secret fields
6. upgrade the result into a durable access key whenever possible
7. write the resulting auth-state JSON into the resolved user-scoped state file
8. avoid leaking passwords, bearer tokens, access-key secrets, or MFA codes into logs
