"""答案缓存：同一道题只花一次 token。用 sqlite，重启也在。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time

from .parsing import parse_options


class AnswerCache:
    def __init__(self, path: str, ttl_hours: float = 168) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.ttl = max(0.0, float(ttl_hours)) * 3600
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS answers (
                key      TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer   TEXT NOT NULL,
                provider TEXT,
                created  REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def make_key(qtype: str, title: str, options_raw) -> str:
        options = parse_options(options_raw)
        raw = f"{qtype}\x00{title.strip()}\x00{'|'.join(options)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT answer, provider, created FROM answers WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            answer, provider, created = row
            if self.ttl and (time.time() - created) > self.ttl:
                self._conn.execute("DELETE FROM answers WHERE key = ?", (key,))
                self._conn.commit()
                return None
            return {"answer": answer, "provider": provider, "created": created}

    def set(self, key: str, question: str, answer: str, provider: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO answers (key, question, answer, provider, created) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, question[:500], answer, provider, time.time()),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM answers").fetchone()
            return int(row[0]) if row else 0

    def clear(self) -> int:
        with self._lock:
            total = self.count()
            self._conn.execute("DELETE FROM answers")
            self._conn.commit()
            return total

    def close(self) -> None:
        with self._lock:
            self._conn.close()
