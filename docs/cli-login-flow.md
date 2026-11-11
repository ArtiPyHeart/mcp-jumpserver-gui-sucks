# CLI Login Flow

Date: 2026-11-11

## Purpose

This project is intentionally CLI-first.

The normal operator flow should avoid browsers and other GUI steps unless the target deployment exposes an MFA mode that cannot be completed through code entry in the terminal.

## Primary Command

```bash
uv run mcp-jumpserver-gui-sucks login \
  --base-url https://jumpserver.example.com \
  --username alice
```

The command prompts for:

- password
- web-session login MFA code when required
- durable access-key confirmation code when required

### Supplying a Password from a Script

`login --password-stdin` reads exactly one password line from standard input.
It removes the line ending and preserves other leading and trailing whitespace.
Empty input fails before any login request; omitting the option keeps the hidden
interactive password prompt.

```bash
JUMPSERVER_PASSWORD='your-plaintext-password'
printf '%s\n' "$JUMPSERVER_PASSWORD" |
  uvx mcp-jumpserver-gui-sucks@0.4.1 login \
    --base-url https://jumpserver.example.com \
    --username alice \
    --password-stdin
```

MFA and captcha answers are still entered through the controlling terminal, not
the password pipe. If a challenge needs hidden input and no controlling terminal
is available, this mode fails instead of reading challenge answers from stdin.
The supplied password is not added to auth state, login results, or application
logs.

The optional [login script](../examples/login.sh) stores the password as plaintext
in a local shell variable and checks the persisted session before a fresh login.
Copy it outside the repository, fill in the URL, username, and password, and set
its permissions to `0700`. Use it with the state file belonging to that same
JumpServer instance and user. The script disables shell tracing before assigning
the password and does not export it or pass it as a process argument. Anyone
able to read the script can read its password; keep the populated copy out of Git.

### Scriptable Session Refresh

`refresh-session --force` has these exit statuses:

- `0`: the existing terminal web session was confirmed and saved.
- `3`: no auth state or terminal cookies exist, or JumpServer explicitly rejected
  the session with HTTP `401` or a JSON `ok: false` result. A fresh login is needed.
- `1`: another failure, including network, TLS, permission (`403`), server,
  malformed-response, or local state read/write errors. Report the error without
  automatically logging in again.
- `2`: invalid CLI arguments.
- `130`: interrupted by the operator.

The example script starts a fresh login only for exit status `3`. MFA remains
manual whenever JumpServer requires it. The [glossary](../GLOSSARY.md) defines
the distinction between session refresh, fresh login, and durable REST credentials.

## Implemented Authentication Strategy

1. Bootstrap a fresh session through `GET /core/auth/login/`.
2. Submit the same encrypted password form that the normal web login uses through `POST /core/auth/login/`.
3. Follow `GET /core/auth/login/guard/` until JumpServer either:
   - establishes the authenticated Django session directly, or
   - redirects to `GET /core/auth/login/mfa/`.
4. If MFA is required, let the operator choose a CLI-supported MFA backend and enter the code in the terminal, then submit it through the web MFA form.
5. Re-enter `GET /core/auth/login/guard/` so JumpServer can execute the final `auth_login()` step and create a real web session.
6. Verify that session through `GET /api/v1/authentication/user-session/`.
7. Reuse an existing durable access key when possible; otherwise discover the confirmation backend through `GET /api/v1/authentication/confirm/?confirm_type=password`.
8. Complete that confirmation step in the terminal.
9. Create an access key through `POST /api/v1/authentication/access-keys/`.
10. Persist the durable access key together with the authenticated cookie jar into the user-scoped auth-state file.

The key design change is that the CLI login must create a real web session first. An API-only bearer-token flow can create a durable access key, but it does not produce the KoKo-compatible cookie-backed session required by the `443-only` terminal path.

## Terminal Session Refresh Behavior

The repository now treats the persisted terminal web session as something that should be maintained while it is still valid, not recreated after it has already expired.

- `uv run mcp-jumpserver-gui-sucks refresh-session --force` explicitly performs a cookie-session keepalive check.
- KoKo-oriented terminal entrypoints also perform that check automatically before opening a terminal or running a one-shot command.
- If the persisted cookie-backed session is still valid, the runtime re-persists any refreshed cookie expiry returned by JumpServer.
- If the persisted cookie-backed session is already invalid, the runtime reports that the operator must re-run the CLI login flow.

This is an important boundary:

- durable `access_key` material keeps REST discovery usable
- it does not currently prove that JumpServer can recreate the web session needed by KoKo without credentials or MFA

## Why the Flow Does Not Stop at the API Token Path

The bearer-token API is still useful for diagnostics, but it is not the primary login path anymore for this project:

- bearer tokens are short-lived
- the API login path does not complete Django `auth_login()`
- KoKo terminal WebSocket access still depends on a browser-equivalent authenticated web session

The login command therefore treats these as the preferred combined outcome:

- an authenticated web session for KoKo
- a durable access key for REST discovery and token creation

## Cookie-Only Fallback

`login` can persist only the authenticated web-session cookies when the operator explicitly allows it:

```bash
uv run mcp-jumpserver-gui-sucks login \
  --base-url https://jumpserver.example.com \
  --username alice \
  --allow-ephemeral
```

This mode exists only as a fallback for environments where durable access keys are not available or cannot be confirmed in the current session.

## MFA Notes

CLI-supported MFA backends:

- `otp`
- `sms`
- `email`
- `otp_radius`
- `mfa_custom`

Known non-CLI backends:

- `passkey`
- `face`

If the target deployment requires one of the non-CLI backends, the project should report that clearly instead of pretending the CLI-first path is complete.

On the currently observed deployment, MFA can still be required twice in the same login command:

- once for the web-session login
- once for the durable access-key confirmation step

Operators should expect that those can be two different OTP codes if enough time passes between the prompts.
