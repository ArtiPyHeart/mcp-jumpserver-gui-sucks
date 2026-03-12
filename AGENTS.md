# AGENTS.md

## Project Positioning

- This repository is a **JumpServer 443-only MCP client** project.
- The goal is to rebuild the practical entry points currently provided by the JumpServer 443 GUI so coding agents such as Codex can directly operate assets behind the bastion host.
- The first version aims to replace common Web UI workflows, not to recreate the original frontend pixel by pixel.
- The implementation must remain within JumpServer's existing authorization, auditing, command filtering, approval, and session control model.

## Language Policy

- All project documentation, code comments, inline explanations, and repository-facing written artifacts must be written in English.
- Chinese is only used when replying to the user in conversation.
- When updating existing project docs, prefer keeping them in English for consistency.

## Confirmed Facts

- The primary design and phased plan live in `mcp-jumpserver-gui-sucks-development-plan.md`.
- `extern/` stores upstream JumpServer open-source code for protocol, API, and implementation reference only.
- `extern/` is not tracked by Git in this repository unless the user explicitly asks otherwise.
- The public project and package name should stay aligned with `mcp-jumpserver-gui-sucks`.

## Implementation Constraints

- **443-only** is a hard constraint. Do not depend on port 2222.
- The first version uses a **Hybrid** architecture: REST/Core API first, KoKo WebSocket for interactive terminal coverage.
- The project must follow a **CLI-first** path. Avoid GUI or browser interaction in the normal workflow unless the CLI path has been technically verified to be insufficient.
- Authentication should prefer direct API and CLI-driven flows, including terminal-entered MFA codes, before any browser-assisted fallback is considered.
- All endpoints, fields, and protocol details must be verified against the target instance's `/api/docs/` and real traffic capture first. `extern/` is only auxiliary reference material.
- The first implementation phase should prioritize queries and non-interactive command execution. Interactive shell support comes later.

## Authentication Requirements

- Login design must account for username and password, MFA compatibility, and persistent authenticated state after login.
- The implementation must not assume username/password-only authentication is sufficient for real JumpServer deployments.
- MFA handling must be designed as a first-class part of the login flow rather than a later patch.
- After successful login, the MCP-side authentication state must be persisted and reusable across later MCP invocations until the session expires or is revoked.
- The preferred MFA UX is CLI-based code entry when the server-side protocol allows it.
- Persisted authenticated state must not be stored in `.env`.
- Runtime environment variables should describe how to locate persisted state, not embed live session secrets themselves.
- The preferred durable persisted credential is a JumpServer access key created through the CLI flow after MFA succeeds.
- Short-lived bearer tokens and session cookies are fallback material only and must not be treated as the final persistence strategy when a durable access key can be established.
- If the full login flow cannot be implemented cleanly as MCP tools, it should be implemented as a prerequisite CLI step that the user runs explicitly before using the MCP server.
- The default persistence target for authenticated state should be a user-scoped application state directory suitable for `uvx`, with an environment-variable override for advanced setups.
- Browser-assisted login is a last-resort fallback only after the CLI-first path has been proven insufficient for the target deployment.

## Development Environment

- Package management and development commands: `uv`
- Current development interpreter: `/Users/rabyte/github/mcp-jumpserver-gui-sucks/.venv/bin/python`
- Current `pyproject.toml` declaration: `Python >=3.12`
- Release target: runnable through `uvx`
- The public package name should remain `mcp-jumpserver-gui-sucks`
- Prefer a matching CLI entrypoint name unless a later compatibility reason requires a second alias

## Dependency Management

- If development work requires a Python library that is not installed, install it directly instead of stopping for approval.
- Any Python library installed for actual project development must also be added to `pyproject.toml`.

## Secrets and Local State

- Login-related settings live in `.env`, and that file must not be tracked by Git.
- Never write cookies, sessions, access keys, tokens, OTP values, or other secrets into the repository.
- Do not treat `.env` as the persistence layer for authenticated runtime state.
- Persisted auth state should be written to a user-scoped runtime state directory, outside the repository, and loaded by path or directory environment variables.
- If an example configuration is needed later, provide a sanitized `.env.example`.

## Observability and Safety

- Critical flows must include enough logging, error context, and targeted debugging information to keep the system observable.
- Logs must be sanitized by default and must never print secrets, cookies, tokens, or OTP values.
- Record meaningful state transitions, branch choices, failure reasons, and high-value server responses. Avoid noisy logs with little diagnostic value.

## Directory Conventions

- `extern/`: upstream reference code, not part of the tracked implementation
- `skills/`: default project-local skill directory for flexible runtime-selected skills
- `.agents/skills/`: project-local standardized Codex skills intended for manual activation
- `docs/`: notes about instance differences, packet capture, API mapping, login flow, and WebSocket flow
- `src/`: main implementation code, including the future MCP server and driver layers

## Initialization Policy

- During `/init`, keep the repository minimal and record only confirmed goals, constraints, environment details, and directory responsibilities.
- Do not create large amounts of placeholder code before dependencies, interfaces, and module boundaries are actually confirmed.
- This file is a living project manual and should be updated as important decisions, pitfalls, and conventions become clear.
