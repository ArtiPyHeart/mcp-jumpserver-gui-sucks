# Web Terminal Flow

Date: 2026-03-12

## Scope

- Observed on the live instance through the existing Luna web flow.
- Sensitive values are redacted. Request bodies and paths are preserved only in sanitized form.

## Confirmed Connect Flow

1. The asset connect action opened Luna with a URL shaped like:

   ```text
   /luna/?login_to=<asset-id>&oid=<org-id>
   ```

2. Luna bootstrapped with several API calls, including:
   - `GET /api/v1/terminal/components/connect-methods/`
   - `GET /api/v1/authentication/user-session/`
   - `GET /api/v1/ops/adhocs/?only_mine=true`
   - `GET /api/v1/perms/users/self/assets/tree/?id=<asset-id>`
   - `GET /api/v1/perms/users/self/assets/<asset-id>/`
   - `GET /api/v1/assets/favorite-assets/`
   - `GET /api/v1/perms/users/self/nodes/children-with-assets/tree/`

3. The connect dialog allowed choosing a protocol and a connection method.

4. For the observed SSH web-terminal path, Luna created a connection token with:

   ```json
   {
     "asset": "<asset-id>",
     "account": "<account-id>",
     "protocol": "ssh",
     "input_username": "<resolved-username>",
     "input_secret": "",
     "connect_method": "web_cli",
     "connect_options": {
       "charset": "default",
       "disableautohash": false,
       "resolution": "auto",
       "backspaceAsCtrlH": false,
       "appletConnectMethod": "web",
       "virtualappConnectMethod": "web",
       "reusable": false,
       "rdp_connection_speed": "auto"
     }
   }
   ```

5. The observed API call sequence after clicking `CONNECT` was:
   - `POST /api/v1/authentication/connection-token/`
   - `GET /api/v1/terminal/endpoints/smart/?protocol=https&token=<connection-token-id>`
   - load iframe: `/koko/connect/?disableautohash=false&token=<connection-token-id>&_=<timestamp>`

6. The iframe then rendered the actual shell session and the terminal connected successfully.

7. A later CLI-side probe against `GET /api/v1/authentication/connection-token/{id}/client-url/` showed that the returned `jms://...` payload still described an endpoint on port `2222` even when the token had been created for `connect_method=web_cli`.

This is a critical distinction:

- `client-url` is useful as a protocol reference sample.
- `client-url` is not sufficient as the final implementation path for this repository, because the project is explicitly `443-only`.
- The real target path for interactive terminal coverage remains the KoKo web route and its WebSocket flow under the HTTPS entrypoint.

8. A direct probe inside the live KoKo iframe confirmed that the browser eventually opens a WebSocket with:

   ```text
   wss://jumpserver.rabyte.cn/koko/ws/terminal/?disableautohash=false&token=<connection-token-id>&...
   ```

   and the WebSocket subprotocol:

   ```text
   JMS-KOKO
   ```

9. The first observed client frame was a JSON message shaped like:

   ```json
   {
     "id": "<terminal-id>",
     "type": "TERMINAL_INIT",
     "data": "{\"cols\":80,\"rows\":24,\"code\":\"\"}"
   }
   ```

10. The first observed server frames included:

- `CONNECT`
- `TERMINAL_SHARE_USERS`
- `TERMINAL_SESSION`
- `TERMINAL_SHARE_JOIN`
- binary terminal payload frames

11. A CLI-side Python probe established these constraints:

- `token=<connection-token-id>` without browser session cookies was rejected during the WebSocket handshake.
- `token=<connection-token-value>` without browser session cookies was also rejected.
- `token=<connection-token-id>` plus a valid session-cookie header and `Origin: https://jumpserver.rabyte.cn` succeeded.
- A later probe using the cookie jar captured from the current CLI login flow still failed, even though the persisted state contained `jms_sessionid`.

This means the current implementation hypothesis is:

- the HTTPS KoKo terminal path is genuinely `443-only`
- the WebSocket handshake still depends on an authenticated web-session cookie, not only on the short-lived connection token
- durable `access_key` auth is enough for REST discovery and token creation, but not enough by itself for the terminal WebSocket
- the current CLI login flow captures cookie material, but that cookie jar is not yet equivalent to a browser-authenticated Luna/KoKo session
- upstream source inspection and direct HTTP form probing now point to the missing step:
  - `/core/auth/login/guard/` performs Django `auth_login()`
  - API-only token login does not
  - the correct CLI implementation target is therefore a web-session-first login flow that emulates the HTTP form redirects without opening a browser

12. After switching the login implementation to that web-session-first flow, a dedicated CLI `koko-probe` command successfully completed the KoKo websocket handshake with the persisted auth state:

