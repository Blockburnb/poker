"""
produce_data.py
---------------
Deterministic data producer for River scenarios.

Fill phase:
1) Enumerate scenarios in deterministic order (opponents 1 -> 9).
2) For each scenario, if missing in DB, run 1000 Monte Carlo iterations.
3) Save checkpoint after each processed scenario (exact resume).

Refine phase:
- Starts only when fill is fully complete.
- Repeatedly adds 1000 iterations to the least-simulated River scenario.
"""

from __future__ import annotations

import json
import sys
import time
from itertools import combinations
from pathlib import Path
from typing import Iterator

import db
import simulator as sim
import ui
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

BATCH_ITERATIONS = 1000
MAX_OPPONENTS = 9
CHECKPOINT_FILE = "producer_checkpoint.json"
STAGE = "River"


def _ordered_deck() -> list[int]:
    # Deterministic card order used for canonical enumeration.
    return list(sim.FULL_DECK)


def _total_fill_scenarios() -> int:
    # 9 opponent counts * C(52,2) hole-card combos * C(50,5) board combos.
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


def _run_and_store(hand: list[int], board: list[int], num_opponents: int) -> None:
    result = sim.simulate(hand, board, num_opponents, BATCH_ITERATIONS)
    db.save_or_update(
        _hand_key(hand),
        "River",
        _community_key(board),
        num_opponents,
        result["wins"],
        result["ties"],
        result["losses"],
        result["total"],
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


def _import_snapshot_with_progress() -> int:
    task_total = 1
    inserted_rows = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,}"),
        TextColumn("inserted={task.fields[inserted]:,}"),
        TimeElapsedColumn(),
        console=ui.console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("Importing DB snapshot", total=task_total, inserted=0)

        def _on_progress(processed: int, total: int, inserted: int) -> None:
            nonlocal inserted_rows
            inserted_rows = inserted
            progress.update(task_id, total=max(1, total), completed=processed, inserted=inserted)

        inserted_rows = db.import_snapshot(progress_callback=_on_progress, progress_every=2500)

    return inserted_rows


def _run_fill(state: dict) -> None:
    total_scenarios = _total_fill_scenarios()

    for opp, hand_idx, board_idx, hand, board in _iter_all_scenarios(
        int(state["opp"]),
        int(state["hand_index"]),
        int(state["board_index"]),
    ):
        hand_key = _hand_key(hand)
        board_key = _community_key(board)
        row = db.fetch_by_exact(hand_key, STAGE, board_key, opp)

        if row is None:
            _run_and_store(hand, board, opp)
            state["inserted"] = int(state["inserted"]) + 1
            action = "added"
        else:
            action = "exists"

        state["processed"] = int(state["processed"]) + 1
        _advance_cursor(state, opp, hand_idx, board_idx)

        # Crash-safe persistence: every processed scenario is committed.
        db.export_snapshot()
        _save_checkpoint(state)

        progress = (int(state["processed"]) / total_scenarios) * 100
        ui.console.print(
            f"[green]{action}[/green] opp={opp} hand={hand_key} board={board_key} "
            f"processed={state['processed']:,}/{total_scenarios:,} ({progress:.8f}%) "
            f"new={state['inserted']:,}"
        )

    _switch_to_refine(state)
    _save_checkpoint(state)
    db.export_snapshot()
    ui.console.print("[bold cyan]Fill phase completed. Switching to refine phase.[/bold cyan]")


def _run_refine(state: dict) -> None:
    while True:
        least = db.fetch_least_simulated(STAGE)
        if not least:
            ui.console.print("[red]No River rows found for refine phase.[/red]")
            return

        hand = [sim.parse_card(x) for x in least["hand"].split()]
        board = [sim.parse_card(x) for x in least["community_cards"].split() if x]
        num_opponents = int(least["num_opponents"])

        _run_and_store(hand, board, num_opponents)
        state["refine_cycles"] = int(state["refine_cycles"]) + 1

        db.export_snapshot()
        _save_checkpoint(state)

        ui.console.print(
            f"[cyan]refine#{state['refine_cycles']}[/cyan] "
            f"id={least['id']} opp={num_opponents} total_before={least['total']:,} +{BATCH_ITERATIONS}"
        )


def main() -> None:
    ui.console.print("[dim]Startup 1/3: initializing SQLite schema...[/dim]")
    db.init_db()
    ui.console.print(
        f"[dim]Startup 2/3: importing snapshot '{db.DB_SNAPSHOT_FILE}' (can be slow on large files)...[/dim]"
    )
    import_started = time.perf_counter()
    imported_rows = _import_snapshot_with_progress()
    import_elapsed = time.perf_counter() - import_started
    ui.console.print(
        f"[dim]Snapshot import done: inserted={imported_rows:,} rows in {import_elapsed:.2f}s[/dim]"
    )
    ui.console.print("[dim]Startup 3/3: loading checkpoint...[/dim]")
    state = _load_checkpoint()

    ui.display_title()
    ui.console.rule("[yellow]Deterministic Producer Mode (River only)[/yellow]")
    ui.console.print("[dim]Order: opponents 1 -> 9, canonical card order. Press Ctrl+C to stop.[/dim]")
    ui.console.print(
        f"[dim]Resume: phase={state['phase']} opp={state['opp']} "
        f"hand_index={state['hand_index']} board_index={state['board_index']}[/dim]"
    )

    if state.get("phase") == "fill":
        _run_fill(state)

    _run_refine(state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
