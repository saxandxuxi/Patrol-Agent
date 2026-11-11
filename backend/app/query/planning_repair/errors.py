from __future__ import annotations

from typing import Any


class RepairPatchError(ValueError):
    def __init__(
        self,
        message: str,
        patches: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(message)
        self.patches = patches
