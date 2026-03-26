from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import arena

STORE_FILE = "bot_league.json"


@dataclass(frozen=True)
class LeagueRow:
    strategy_key: str
    strategy_name: str
    tournaments: int
    matches: int
    hands: int
    total_profit: int
    avg_profit_per_100: float


def _read_payload() -> dict:
    path = Path(STORE_FILE)
    if not path.exists():
        return {"version": 1, "strategies": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_payload(payload: dict) -> None:
    Path(STORE_FILE).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def init_store() -> None:
    payload = _read_payload()
    payload.setdefault("version", 1)
    payload.setdefault("strategies", {})
    _write_payload(payload)


def record_tournament(rows: Iterable[arena.TournamentRow], runs: int) -> None:
    init_store()
    payload = _read_payload()
    bucket = payload.setdefault("strategies", {})
    now = datetime.now().isoformat(timespec="seconds")

    for row in rows:
        key = row.strategy_key
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = {
                "strategy_name": row.strategy_name,
                "tournaments": int(runs),
                "matches": int(row.matches),
                "hands": int(row.hands),
                "total_profit": int(row.total_profit),
                "updated_at": now,
            }
            continue

        existing["strategy_name"] = row.strategy_name
        existing["tournaments"] = int(existing.get("tournaments", 0)) + int(runs)
        existing["matches"] = int(existing.get("matches", 0)) + int(row.matches)
        existing["hands"] = int(existing.get("hands", 0)) + int(row.hands)
        existing["total_profit"] = int(existing.get("total_profit", 0)) + int(row.total_profit)
        existing["updated_at"] = now

    _write_payload(payload)


def load_leaderboard(strategy_keys: list[str] | None = None) -> list[LeagueRow]:
    init_store()

    payload = _read_payload()
    bucket = payload.get("strategies", {})

    out: list[LeagueRow] = []
    for key, row in bucket.items():
        if strategy_keys and key not in strategy_keys:
            continue
        hands = int(row.get("hands", 0))
        profit = int(row.get("total_profit", 0))
        out.append(
            LeagueRow(
                strategy_key=str(key),
                strategy_name=str(row.get("strategy_name", key)),
                tournaments=int(row.get("tournaments", 0)),
                matches=int(row.get("matches", 0)),
                hands=hands,
                total_profit=profit,
                avg_profit_per_100=(profit / hands) * 100 if hands else 0.0,
            )
        )

    out.sort(key=lambda x: x.avg_profit_per_100, reverse=True)
    return out
