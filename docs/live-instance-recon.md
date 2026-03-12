# Live Instance Recon

Date: 2026-03-12

## Scope

- Observed against `https://jumpserver.rabyte.cn` with a browser session that had already completed the normal human login flow.
- The goal of this note is to record instance-specific behavior before any client code is written.
- Cookies, token values, asset IPs, usernames, email addresses, UUIDs, and other sensitive values are intentionally redacted.

## Confirmed OpenAPI Facts

- `/api/docs` is available and renders the Swagger UI.
- `/api/docs?format=openapi` returns the instance OpenAPI document.
- Observed metadata:
  - title: `JumpServer API Docs`
  - version: `v1`
  - base path: `/api/v1`
- The instance currently exposes 132 documented paths.

## Authentication and Session Behavior

- The logged-in Web UI behaves as a cookie-backed same-origin client.
- The browser-visible cookie set includes a CSRF cookie, an organization cookie, and a public-key-related cookie.
- The effective session cookie is HttpOnly and is sent on API requests, even though it is not readable through `document.cookie`.
- API requests from the logged-in UI include:
  - browser session cookies
  - `X-CSRFToken`
  - `X-JMS-ORG`
- `GET /api/v1/authentication/user-session/` returned `{"ok": true}` for the active session and refreshed the session cookie. This is the current best candidate for an MCP `status` or `health` probe.
- `GET /api/v1/users/profile/` returned MFA-related flags on the observed account, including that MFA was enabled and enforced. This confirms that the project cannot assume a username/password-only deployment.
- A pure CLI API-first login flow succeeded against the live instance with:
  - username and password
  - one MFA code for the token-login step
  - one MFA code for the confirmation step required before access-key creation
- `POST /api/v1/authentication/auth/` returned `HTTP 201 Created` when issuing the bearer token on this deployment. The client must not assume `HTTP 200` only.
- That API-first flow did create a durable access key that was sufficient for later REST calls without reusing browser cookies.
- However, the cookie jar captured from that API-first flow did not authenticate:
  - `GET /api/v1/authentication/user-session/`
  - `GET /api/v1/users/profile/`
- Separate HTTP form probes confirmed the web login redirect chain:
  - `POST /core/auth/login/` -> `302 /core/auth/login/guard/`
  - `GET /core/auth/login/guard/` -> `302 /core/auth/login/mfa/`
- Upstream source inspection showed that `GET /core/auth/login/guard/` is the step that calls Django `auth_login()` and turns the partially authenticated session into a real browser-equivalent web session.
- Browser-backed session auth was also confirmed to work directly against:
  - `GET /api/v1/authentication/confirm/?confirm_type=password`
  - `GET /api/v1/authentication/access-keys/`
- A later CLI-emulated web-session login completed successfully and persisted a cookie jar that now passes:
  - `GET /api/v1/authentication/user-session/`
  - KoKo terminal websocket handshake under `/koko/ws/terminal/`
- A later one-shot CLI terminal execution against the same SSH asset also succeeded:
  - websocket connected through KoKo
  - the remote command output was captured from websocket binary frames
  - the remote exit status was recovered from the shell transcript
- A later managed multi-turn terminal-session prototype also succeeded against the same SSH asset:
  - a process-local session handle remained reusable across multiple reads and writes
  - `TERMINAL_RESIZE` worked during the same live session
  - the session closed cleanly after sending `exit`
  - the cleaned `stdout_text` view could remove leading echoed command input while `output_text` preserved fuller shell context
  - later lifecycle validation also confirmed that idle sessions can be reaped automatically and that reads on expired handles fail explicitly
- `GET /api/v1/authentication/user-session/` is not a reliable health probe under access-key auth on this instance. `GET /api/v1/users/profile/` is the better general-purpose authenticated probe.

## Relevant Authentication Endpoints Found in OpenAPI

- `POST /api/v1/authentication/auth/`
- `GET /api/v1/authentication/user-session/`
- `POST /api/v1/authentication/mfa/challenge/`
- `POST /api/v1/authentication/mfa/select/`
- `POST /api/v1/authentication/mfa/send-code/`
- `POST /api/v1/authentication/mfa/verify/`
- `POST /api/v1/authentication/connection-token/`
- `POST /api/v1/authentication/connection-token/exchange/`
- `GET /api/v1/authentication/connection-token/{id}/client-url/`
- `GET /api/v1/authentication/access-keys/`

## Observed OpenAPI Body Shapes Worth Preserving

- `POST /api/v1/authentication/auth/` uses a `BearerToken` schema with at least:
  - `username`
  - `password`
  - `public_key`
- `POST /api/v1/authentication/mfa/challenge/` and `POST /api/v1/authentication/mfa/verify/` use a schema that requires:
  - `code`
  - optional `type`
