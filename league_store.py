from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import arena

DB_FILE = "bot_league.db"


@dataclass(frozen=True)
class LeagueRow:
    strategy_key: str
    strategy_name: str
    tournaments: int
    matches: int
    hands: int
    total_profit: int
    avg_profit_per_100: float


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_store() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_stats (
                strategy_key    TEXT PRIMARY KEY,
                strategy_name   TEXT NOT NULL,
                tournaments     INTEGER NOT NULL,
                matches         INTEGER NOT NULL,
                hands           INTEGER NOT NULL,
                total_profit    INTEGER NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        conn.commit()


def record_tournament(rows: Iterable[arena.TournamentRow], runs: int) -> None:
    init_store()
    now = datetime.now().isoformat(timespec="seconds")

    with _conn() as conn:
        for row in rows:
            existing = conn.execute(
                "SELECT * FROM strategy_stats WHERE strategy_key = ?",
                (row.strategy_key,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO strategy_stats
                           (strategy_key, strategy_name, tournaments, matches, hands, total_profit, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.strategy_key,
                        row.strategy_name,
                        int(runs),
                        int(row.matches),
                        int(row.hands),
                        int(row.total_profit),
                        now,
                    ),
                )
                continue

            conn.execute(
                """
                UPDATE strategy_stats
                   SET strategy_name = ?,
                       tournaments = tournaments + ?,
                       matches = matches + ?,
                       hands = hands + ?,
                       total_profit = total_profit + ?,
                       updated_at = ?
                 WHERE strategy_key = ?
                """,
                (
                    row.strategy_name,
                    int(runs),
                    int(row.matches),
                    int(row.hands),
                    int(row.total_profit),
                    now,
                    row.strategy_key,
                ),
            )
        conn.commit()


def load_leaderboard(strategy_keys: list[str] | None = None) -> list[LeagueRow]:
    init_store()

    query = "SELECT * FROM strategy_stats"
    params: tuple = ()
    if strategy_keys:
        placeholders = ",".join("?" for _ in strategy_keys)
        query += f" WHERE strategy_key IN ({placeholders})"
        params = tuple(strategy_keys)

    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()

    out: list[LeagueRow] = []
    for row in rows:
        hands = int(row["hands"])
        profit = int(row["total_profit"])
        out.append(
            LeagueRow(
                strategy_key=str(row["strategy_key"]),
                strategy_name=str(row["strategy_name"]),
                tournaments=int(row["tournaments"]),
                matches=int(row["matches"]),
                hands=hands,
                total_profit=profit,
                avg_profit_per_100=(profit / hands) * 100 if hands else 0.0,
            )
        )

    out.sort(key=lambda x: x.avg_profit_per_100, reverse=True)
    return out
