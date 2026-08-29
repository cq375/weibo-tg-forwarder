from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import WeiboPost


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                post_id TEXT PRIMARY KEY,
                uid TEXT NOT NULL,
                author TEXT NOT NULL,
                post_url TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                telegram_message_ids TEXT,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_uid ON posts(uid, post_id)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS health_state (
                key TEXT PRIMARY KEY,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                alert_active INTEGER NOT NULL DEFAULT 0,
                last_alert_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def has_any(self, uid: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM posts WHERE uid = ? LIMIT 1", (uid,)
        ).fetchone()
        return row is not None

    def contains(self, post_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM posts WHERE post_id = ? LIMIT 1", (post_id,)
        ).fetchone()
        return row is not None

    def status(self, post_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        return str(row["status"]) if row else None

    def mark_seeded(self, posts: list[WeiboPost]) -> None:
        now = self._now()
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO posts (
                post_id, uid, author, post_url, status, discovered_at, updated_at
            ) VALUES (?, ?, ?, ?, 'seeded', ?, ?)
            """,
            [(post.post_id, post.uid, post.author, post.url, now, now) for post in posts],
        )
        self.connection.commit()

    def mark_processing(self, post: WeiboPost) -> None:
        now = self._now()
        self.connection.execute(
            """
            INSERT INTO posts (
                post_id, uid, author, post_url, status, attempts,
                discovered_at, updated_at
            ) VALUES (?, ?, ?, ?, 'processing', 1, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                status = 'processing',
                attempts = posts.attempts + 1,
                error = NULL,
                updated_at = excluded.updated_at
            """,
            (post.post_id, post.uid, post.author, post.url, now, now),
        )
        self.connection.commit()

    def mark_sent(self, post_id: str, message_ids: list[int]) -> None:
        now = self._now()
        serialized = ",".join(str(item) for item in message_ids)
        self.connection.execute(
            """
            UPDATE posts
            SET status = 'sent', telegram_message_ids = ?, error = NULL, updated_at = ?
            WHERE post_id = ?
            """,
            (serialized, now, post_id),
        )
        self.connection.commit()

    def mark_failed(self, post_id: str, error: str) -> None:
        now = self._now()
        self.connection.execute(
            "UPDATE posts SET status = 'failed', error = ?, updated_at = ? WHERE post_id = ?",
            (error[:2000], now, post_id),
        )
        self.connection.commit()

    def mark_delivery_unconfirmed(self, post_id: str, error: str) -> None:
        now = self._now()
        self.connection.execute(
            """
            UPDATE posts
            SET status = 'delivery_unconfirmed', error = ?, updated_at = ?
            WHERE post_id = ?
            """,
            (error[:2000], now, post_id),
        )
        self.connection.commit()

    def record_health_failure(self, key: str, error: str) -> sqlite3.Row:
        now = self._now()
        self.connection.execute(
            """
            INSERT INTO health_state (
                key, consecutive_failures, alert_active, last_error, updated_at
            ) VALUES (?, 1, 0, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                consecutive_failures = health_state.consecutive_failures + 1,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (key, error[:4000], now),
        )
        self.connection.commit()
        return self.get_health_state(key)

    def get_health_state(self, key: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM health_state WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            return row
        now = self._now()
        self.connection.execute(
            """
            INSERT INTO health_state (
                key, consecutive_failures, alert_active, updated_at
            ) VALUES (?, 0, 0, ?)
            """,
            (key, now),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM health_state WHERE key = ?", (key,)
        ).fetchone()
        assert row is not None
        return row

    def mark_health_alerted(self, key: str) -> None:
        now = self._now()
        self.connection.execute(
            """
            INSERT INTO health_state (
                key, consecutive_failures, alert_active, last_alert_at, updated_at
            ) VALUES (?, 0, 1, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                alert_active = 1,
                last_alert_at = excluded.last_alert_at,
                updated_at = excluded.updated_at
            """,
            (key, now, now),
        )
        self.connection.commit()

    def reset_health(self, key: str) -> None:
        now = self._now()
        self.connection.execute(
            """
            UPDATE health_state
            SET consecutive_failures = 0, alert_active = 0,
                last_error = NULL, updated_at = ?
            WHERE key = ?
            """,
            (now, key),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
