"""
db.py
-----
SQLite persistence layer for PokerOracle simulations.

Each simulation is uniquely identified by (hand, stage, community_cards,
num_opponents).  Re-running the same scenario accumulates the new iterations
into the existing row so that statistical precision improves over time.
"""

import sqlite3
from datetime import datetime
from typing import Optional, Tuple

DB_FILE = "poker_oracle.db"


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
