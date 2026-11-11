#!/bin/bash
set +x
set -euo pipefail

# Keep this local copy private: it contains a plaintext login password.
unset JUMPSERVER_PASSWORD
JUMPSERVER_URL='https://jumpserver.example.com'
JUMPSERVER_USERNAME='alice'
JUMPSERVER_PASSWORD='replace-with-your-password'

if uvx mcp-jumpserver-gui-sucks@0.4.2 refresh-session --force; then
    exit 0
else
    refresh_status=$?
fi

# Only missing or invalid authentication should start a new login.
if [[ "$refresh_status" -ne 3 ]]; then
    exit "$refresh_status"
fi

printf '%s\n' "$JUMPSERVER_PASSWORD" |
    uvx mcp-jumpserver-gui-sucks@0.4.2 login \
        --base-url "$JUMPSERVER_URL" \
        --username "$JUMPSERVER_USERNAME" \
        --password-stdin
