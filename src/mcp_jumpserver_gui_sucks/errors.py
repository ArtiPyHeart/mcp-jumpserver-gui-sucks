class JumpServerMCPError(Exception):
    """Base exception for local runtime failures."""


class ConfigError(JumpServerMCPError):
    """Raised when runtime configuration is incomplete or invalid."""


class MissingAuthStateError(JumpServerMCPError):
    """Raised when a persisted auth state file is required but missing."""


class TargetResolutionError(JumpServerMCPError):
    """Raised when a user-facing asset or account reference cannot be resolved safely."""


class JumpServerAPIError(JumpServerMCPError):
    """Raised when a JumpServer HTTP call fails."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        detail: str | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        message = f"JumpServer API request failed: {method} {path} -> HTTP {status_code}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
