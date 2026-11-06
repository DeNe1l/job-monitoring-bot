"""SQLite-хранилище: какие вакансии уже видели, о каких уведомили, какие закрылись."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from models import Vacancy, iso_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    key         TEXT PRIMARY KEY,
    dupe_hash   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    company     TEXT DEFAULT '',
    location    TEXT DEFAULT '',
    url         TEXT NOT NULL,
    snippet     TEXT DEFAULT '',
    track       TEXT NOT NULL,
    published   TEXT,
    first_seen  TEXT NOT NULL,
    last_check  TEXT,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | closed | expired
    closed_hits INTEGER NOT NULL DEFAULT 0,
    notified    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vac_dupe ON vacancies(dupe_hash);
CREATE TABLE IF NOT EXISTS source_health (
    source     TEXT PRIMARY KEY,
    fails      INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


class Storage:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---------- вакансии ----------
    def is_empty(self) -> bool:
        return self.conn.execute("SELECT 1 FROM vacancies LIMIT 1").fetchone() is None

    def find(self, key: str, dupe_hash: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM vacancies WHERE key = ? OR dupe_hash = ? LIMIT 1", (key, dupe_hash)
        ).fetchone()

    def get(self, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM vacancies WHERE key = ?", (key,)).fetchone()

    def add(self, v: Vacancy, now: datetime) -> None:
        snippet = v.text[:300] if v.kind == "telegram" else ""
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO vacancies
                   (key, dupe_hash, kind, source, title, company, location, url, snippet,
                    track, published, first_seen, last_check)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (v.key, v.dupe_hash, v.kind, v.source, v.title, v.company, v.location, v.url,
                 snippet, v.track, iso_utc(v.published) if v.published else None,
                 iso_utc(now), iso_utc(now)),
            )

    def pending(self) -> List[sqlite3.Row]:
        """Найденные, но ещё не отправленные пользователю (например, Telegram был недоступен)."""
        return self.conn.execute(
            "SELECT * FROM vacancies WHERE notified = 0 AND status = 'active' ORDER BY first_seen, key"
        ).fetchall()

    def mark_notified(self, keys: Iterable[str]) -> None:
        with self.conn:
            self.conn.executemany("UPDATE vacancies SET notified = 1 WHERE key = ?", [(k,) for k in keys])

    def mark_closed(self, key: str) -> Optional[sqlite3.Row]:
        """Помечает вакансию закрытой. Возвращает её строку, если статус реально поменялся."""
        row = self.get(key)
        if not row or row["status"] != "active":
            return None
        with self.conn:
            self.conn.execute("UPDATE vacancies SET status = 'closed' WHERE key = ?", (key,))
        return row

    def expire_old(self, cutoff: datetime) -> int:
        """Посты Telegram старше cutoff перестаём считать актуальными."""
        with self.conn:
            cur = self.conn.execute(
                """UPDATE vacancies SET status = 'expired'
                   WHERE kind = 'telegram' AND status = 'active'
                     AND COALESCE(published, first_seen) < ?""",
                (iso_utc(cutoff),),
            )
        return cur.rowcount

    # ---------- перепроверка актуальности ----------
    def to_recheck(self, kinds, hours: float, limit: int, now: datetime) -> List[sqlite3.Row]:
        threshold = iso_utc(now - timedelta(hours=hours))
        marks = ",".join("?" for _ in kinds)
        return self.conn.execute(
            f"""SELECT * FROM vacancies
                WHERE status = 'active' AND kind IN ({marks})
                  AND (last_check IS NULL OR last_check < ?)
                ORDER BY COALESCE(last_check, first_seen) LIMIT ?""",
            (*kinds, threshold, limit),
        ).fetchall()

    def set_checked(self, key: str, now: datetime) -> None:
        with self.conn:
            self.conn.execute("UPDATE vacancies SET last_check = ? WHERE key = ?", (iso_utc(now), key))

    def add_closed_hit(self, key: str) -> int:
        with self.conn:
            self.conn.execute("UPDATE vacancies SET closed_hits = closed_hits + 1 WHERE key = ?", (key,))
        return self.get(key)["closed_hits"]

    def reset_closed_hits(self, key: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE vacancies SET closed_hits = 0 WHERE key = ?", (key,))

    # ---------- здоровье источников ----------
    def health_fail(self, source: str, error: str) -> int:
        with self.conn:
            self.conn.execute(
                """INSERT INTO source_health (source, fails, last_error) VALUES (?, 1, ?)
                   ON CONFLICT(source) DO UPDATE SET fails = fails + 1, last_error = excluded.last_error""",
                (source, error[:500]),
            )
        return self.conn.execute("SELECT fails FROM source_health WHERE source = ?", (source,)).fetchone()[0]

    def health_ok(self, source: str, alert_threshold: int) -> bool:
        """Сбрасывает счётчик ошибок. True, если источник «чинится» после уже отправленного предупреждения."""
        row = self.conn.execute("SELECT fails FROM source_health WHERE source = ?", (source,)).fetchone()
        was_alerted = bool(row and row["fails"] >= alert_threshold)
        if row:
            with self.conn:
                self.conn.execute("DELETE FROM source_health WHERE source = ?", (source,))
        return was_alerted
