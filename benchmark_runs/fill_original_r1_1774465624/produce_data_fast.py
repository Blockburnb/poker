"""
produce_data_fast.py
--------------------
Higher-throughput deterministic producer for River scenarios.

Main performance changes vs produce_data.py:
1) Parallel Monte Carlo simulation using multiple processes.
2) Fill phase processed in 1000-scenario chunks (checkpoint written per chunk).
3) Snapshot export throttled to once every 5 minutes.
4) Single-process DB writes to avoid SQLite writer contention.

Notes:
- Enumeration order remains deterministic (opponents 1 -> 9).
- Checkpoint granularity is intentionally coarser for speed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from itertools import combinations, islice
from pathlib import Path
from typing import Iterator

import db
import simulator as sim
import ui

BATCH_ITERATIONS = 1000
MAX_OPPONENTS = 9
CHECKPOINT_FILE = "producer_checkpoint_fast.json"
STAGE = "River"

# Persist state every N scenarios/cycles.
CHECKPOINT_EVERY = 1000
# Export full JSON snapshot at most once every 5 minutes.
EXPORT_EVERY_SECONDS = 5 * 60
# Bound worker count to keep the machine responsive.
MAX_WORKERS = max(1, (os.cpu_count() or 2) - 1)
# Number of least-simulated rows processed in parallel per refine batch.
REFINE_PARALLEL_BATCH = max(1, MAX_WORKERS * 2)


def _ordered_deck() -> list[int]:
    return list(sim.FULL_DECK)


def _total_fill_scenarios() -> int:
    return 9 * 1326 * 2_118_760


def _load_checkpoint() -> dict:
    path = Path(CHECKPOINT_FILE)
    if not path.exists():
        return {
            "phase": "fill",
            "opp": 1,
            "hand_index": 0,
            "board_index": 0,
            "processed": 0,
            "inserted": 0,
            "refine_cycles": 0,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _save_checkpoint(state: dict) -> None:
    Path(CHECKPOINT_FILE).write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _hand_key(hand: list[int]) -> str:
    return " ".join(sorted(sim.cards_to_str([c]).strip() for c in hand))


def _community_key(community_cards: list[int]) -> str:
    return " ".join(sim.cards_to_str([c]).strip() for c in community_cards)


def _iter_all_scenarios(
    start_opp: int,
    start_hand_index: int,
    start_board_index: int,
) -> Iterator[tuple[int, int, int, list[int], list[int]]]:
    deck = _ordered_deck()

    for opp in range(start_opp, MAX_OPPONENTS + 1):
        hand_start = start_hand_index if opp == start_opp else 0
        for hand_idx, hand_tuple in enumerate(combinations(deck, 2)):
            if hand_idx < hand_start:
                continue

            remaining = [c for c in deck if c not in hand_tuple]
            board_start = start_board_index if (opp == start_opp and hand_idx == hand_start) else 0
            for board_idx, board_tuple in enumerate(combinations(remaining, 5)):
                if board_idx < board_start:
                    continue
                yield opp, hand_idx, board_idx, list(hand_tuple), list(board_tuple)


def _simulate_worker(args: tuple[tuple[int, ...], tuple[int, ...], int, int]) -> dict:
    hand, board, num_opponents, iterations = args
    return sim.simulate(list(hand), list(board), num_opponents, iterations)


def _save_result_row(hand: list[int], board: list[int], num_opponents: int, result: dict) -> None:
    db.save_or_update(
        _hand_key(hand),
        STAGE,
        _community_key(board),
        num_opponents,
        int(result["wins"]),
        int(result["ties"]),
        int(result["losses"]),
        int(result["total"]),
    )


def _advance_cursor(state: dict, opp: int, hand_idx: int, board_idx: int) -> None:
    state["opp"] = opp
    state["hand_index"] = hand_idx
    state["board_index"] = board_idx + 1


def _switch_to_refine(state: dict) -> None:
    state["phase"] = "refine"
    state["opp"] = 1
    state["hand_index"] = 0
    state["board_index"] = 0


def _maybe_export_snapshot(last_export_at: float) -> float:
    now = time.time()
    if now - last_export_at >= EXPORT_EVERY_SECONDS:
        db.export_snapshot()
        return now
    return last_export_at


def _fetch_least_simulated_batch(stage: str, limit: int) -> list[dict]:
    with sqlite3.connect(db.DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM simulations
             WHERE stage = ?
             ORDER BY total ASC, updated_at ASC
             LIMIT ?
            """,
            (stage, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _run_fill(state: dict, pool: ProcessPoolExecutor) -> float:
    total_scenarios = _total_fill_scenarios()
    last_export_at = time.time()

    scenario_iter = _iter_all_scenarios(
        int(state["opp"]),
        int(state["hand_index"]),
        int(state["board_index"]),
    )

    while True:
        chunk = list(islice(scenario_iter, CHECKPOINT_EVERY))
        if not chunk:
            break

        missing_records: list[tuple[int, int, int, list[int], list[int], str, str]] = []
        skipped_existing = 0

        for opp, hand_idx, board_idx, hand, board in chunk:
            hand_key = _hand_key(hand)
            board_key = _community_key(board)
            row = db.fetch_by_exact(hand_key, STAGE, board_key, opp)
            if row is None:
                missing_records.append((opp, hand_idx, board_idx, hand, board, hand_key, board_key))
            else:
                skipped_existing += 1

        if missing_records:
            tasks = [
                (tuple(hand), tuple(board), opp, BATCH_ITERATIONS)
                for opp, _, _, hand, board, _, _ in missing_records
            ]
            results = list(pool.map(_simulate_worker, tasks, chunksize=8))

            for (opp, _, _, hand, board, _, _), result in zip(missing_records, results):
                _save_result_row(hand, board, opp, result)

            state["inserted"] = int(state["inserted"]) + len(missing_records)

        state["processed"] = int(state["processed"]) + len(chunk)
        last_opp, last_hand_idx, last_board_idx, _, _ = chunk[-1]
        _advance_cursor(state, last_opp, last_hand_idx, last_board_idx)

        _save_checkpoint(state)
        last_export_at = _maybe_export_snapshot(last_export_at)

        progress = (int(state["processed"]) / total_scenarios) * 100
        ui.console.print(
            f"[green]fill[/green] processed={state['processed']:,}/{total_scenarios:,} "
            f"({progress:.8f}%) new={state['inserted']:,} "
            f"batch_new={len(missing_records):,} batch_existing={skipped_existing:,}"
        )

    _switch_to_refine(state)
    _save_checkpoint(state)
    db.export_snapshot()
    ui.console.print("[bold cyan]Fill phase completed. Switching to refine phase.[/bold cyan]")
    return time.time()


def _run_refine(state: dict, pool: ProcessPoolExecutor, last_export_at: float) -> None:
    while True:
        least_rows = _fetch_least_simulated_batch(STAGE, REFINE_PARALLEL_BATCH)
        if not least_rows:
            ui.console.print("[red]No River rows found for refine phase.[/red]")
            return

        tasks: list[tuple[tuple[int, ...], tuple[int, ...], int, int]] = []
        parsed_rows: list[tuple[int, list[int], list[int], int, int]] = []

        for row in least_rows:
            hand = [sim.parse_card(x) for x in str(row["hand"]).split()]
            board = [sim.parse_card(x) for x in str(row["community_cards"]).split() if x]
            num_opponents = int(row["num_opponents"])
            tasks.append((tuple(hand), tuple(board), num_opponents, BATCH_ITERATIONS))
            parsed_rows.append((int(row["id"]), hand, board, num_opponents, int(row["total"])))

        results = list(pool.map(_simulate_worker, tasks, chunksize=8))

        for (row_id, hand, board, num_opponents, total_before), result in zip(parsed_rows, results):
            _save_result_row(hand, board, num_opponents, result)
            state["refine_cycles"] = int(state["refine_cycles"]) + 1

            if int(state["refine_cycles"]) % CHECKPOINT_EVERY == 0:
                _save_checkpoint(state)
                ui.console.print(
                    f"[cyan]checkpoint[/cyan] refine_cycles={state['refine_cycles']:,}"
                )

            ui.console.print(
                f"[cyan]refine#{state['refine_cycles']}[/cyan] "
                f"id={row_id} opp={num_opponents} total_before={total_before:,} +{BATCH_ITERATIONS}"
            )

        last_export_at = _maybe_export_snapshot(last_export_at)


def main() -> None:
    db.init_db()
    db.import_snapshot()
    state = _load_checkpoint()

    ui.display_title()
    ui.console.rule("[yellow]Deterministic Producer Mode FAST (River only)[/yellow]")
    ui.console.print(
        f"[dim]Workers={MAX_WORKERS} checkpoint_every={CHECKPOINT_EVERY} "
        f"export_every={EXPORT_EVERY_SECONDS}s[/dim]"
    )
    ui.console.print(
        f"[dim]Resume: phase={state['phase']} opp={state['opp']} "
        f"hand_index={state['hand_index']} board_index={state['board_index']}[/dim]"
    )

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        last_export_at = time.time()
        if state.get("phase") == "fill":
            last_export_at = _run_fill(state, pool)

        _run_refine(state, pool, last_export_at)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Ensure an up-to-date snapshot/checkpoint on manual stop.
        state = _load_checkpoint()
        _save_checkpoint(state)
        db.export_snapshot()
        ui.console.print("\n[dim]Interrupted. Checkpoint and snapshot saved.[/dim]")
        sys.exit(0)
