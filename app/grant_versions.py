"""Per-client/subject write revocation. No credentials or bearer tokens are persisted."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path


class GrantVersions:
    """A shared-volume SQLite counter; absent legacy grants have generation zero.

    Downgrading one connection increments its counter. Old tokens remain usable
    for reading, but cannot regain write permission through refresh/reconsent of
    a different token. Separate replicas require the same store (not local copies).
    """

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def key(client_id: str, subject: str) -> str:
        return hashlib.sha256(f"{client_id}\0{subject}".encode()).hexdigest()

    def current(self, client_id: str, subject: str) -> int:
        if not self.path.exists():
            return 0
        # A read never creates/repairs an empty or corrupt policy database.
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5) as db:
            row = db.execute("SELECT generation FROM grants WHERE id=?", (self.key(client_id, subject),)).fetchone()
        return int(row[0]) if row else 0

    def downgrade(self, client_id: str, subject: str) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, generation INTEGER NOT NULL)")
            key = self.key(client_id, subject)
            db.execute(
                "INSERT INTO grants VALUES (?,1) ON CONFLICT(id) DO UPDATE SET generation=generation+1", (key,)
            )
            generation = db.execute("SELECT generation FROM grants WHERE id=?", (key,)).fetchone()[0]
        return int(generation)