- `ws_connected = true`
- agreed websocket subprotocol: `JMS-KOKO`
- first observed server message type: `CONNECT`
- the short-lived connection token was consumed during the process, so an explicit later expire call returned the equivalent of `already consumed`

13. A later CLI `terminal-exec` command also succeeded against the same SSH asset:

- it opened the KoKo websocket with the persisted authenticated cookie jar
- it sent `TERMINAL_DATA` frames through the SSH/Web CLI path
- it captured remote shell output from websocket binary frames
- it recovered the remote exit status from a marker-based shell transcript

This means the current repository now has a verified path for:

- interactive-session preparation
- websocket handshake diagnostics
- one-shot shell command execution through KoKo

14. Browser-backed session auth was further confirmed to reach session-protected REST endpoints directly:

- `GET /api/v1/users/profile/`
- `GET /api/v1/authentication/confirm/?confirm_type=password`
- `GET /api/v1/authentication/access-keys/`

This means a single authenticated web session should be able to support both:

- KoKo WebSocket access
- durable access-key creation after the required confirmation step

15. A later MCP-side managed terminal-session prototype also succeeded against the same SSH asset. The verified lifecycle was:

- open a KoKo terminal session
- write terminal input in multiple rounds
- read terminal output after each round
- send `TERMINAL_RESIZE`
- close the session explicitly or by sending `exit`

16. The managed-session output model now deliberately keeps two text views:

- `output_text`: a fuller cleaned transcript that still preserves prompt and echoed input context
- `stdout_text`: a more agent-friendly view that strips ANSI noise, shell-prompt prefixes, and leading echoed input captured from prior writes

17. The current managed-session lifetime is process-local:

- it is suitable for an MCP server that stays resident
- it is not meant to survive across unrelated standalone CLI process executions

18. The managed terminal-session layer now has explicit lifecycle controls:

- idle sessions are reaped automatically after a configurable timeout
- the manager enforces a configurable maximum number of concurrent sessions
- once a session has been reaped, later reads and writes fail with an explicit expired-session error instead of reusing a stale handle

## Confirmed Connect Methods

Observed from `GET /api/v1/terminal/components/connect-methods/`:

- `ssh`
  - `web_cli`
  - `ssh_client`
  - `ssh_guide`
- `sftp`
  - `web_sftp`
  - `sftp_client`

## Relevant OpenAPI Schema Fact

`POST /api/v1/authentication/connection-token/` expects a `ConnectionToken` schema whose required fields include:

- `account`
- `connect_method`

The schema also documents fields such as:

- `asset`
- `protocol`
- `input_username`
- `input_secret`
- `connect_options`

## Frontend Bundle Evidence

The live Luna and KoKo bundles exposed several useful strings:

- shell websocket code builds a URL shaped like:

  ```text
  /koko/ws/token/?...
  ```

- SFTP/file-management websocket code builds a URL shaped like:

  ```text
  /koko/ws/sftp/?token=<connection-token>
  ```

- A websocket subprotocol string `JMS-KOKO` appears in the file-management path.
- Frontend helpers construct JSON message envelopes shaped like:

  ```json
  {"id": "<id>", "type": "<message-type>", "data": "<payload>"}
  ```

These points are strong implementation clues, but they are not yet a complete protocol specification.

## What Is Still Unknown

- The full message taxonomy for terminal stdin, resize, heartbeat, close, and reconnect behavior.
- Whether `terminal/endpoints/smart/` is mandatory for all deployment topologies, or only for some gateway layouts.

## Current Implementation Guidance

- Treat interactive terminal support as a second-stage feature, even though the connect chain is now partially mapped.
- Build and verify the REST and session layers first.
- Treat both the top-level connection-token `value` field and the nested `token.value` field as secrets that must be redacted from normal logs and MCP responses.
- Preserve the authenticated web-session cookies when a successful login also creates a durable access key, because those cookies are required later for KoKo WebSocket handshakes.
- Prefer a CLI-emulated web login flow over the bearer-token API for terminal-oriented authentication work.
- Keep a dedicated diagnostic path to capture the KoKo websocket handshake and frame shapes with an already authenticated CLI-derived session.
- Treat `PATCH .../connection-token/{id}/expire/ -> 404` after a successful websocket handshake as an expected cleanup outcome, because the token may already have been consumed.
- For one-shot command execution, prefer returning both:
  - a raw transcript for debugging
  - a cleaned text view with ANSI/prompt noise reduced for agent consumption
- For managed multi-turn terminal sessions, preserve both:
  - a fuller terminal transcript for debugging and prompt-awareness
  - a cleaned stdout-oriented text field for agent reasoning after each read step
- Keep terminal-session lifecycle configurable through environment variables rather than hard-coding timeout and concurrency limits.
