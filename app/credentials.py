from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class VikingCredentials:
    email: str
    api_key: str
    role: str

    @property
    def fingerprint(self) -> str:
        value = f"{self.email}\0{self.role}\0{self.api_key}".encode()
        return hashlib.sha256(value).hexdigest()
