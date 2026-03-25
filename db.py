"""
db.py
-----
SQLite persistence layer for PokerOracle simulations.

Each simulation is uniquely identified by (hand, stage, community_cards,
num_opponents).  Re-running the same scenario accumulates the new iterations
into the existing row so that statistical precision improves over time.
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

DB_FILE = "poker_oracle.db"
DB_SNAPSHOT_FILE = "db_snapshot.json"


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the simulations table if it does not already exist."""
    with _get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS simulations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                hand            TEXT    NOT NULL,
                stage           TEXT    NOT NULL,
                community_cards TEXT    NOT NULL DEFAULT '',
                num_opponents   INTEGER NOT NULL,
                wins            INTEGER NOT NULL,
                ties            INTEGER NOT NULL,
                losses          INTEGER NOT NULL,
                total           INTEGER NOT NULL,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            )
            """
        )
        conn.commit()


def save_or_update(
    hand: str,
    stage: str,
    community_cards: str,
    num_opponents: int,
    wins: int,
    ties: int,
    losses: int,
    total: int,
) -> Tuple[int, bool]:
    """
    Insert a new simulation row, or accumulate results into the existing row.

    Returns:
        (row_id, was_updated) – was_updated is True when an existing row was
        merged, False when a brand-new row was inserted.
    """
    now = datetime.now().isoformat(timespec="seconds")
    community_cards = community_cards or ""

    with _get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, wins, ties, losses, total
              FROM simulations
             WHERE hand = ? AND stage = ? AND community_cards = ?
               AND num_opponents = ?
            """,
            (hand, stage, community_cards, num_opponents),
        ).fetchone()

        if row:
            new_wins = row["wins"] + wins
            new_ties = row["ties"] + ties
            new_losses = row["losses"] + losses
            new_total = row["total"] + total
            conn.execute(
                """
                UPDATE simulations
                   SET wins = ?, ties = ?, losses = ?, total = ?, updated_at = ?
                 WHERE id = ?
                """,
                (new_wins, new_ties, new_losses, new_total, now, row["id"]),
            )
            conn.commit()
            return row["id"], True

        cursor = conn.execute(
            """
            INSERT INTO simulations
                   (hand, stage, community_cards, num_opponents,
                    wins, ties, losses, total, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hand,
                stage,
                community_cards,
                num_opponents,
                wins,
                ties,
                losses,
                total,
                now,
                now,
            ),
        )
        conn.commit()
        return cursor.lastrowid, False


def fetch_existing(
    hand: str,
    stage: str,
    community_cards: str,
    num_opponents: int,
) -> Optional[sqlite3.Row]:
    """
    Return the stored simulation row for the given key, or None.
    """
    community_cards = community_cards or ""
    with _get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM simulations
             WHERE hand = ? AND stage = ? AND community_cards = ?
               AND num_opponents = ?
            """,
            (hand, stage, community_cards, num_opponents),
        ).fetchone()


def fetch_all() -> list:
    """Return all simulation rows ordered by most-recently updated."""
    with _get_connection() as conn:
        return conn.execute(
            "SELECT * FROM simulations ORDER BY updated_at DESC"
        ).fetchall()


def export_snapshot(snapshot_file: str = DB_SNAPSHOT_FILE) -> Path:
    """Export current DB rows to a JSON snapshot that can be committed to Git."""
    rows = fetch_all()
    payload = {
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "simulations": [dict(row) for row in rows],
    }
    out = Path(snapshot_file)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out


def import_snapshot(snapshot_file: str = DB_SNAPSHOT_FILE) -> int:
    """
    Import rows from a JSON snapshot into SQLite.

    Returns the number of inserted rows. Existing scenario keys are skipped.
    """
    path = Path(snapshot_file)
    if not path.exists():
        return 0

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("simulations", [])
    inserted = 0

    with _get_connection() as conn:
        for row in rows:
            existing = conn.execute(
                """
                SELECT id FROM simulations
                 WHERE hand = ? AND stage = ? AND community_cards = ?
                   AND num_opponents = ?
                """,
                (
                    row.get("hand", ""),
                    row.get("stage", ""),
                    row.get("community_cards", ""),
                    row.get("num_opponents", 0),
                ),
            ).fetchone()
            if existing:
                continue

            conn.execute(
                """
                INSERT INTO simulations
                       (hand, stage, community_cards, num_opponents,
                        wins, ties, losses, total, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("hand", ""),
                    row.get("stage", ""),
                    row.get("community_cards", ""),
                    row.get("num_opponents", 0),
                    row.get("wins", 0),
                    row.get("ties", 0),
                    row.get("losses", 0),
                    row.get("total", 0),
                    row.get("created_at") or datetime.now().isoformat(timespec="seconds"),
                    row.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
                ),
            )
            inserted += 1

        conn.commit()

    return inserted
