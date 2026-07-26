from __future__ import annotations

import csv
import hashlib
import hmac
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from app.config import Settings

SAFE_FILENAME = re.compile(r"^[a-f0-9]{32}--[A-Za-z0-9_.-]+\.csv$")


@dataclass(frozen=True)
class ExportedFile:
    path: Path
    filename: str
    size_bytes: int
    expires_at: int
    download_url: str


class ExportStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._signing_key = settings.export_signing_key or secrets.token_urlsafe(32)
        self.root = settings.export_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save_csv(
        self,
        *,
        rows: list[dict[str, Any]],
        fields: list[str],
        robot_id: str,
        portfolio: str,
    ) -> ExportedFile:
        self.cleanup_expired()
        human_name = self._safe_component(f"{robot_id}_{portfolio}")
        filename = f"{uuid.uuid4().hex}--{human_name}.csv"
        path = self.root / filename

        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=["timestamp", *fields])
            writer.writeheader()
            writer.writerows(rows)

        expires_at = int(time.time()) + self.settings.export_ttl_seconds
        return ExportedFile(
            path=path,
            filename=filename,
            size_bytes=path.stat().st_size,
            expires_at=expires_at,
            download_url=self._download_url(filename, expires_at),
        )

    def resolve_download(self, filename: str, expires_at: int, signature: str) -> Path | None:
        if not SAFE_FILENAME.fullmatch(filename):
            return None
        if expires_at < int(time.time()):
            return None
        expected = self._signature(filename, expires_at)
        if not hmac.compare_digest(expected, signature):
            return None
        path = (self.root / filename).resolve()
        if path.parent != self.root or not path.is_file():
            return None
        return path

    def cleanup_expired(self) -> None:
        cutoff = time.time() - self.settings.export_ttl_seconds
        for path in self.root.glob("*.csv"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue

    def _download_url(self, filename: str, expires_at: int) -> str:
        query = urlencode(
            {
                "expires": expires_at,
                "sig": self._signature(filename, expires_at),
            }
        )
        return f"{self.settings.resolved_public_base_url}/downloads/{filename}?{query}"

    def _signature(self, filename: str, expires_at: int) -> str:
        message = f"{filename}:{expires_at}".encode()
        return hmac.new(
            self._signing_key.encode(),
            message,
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _safe_component(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return cleaned[:80] or "portfolio"
