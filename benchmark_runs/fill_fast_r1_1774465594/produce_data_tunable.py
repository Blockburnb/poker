"""
produce_data_tunable.py
-----------------------
Tunable high-throughput deterministic producer for River scenarios.

This variant is designed for benchmarking and parameter sweeps.
Compared to produce_data_fast.py, it exposes runtime knobs via CLI:
- number of workers
- Monte Carlo iterations per scenario
- checkpoint frequency
- snapshot export interval
- refine batch size
- phase selection and optional stop limits for timed benchmarks

Example benchmark-friendly run:
    python produce_data_tunable.py \
      --phase fill \
      --workers 8 \
      --batch-iterations 500 \
      --checkpoint-every 1000 \
      --export-every-seconds 300 \
      --max-fill-scenarios 20000 \
      --checkpoint-file producer_checkpoint_tunable.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from itertools import combinations, islice
from pathlib import Path
from typing import Iterator

import db
import simulator as sim
import ui

DEFAULT_STAGE = "River"
DEFAULT_MAX_OPPONENTS = 9
DEFAULT_CHECKPOINT_FILE = "producer_checkpoint_tunable.json"
DEFAULT_WORKERS = max(1, (os.cpu_count() or 2) - 1)


@dataclass(frozen=True)
class Settings:
    stage: str
    max_opponents: int
    batch_iterations: int
    checkpoint_every: int
    export_every_seconds: int
    workers: int
    refine_parallel_batch: int
    checkpoint_file: str
    phase: str
    max_fill_scenarios: int | None
    max_refine_cycles: int | None


def _parse_args() -> Settings:
    parser = argparse.ArgumentParser(
        description="Tunable multiprocessing producer for River scenarios.",
    )
    parser.add_argument("--phase", choices=["fill", "refine", "both"], default="both")
    parser.add_argument("--stage", default=DEFAULT_STAGE)
    parser.add_argument("--max-opponents", type=int, default=DEFAULT_MAX_OPPONENTS)
    parser.add_argument("--batch-iterations", type=int, default=1000)
    parser.add_argument("--checkpoint-every", type=int, default=1000)
    parser.add_argument("--export-every-seconds", type=int, default=300)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--refine-parallel-batch", type=int, default=0)
    parser.add_argument("--checkpoint-file", default=DEFAULT_CHECKPOINT_FILE)

    # Optional limits to make short benchmark runs easy.
    parser.add_argument("--max-fill-scenarios", type=int, default=None)
    parser.add_argument("--max-refine-cycles", type=int, default=None)

    # Optional isolated artifacts for A/B benchmark runs.
    parser.add_argument("--db-file", default=db.DB_FILE)
    parser.add_argument("--snapshot-file", default=db.DB_SNAPSHOT_FILE)

    args = parser.parse_args()

    if args.max_opponents < 1:
        parser.error("--max-opponents must be >= 1")
    if args.batch_iterations < 1:
        parser.error("--batch-iterations must be >= 1")
    if args.checkpoint_every < 1:
        parser.error("--checkpoint-every must be >= 1")
    if args.export_every_seconds < 0:
        parser.error("--export-every-seconds must be >= 0")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.max_fill_scenarios is not None and args.max_fill_scenarios < 1:
        parser.error("--max-fill-scenarios must be >= 1")
    if args.max_refine_cycles is not None and args.max_refine_cycles < 1:
        parser.error("--max-refine-cycles must be >= 1")

    refine_batch = args.refine_parallel_batch
    if refine_batch < 1:
        refine_batch = max(1, args.workers * 2)

    # Allow benchmark script to isolate datasets per run.
    db.DB_FILE = args.db_file
    db.DB_SNAPSHOT_FILE = args.snapshot_file

    return Settings(
        stage=args.stage,
        max_opponents=args.max_opponents,
        batch_iterations=args.batch_iterations,
        checkpoint_every=args.checkpoint_every,
        export_every_seconds=args.export_every_seconds,
        workers=args.workers,
        refine_parallel_batch=refine_batch,
        checkpoint_file=args.checkpoint_file,
        phase=args.phase,
        max_fill_scenarios=args.max_fill_scenarios,
        max_refine_cycles=args.max_refine_cycles,
    )


def _ordered_deck() -> list[int]:
    return list(sim.FULL_DECK)


def _total_fill_scenarios(max_opponents: int) -> int:
    return max_opponents * 1326 * 2_118_760


def _load_checkpoint(checkpoint_file: str) -> dict:
    path = Path(checkpoint_file)
    if not path.exists():
        return {
            "phase": "fill",
            "opp": 1,
            "hand_index": 0,
            "board_index": 0,
            "processed": 0,
            "inserted": 0,
            "refine_cycles": 0,
            "last_saved_at": int(time.time()),
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("last_saved_at", int(time.time()))
    return state


def _save_checkpoint(checkpoint_file: str, state: dict) -> None:
    state["last_saved_at"] = int(time.time())
    Path(checkpoint_file).write_text(
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
    max_opponents: int,
) -> Iterator[tuple[int, int, int, list[int], list[int]]]:
    deck = _ordered_deck()

    for opp in range(start_opp, max_opponents + 1):
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


def _save_result_row(stage: str, hand: list[int], board: list[int], num_opponents: int, result: dict) -> None:
    db.save_or_update(
        _hand_key(hand),
        stage,
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


def _maybe_export_snapshot(last_export_at: float, settings: Settings) -> float:
    if settings.export_every_seconds == 0:
        return last_export_at
    now = time.time()
    if now - last_export_at >= settings.export_every_seconds:
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


def _run_fill(state: dict, settings: Settings, pool: ProcessPoolExecutor) -> tuple[float, bool]:
    total_scenarios = _total_fill_scenarios(settings.max_opponents)
    last_export_at = time.time()

    scenario_iter = _iter_all_scenarios(
        int(state["opp"]),
        int(state["hand_index"]),
        int(state["board_index"]),
        settings.max_opponents,
    )

    stop_requested = False

    while True:
        chunk = list(islice(scenario_iter, settings.checkpoint_every))
        if not chunk:
            break

        if settings.max_fill_scenarios is not None:
            remaining_allowed = settings.max_fill_scenarios - int(state["processed"])
            if remaining_allowed <= 0:
                stop_requested = True
                break
            if len(chunk) > remaining_allowed:
                chunk = chunk[:remaining_allowed]

        missing_records: list[tuple[int, int, int, list[int], list[int]]] = []
        skipped_existing = 0

        for opp, hand_idx, board_idx, hand, board in chunk:
            row = db.fetch_by_exact(_hand_key(hand), settings.stage, _community_key(board), opp)
            if row is None:
                missing_records.append((opp, hand_idx, board_idx, hand, board))
            else:
                skipped_existing += 1

        if missing_records:
            tasks = [
                (tuple(hand), tuple(board), opp, settings.batch_iterations)
                for opp, _, _, hand, board in missing_records
            ]
            results = list(pool.map(_simulate_worker, tasks, chunksize=8))

            for (opp, _, _, hand, board), result in zip(missing_records, results):
                _save_result_row(settings.stage, hand, board, opp, result)

            state["inserted"] = int(state["inserted"]) + len(missing_records)

        state["processed"] = int(state["processed"]) + len(chunk)
        last_opp, last_hand_idx, last_board_idx, _, _ = chunk[-1]
        _advance_cursor(state, last_opp, last_hand_idx, last_board_idx)

        _save_checkpoint(settings.checkpoint_file, state)
        last_export_at = _maybe_export_snapshot(last_export_at, settings)

        progress = (int(state["processed"]) / total_scenarios) * 100
        ui.console.print(
            f"[green]fill[/green] processed={state['processed']:,}/{total_scenarios:,} "
            f"({progress:.8f}%) new={state['inserted']:,} "
            f"batch_new={len(missing_records):,} batch_existing={skipped_existing:,}"
        )

        if settings.max_fill_scenarios is not None and int(state["processed"]) >= settings.max_fill_scenarios:
            stop_requested = True
            break

    if stop_requested:
        ui.console.print("[yellow]Fill paused due to --max-fill-scenarios limit.[/yellow]")
        return last_export_at, False

    _switch_to_refine(state)
    _save_checkpoint(settings.checkpoint_file, state)
    db.export_snapshot()
    ui.console.print("[bold cyan]Fill phase completed. Switching to refine phase.[/bold cyan]")
    return time.time(), True


def _run_refine(state: dict, settings: Settings, pool: ProcessPoolExecutor, last_export_at: float) -> float:
    while True:
        if settings.max_refine_cycles is not None and int(state["refine_cycles"]) >= settings.max_refine_cycles:
            ui.console.print("[yellow]Refine paused due to --max-refine-cycles limit.[/yellow]")
            return last_export_at

        least_rows = _fetch_least_simulated_batch(settings.stage, settings.refine_parallel_batch)
        if not least_rows:
            ui.console.print(f"[red]No {settings.stage} rows found for refine phase.[/red]")
            return last_export_at

        tasks: list[tuple[tuple[int, ...], tuple[int, ...], int, int]] = []
        parsed_rows: list[tuple[int, list[int], list[int], int, int]] = []

        for row in least_rows:
            hand = [sim.parse_card(x) for x in str(row["hand"]).split()]
            board = [sim.parse_card(x) for x in str(row["community_cards"]).split() if x]
            num_opponents = int(row["num_opponents"])
            tasks.append((tuple(hand), tuple(board), num_opponents, settings.batch_iterations))
            parsed_rows.append((int(row["id"]), hand, board, num_opponents, int(row["total"])))

        results = list(pool.map(_simulate_worker, tasks, chunksize=8))

        for (row_id, hand, board, num_opponents, total_before), result in zip(parsed_rows, results):
            _save_result_row(settings.stage, hand, board, num_opponents, result)
            state["refine_cycles"] = int(state["refine_cycles"]) + 1

            if int(state["refine_cycles"]) % settings.checkpoint_every == 0:
                _save_checkpoint(settings.checkpoint_file, state)
                ui.console.print(
                    f"[cyan]checkpoint[/cyan] refine_cycles={state['refine_cycles']:,}"
                )

            ui.console.print(
                f"[cyan]refine#{state['refine_cycles']}[/cyan] "
                f"id={row_id} opp={num_opponents} total_before={total_before:,} +{settings.batch_iterations}"
            )

            if settings.max_refine_cycles is not None and int(state["refine_cycles"]) >= settings.max_refine_cycles:
                _save_checkpoint(settings.checkpoint_file, state)
                ui.console.print("[yellow]Reached --max-refine-cycles limit.[/yellow]")
                return _maybe_export_snapshot(last_export_at, settings)

        last_export_at = _maybe_export_snapshot(last_export_at, settings)


def main() -> None:
    settings = _parse_args()

    db.init_db()
    db.import_snapshot()
    state = _load_checkpoint(settings.checkpoint_file)

    run_started_at = time.time()

    ui.display_title()
    ui.console.rule("[yellow]Deterministic Producer Mode TUNABLE[/yellow]")
    ui.console.print(
        f"[dim]phase={settings.phase} workers={settings.workers} "
        f"batch_iterations={settings.batch_iterations} checkpoint_every={settings.checkpoint_every} "
        f"export_every={settings.export_every_seconds}s refine_batch={settings.refine_parallel_batch}[/dim]"
    )
    ui.console.print(
        f"[dim]db={db.DB_FILE} snapshot={db.DB_SNAPSHOT_FILE} checkpoint={settings.checkpoint_file}[/dim]"
    )
    ui.console.print(
        f"[dim]Resume: phase={state['phase']} opp={state['opp']} "
        f"hand_index={state['hand_index']} board_index={state['board_index']}[/dim]"
    )

    with ProcessPoolExecutor(max_workers=settings.workers) as pool:
        last_export_at = time.time()

        should_run_fill = settings.phase in {"fill", "both"} and state.get("phase") == "fill"
        should_run_refine = settings.phase in {"refine", "both"}

        fill_completed = state.get("phase") != "fill"
        if should_run_fill:
            last_export_at, fill_completed = _run_fill(state, settings, pool)

        if should_run_refine and state.get("phase") == "refine" and fill_completed:
            last_export_at = _run_refine(state, settings, pool, last_export_at)

        _save_checkpoint(settings.checkpoint_file, state)
        # Force a final snapshot at process end for benchmark reproducibility.
        db.export_snapshot()

    elapsed = time.time() - run_started_at
    summary = {
        "elapsed_seconds": round(elapsed, 3),
        "phase": state.get("phase"),
        "processed": int(state.get("processed", 0)),
        "inserted": int(state.get("inserted", 0)),
        "refine_cycles": int(state.get("refine_cycles", 0)),
        "workers": settings.workers,
        "batch_iterations": settings.batch_iterations,
        "checkpoint_every": settings.checkpoint_every,
        "export_every_seconds": settings.export_every_seconds,
        "refine_parallel_batch": settings.refine_parallel_batch,
    }

    ui.console.rule("[green]Run Summary[/green]")
    ui.console.print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
