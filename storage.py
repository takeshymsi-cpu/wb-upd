"""SQLite-журнал обработанных уведомлений о выкупе."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = Path(__file__).parent / "storage.db"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed (
                redemption_id   TEXT PRIMARY KEY,
                service_name    TEXT NOT NULL,
                notice_name     TEXT NOT NULL,
                notice_date     TEXT,
                upd_number      TEXT,
                upd_date        TEXT,
                xml_path        TEXT,
                total_sum       REAL,
                items_count     INTEGER,
                processed_at    TEXT NOT NULL,
                status          TEXT DEFAULT 'generated'  -- generated / uploaded / signed
            );
            """
        )


def mark_processed(
    redemption_id: str,
    service_name: str,
    notice_name: str,
    notice_date: str,
    upd_number: str,
    upd_date: str,
    xml_path: str,
    total_sum: float,
    items_count: int,
    status: str = "generated",
) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO processed
            (redemption_id, service_name, notice_name, notice_date,
             upd_number, upd_date, xml_path, total_sum, items_count,
             processed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                redemption_id, service_name, notice_name, notice_date,
                upd_number, upd_date, xml_path, total_sum, items_count,
                datetime.now().isoformat(timespec="seconds"), status,
            ),
        )


def is_processed(redemption_id: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "SELECT 1 FROM processed WHERE redemption_id = ? LIMIT 1", (redemption_id,)
        )
        return cur.fetchone() is not None


def list_processed() -> list[dict]:
    with _conn() as c:
        cur = c.execute(
            "SELECT * FROM processed ORDER BY processed_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]


def get_processed(redemption_id: str) -> Optional[dict]:
    with _conn() as c:
        cur = c.execute(
            "SELECT * FROM processed WHERE redemption_id = ?", (redemption_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_status(redemption_id: str, status: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE processed SET status = ? WHERE redemption_id = ?",
            (status, redemption_id),
        )
