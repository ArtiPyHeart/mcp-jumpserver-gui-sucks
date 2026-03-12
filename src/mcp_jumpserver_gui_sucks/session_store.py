from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .auth_state import AuthState


class SessionStore:
    def __init__(self, state_file: Path) -> None:
        self._state_file = state_file

    @property
    def path(self) -> Path:
        return self._state_file

    def exists(self) -> bool:
        return self._state_file.is_file()

    def load(self) -> AuthState | None:
        if not self.exists():
            return None
        raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        return AuthState.from_dict(raw)

    def save(self, auth_state: AuthState) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = auth_state.to_dict()
        payload["updated_at"] = datetime.now(tz=UTC).isoformat()

        tmp_path = self._state_file.with_suffix(f"{self._state_file.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(self._state_file)
        os.chmod(self._state_file, 0o600)

    def describe(self) -> dict[str, Any]:
        description: dict[str, Any] = {
            "exists": self.exists(),
            "state_file": str(self._state_file),
        }
        if not self.exists():
            return description

        stat = self._state_file.stat()
        description.update(
            {
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime,
                    tz=UTC,
                ).isoformat(),
                "size_bytes": stat.st_size,
            }
        )
        return description

    def clear(self) -> bool:
        if not self.exists():
            return False
        self._state_file.unlink()
        return True