- `POST /api/v1/authentication/mfa/select/` and `POST /api/v1/authentication/mfa/send-code/` use a schema that requires:
  - `type`
  - optional `username`

## Real Asset Discovery Flow Used by the Web UI

The observed workbench asset pages did not primarily use `/assets/my-asset/` for list and detail views.

The practical flow was:

- `GET /api/v1/perms/users/self/nodes/children/tree/`
- `GET /api/v1/assets/favorite-assets/`
- `GET /api/v1/perms/users/self/assets/?asset=&node=&offset=0&limit=15&display=1&draw=1`
- `GET /api/v1/perms/users/self/assets/{asset_id}/`
- `GET /api/v1/terminal/components/connect-methods/`
- `POST /api/v1/authentication/connection-token/`
- `PATCH /api/v1/authentication/connection-token/{token_id}/expire/`

The asset detail response included:

- `permed_protocols`
- `permed_accounts`
- per-account action capabilities such as connect, file transfer, copy, paste, and delete permissions

The node tree response included:

- a GUI-facing tree key such as `1:9`
- a real node UUID in `meta.data.id`
- human-readable names with asset counts baked into the display title

The observed `node` query parameter behavior for asset listing is still not fully pinned down on this instance. Both GUI-style tree keys and UUID-shaped values returned the same asset set in the limited sample checked so far, so filtering semantics should be treated as provisional until more node shapes are tested.

## Connection Token Findings

- `POST /api/v1/authentication/connection-token/` worked under durable access-key authentication.
- For the tested Linux asset, the minimal successful payload was:
  - `asset`: the asset UUID
  - `account`: the permed account UUID-like alias, not the human-readable account name
  - `protocol`: `ssh`
  - `connect_method`: `web_cli`
- The response included a short-lived token value plus enough metadata to describe the target asset, connect method, allowed actions, and expiration window.
- Token values must be treated as secrets and should be redacted from normal MCP responses.
- `PATCH /api/v1/authentication/connection-token/{token_id}/expire/` returned `204 No Content` and successfully invalidated the created token.
- The KoKo web-terminal path did not use the token secret value in the browser URL. The observed web path used the connection-token object ID.
- A direct CLI WebSocket probe to `/koko/ws/terminal/` showed:
  - `token=<connection-token-id>` without cookies was rejected
  - `token=<connection-token-value>` without cookies was also rejected
  - `token=<connection-token-id>` plus a valid session-cookie header and `Origin` succeeded
- A later probe using the cookie jar persisted from the current CLI login flow still failed both:
  - `GET /api/v1/authentication/user-session/`
  - `GET /api/v1/users/profile/`
- After switching to the CLI-emulated web login form flow, those failures disappeared and KoKo probing succeeded.
- The current confirmed requirement set is:
  - a short-lived connection token
  - a valid authenticated web-session cookie
- The current implementation direction should therefore be:
  - use a CLI-emulated web login form flow to establish the web session
  - reuse or create a durable access key inside that authenticated session
  - keep both credential layers in the persisted state
  - use KoKo websocket sessions as the fallback command-execution path when `ops/adhoc` is disabled

## Command Execution Findings

- `GET /api/v1/ops/adhocs/` was reachable.
- `GET /api/v1/ops/jobs/` returned `403` with `Command execution disabled`.
- `OPTIONS /api/v1/ops/job-executions/` also returned `403` with `Command execution disabled`.
- This means the current deployment exposes some `ops` metadata but has the actual command-execution path disabled at the server-policy level.
- The MCP roadmap for this deployment should therefore prioritize terminal connection coverage ahead of adhoc execution tooling.

## Implications for the MCP Client

- The primary discovery client should target the `perms/users/self/...` endpoints, because that is what the current GUI actually uses for user-visible asset selection.
- `/assets/my-asset/` should be treated as a secondary path or an explicitly justified feature, not the default list API.
- CLI login can bootstrap the same RSA/CSRF/session cookies without opening a browser, so GUI dependence is not required for password encryption.
- Bearer tokens alone are not sufficient for durable persistence because they are short-lived by design.
- Access keys should be treated as the preferred durable REST credential once CLI login and MFA have completed.
- The main login implementation should be web-session-first, not bearer-token-first, because the terminal path depends on a real authenticated web session.
- A CLI-derived web session is now sufficient to complete the KoKo websocket handshake on this instance.
- A CLI-derived web session is also sufficient to execute a one-shot SSH shell command through KoKo on this instance.
- A CLI-derived web session is also sufficient to keep a multi-turn KoKo shell session alive inside a resident MCP server process on this instance.
- The first useful MCP surface can be built on top of:
  - session status
  - current profile
  - node tree
  - asset search
  - asset detail

## Open Questions

- Whether all connection-token and terminal APIs behave correctly under access-key authentication on this specific instance.
- How long the authenticated web session remains reusable when `auto_login` is enabled, and whether it can be refreshed without prompting for credentials again.
