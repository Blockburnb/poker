"""
benchmark_producers.py
----------------------
Benchmark helper to identify the best producer on the current machine for:
- fill throughput (new River rows / second)
- refine throughput (Monte Carlo iterations / second)

It compares:
1) produce_data.py (original)
2) produce_data_fast.py (fast)
3) produce_data_tunable.py (parameter sweep)

The script runs each candidate in an isolated temporary run directory so DB,
checkpoint, and snapshot files do not interfere across candidates.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations, product
from pathlib import Path

import simulator as sim

PROJECT_FILES = [
    "db.py",
    "simulator.py",
    "ui.py",
    "produce_data.py",
    "produce_data_fast.py",
    "produce_data_tunable.py",
]


@dataclass(frozen=True)
class Candidate:
    name: str
    command: list[str]
    checkpoint_file: str
    batch_iterations: int


@dataclass
class RunResult:
    scenario: str
    candidate: str
    repeat: int
    elapsed_seconds: float
    timed_out: bool
    exit_code: int | None
    metric_name: str
    metric_value: float
    extra: dict
    log_file: str


def _parse_csv_ints(value: str) -> list[int]:
    out: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def _parse_csv_positive_ints(value: str, arg_name: str) -> list[int]:
    values = _parse_csv_ints(value)
    if not values:
        raise ValueError(f"{arg_name} must contain at least one integer value")
    if any(v < 1 for v in values):
        raise ValueError(f"{arg_name} values must be >= 1")
    # Keep ordering stable but avoid duplicate candidate generation.
    return list(dict.fromkeys(values))


def _parse_csv_non_negative_ints(value: str, arg_name: str) -> list[int]:
    values = _parse_csv_ints(value)
    if not values:
        raise ValueError(f"{arg_name} must contain at least one integer value")
    if any(v < 0 for v in values):
        raise ValueError(f"{arg_name} values must be >= 0")
    return list(dict.fromkeys(values))


def _build_candidates(args: argparse.Namespace) -> list[Candidate]:
    candidates: list[Candidate] = []

    if args.include_original:
        candidates.append(
            Candidate(
                name="original",
                command=["produce_data.py"],
                checkpoint_file="producer_checkpoint.json",
                batch_iterations=1000,
            )
        )

    if args.include_fast:
        candidates.append(
            Candidate(
                name="fast",
                command=["produce_data_fast.py"],
                checkpoint_file="producer_checkpoint_fast.json",
                batch_iterations=1000,
            )
        )

    workers = _parse_csv_positive_ints(args.tunable_workers, "--tunable-workers")
    checkpoints = _parse_csv_positive_ints(
        args.tunable_checkpoint_every,
        "--tunable-checkpoint-every",
    )
    batch_iterations_values = _parse_csv_positive_ints(
        args.tunable_batch_iterations,
        "--tunable-batch-iterations",
    )
    export_every_values = _parse_csv_non_negative_ints(
        args.tunable_export_every_seconds,
        "--tunable-export-every-seconds",
    )
    refine_batch_values = _parse_csv_non_negative_ints(
        args.tunable_refine_parallel_batch,
        "--tunable-refine-parallel-batch",
    )

    for workers_value, checkpoint_value, batch_value, export_value, refine_batch_value in product(
        workers,
        checkpoints,
        batch_iterations_values,
        export_every_values,
        refine_batch_values,
    ):
        checkpoint_file = (
            "producer_checkpoint_tunable_"
            f"w{workers_value}_c{checkpoint_value}_b{batch_value}_"
            f"e{export_value}_r{refine_batch_value}.json"
        )
        name = (
            f"tunable_w{workers_value}_c{checkpoint_value}_"
            f"b{batch_value}_e{export_value}_r{refine_batch_value}"
        )
        cmd = [
            "produce_data_tunable.py",
            "--workers",
            str(workers_value),
            "--batch-iterations",
            str(batch_value),
            "--checkpoint-every",
            str(checkpoint_value),
            "--export-every-seconds",
            str(export_value),
            "--refine-parallel-batch",
            str(refine_batch_value),
            "--checkpoint-file",
            checkpoint_file,
            "--phase",
            "both",
        ]
        candidates.append(
            Candidate(
                name=name,
                command=cmd,
                checkpoint_file=checkpoint_file,
                batch_iterations=batch_value,
            )
        )

    return candidates


def _copy_project_files(project_root: Path, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for filename in PROJECT_FILES:
        src = project_root / filename
        if not src.exists():
            raise FileNotFoundError(f"Required file not found: {src}")
        shutil.copy2(src, run_dir / filename)


def _init_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
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


def _seed_refine_db(db_path: Path, stage: str, rows_to_seed: int) -> None:
    _init_db(db_path)
    now = datetime.now().isoformat(timespec="seconds")

    deck = list(sim.FULL_DECK)
    inserted = 0

    with sqlite3.connect(db_path) as conn:
        for hand in combinations(deck, 2):
            remaining = [c for c in deck if c not in hand]
            for board in combinations(remaining, 5):
                hand_key = " ".join(sorted(sim.cards_to_str([c]).strip() for c in hand))
                board_key = " ".join(sim.cards_to_str([c]).strip() for c in board)
                conn.execute(
                    """
                    INSERT INTO simulations
                           (hand, stage, community_cards, num_opponents,
                            wins, ties, losses, total, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hand_key,
                        stage,
                        board_key,
                        1,
                        500,
                        100,
                        400,
                        1000,
                        now,
                        now,
                    ),
                )
                inserted += 1
                if inserted >= rows_to_seed:
                    conn.commit()
                    return
        conn.commit()


def _write_checkpoint(checkpoint_path: Path, phase: str) -> None:
    payload = {
        "phase": phase,
        "opp": 1,
        "hand_index": 0,
        "board_index": 0,
        "processed": 0,
        "inserted": 0,
        "refine_cycles": 0,
    }
    checkpoint_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _db_row_count(db_path: Path, stage: str) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM simulations WHERE stage = ?",
            (stage,),
        ).fetchone()
    return int(row[0]) if row else 0


def _db_total_sum(db_path: Path, stage: str) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total), 0) FROM simulations WHERE stage = ?",
            (stage,),
        ).fetchone()
    return int(row[0]) if row else 0


def _run_with_timeout(run_dir: Path, command: list[str], timeout_seconds: int, log_file: Path) -> tuple[float, bool, int | None]:
    full_cmd = [sys.executable] + command
    child_env = os.environ.copy()
    # Rich title output contains Unicode characters; force UTF-8 for child stdout/stderr.
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    start = time.perf_counter()

    with log_file.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            full_cmd,
            cwd=str(run_dir),
            env=child_env,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
        )

        timed_out = False
        exit_code: int | None = None

        try:
            proc.wait(timeout=timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                proc.wait(timeout=20)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                exit_code = proc.returncode

    elapsed = time.perf_counter() - start
    return elapsed, timed_out, exit_code


def _rank_and_print(results: list[RunResult], scenario: str) -> None:
    scoped = [r for r in results if r.scenario == scenario]
    if not scoped:
        return

    grouped: dict[str, list[RunResult]] = {}
    for item in scoped:
        grouped.setdefault(item.candidate, []).append(item)

    aggregates: list[tuple[str, float, float, str]] = []
    for candidate, items in grouped.items():
        metrics = [x.metric_value for x in items]
        avg = sum(metrics) / len(metrics)
        best = max(metrics)
        metric_name = items[0].metric_name
        aggregates.append((candidate, avg, best, metric_name))

    aggregates.sort(key=lambda x: x[1], reverse=True)

    print(f"\n=== {scenario.upper()} ranking ===")
    for idx, (candidate, avg, best, metric_name) in enumerate(aggregates, start=1):
        print(f"{idx}. {candidate:28s} avg_{metric_name}={avg:.3f} best={best:.3f}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark original/fast/tunable producer scripts.")

    parser.add_argument("--mode", choices=["fill", "refine", "both"], default="both")
    parser.add_argument("--stage", default="River")
    parser.add_argument("--repeats", type=int, default=1)

    parser.add_argument("--fill-duration", type=int, default=90)
    parser.add_argument("--refine-duration", type=int, default=90)
    parser.add_argument("--refine-seed-rows", type=int, default=2000)

    parser.add_argument("--include-original", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-fast", action=argparse.BooleanOptionalAction, default=True)

    parser.add_argument("--tunable-workers", default="1")
    parser.add_argument("--tunable-checkpoint-every", default="1000")
    parser.add_argument("--tunable-batch-iterations", default="1000")
    parser.add_argument("--tunable-export-every-seconds", default="300")
    parser.add_argument("--tunable-refine-parallel-batch", default="0")

    parser.add_argument("--runs-dir", default="benchmark_runs")
    parser.add_argument("--keep-runs", action="store_true")
    parser.add_argument("--results-json", default="benchmark_results.json")

    return parser


def main() -> None:
    args = _build_parser().parse_args()

    if args.repeats < 1:
        raise ValueError("--repeats must be >= 1")
    if args.fill_duration < 1:
        raise ValueError("--fill-duration must be >= 1")
    if args.refine_duration < 1:
        raise ValueError("--refine-duration must be >= 1")
    if args.refine_seed_rows < 1:
        raise ValueError("--refine-seed-rows must be >= 1")

    project_root = Path(__file__).resolve().parent
    runs_root = project_root / args.runs_dir
    runs_root.mkdir(parents=True, exist_ok=True)

    candidates = _build_candidates(args)
    if not candidates:
        raise RuntimeError("No candidates selected for benchmark.")

    all_results: list[RunResult] = []
    selected_scenarios: list[str] = []
    if args.mode in {"fill", "both"}:
        selected_scenarios.append("fill")
    if args.mode in {"refine", "both"}:
        selected_scenarios.append("refine")

    print("Candidates:")
    for c in candidates:
        print(f"- {c.name}: {' '.join(c.command)}")

    for scenario in selected_scenarios:
        for repeat in range(1, args.repeats + 1):
            for candidate in candidates:
                run_id = f"{scenario}_{candidate.name}_r{repeat}_{int(time.time())}"
                run_dir = runs_root / run_id
                _copy_project_files(project_root, run_dir)

                db_path = run_dir / "poker_oracle.db"
                checkpoint_path = run_dir / candidate.checkpoint_file

                if scenario == "fill":
                    _write_checkpoint(checkpoint_path, phase="fill")
                    before = _db_row_count(db_path, args.stage)
                    elapsed, timed_out, exit_code = _run_with_timeout(
                        run_dir,
                        candidate.command,
                        args.fill_duration,
                        run_dir / "benchmark.log",
                    )
                    after = _db_row_count(db_path, args.stage)
                    delta_rows = max(0, after - before)
                    rows_per_sec = delta_rows / elapsed if elapsed > 0 else 0.0

                    all_results.append(
                        RunResult(
                            scenario=scenario,
                            candidate=candidate.name,
                            repeat=repeat,
                            elapsed_seconds=elapsed,
                            timed_out=timed_out,
                            exit_code=exit_code,
                            metric_name="rows_per_sec",
                            metric_value=rows_per_sec,
                            extra={
                                "rows_before": before,
                                "rows_after": after,
                                "rows_added": delta_rows,
                            },
                            log_file=str((run_dir / "benchmark.log").resolve()),
                        )
                    )

                if scenario == "refine":
                    _seed_refine_db(db_path, args.stage, args.refine_seed_rows)
                    _write_checkpoint(checkpoint_path, phase="refine")

                    before_total = _db_total_sum(db_path, args.stage)
                    elapsed, timed_out, exit_code = _run_with_timeout(
                        run_dir,
                        candidate.command,
                        args.refine_duration,
                        run_dir / "benchmark.log",
                    )
                    after_total = _db_total_sum(db_path, args.stage)

                    delta_total = max(0, after_total - before_total)
                    iters_per_sec = delta_total / elapsed if elapsed > 0 else 0.0
                    cycles = delta_total / max(1, candidate.batch_iterations)
                    cycles_per_sec = cycles / elapsed if elapsed > 0 else 0.0

                    all_results.append(
                        RunResult(
                            scenario=scenario,
                            candidate=candidate.name,
                            repeat=repeat,
                            elapsed_seconds=elapsed,
                            timed_out=timed_out,
                            exit_code=exit_code,
                            metric_name="iters_per_sec",
                            metric_value=iters_per_sec,
                            extra={
                                "total_before": before_total,
                                "total_after": after_total,
                                "total_delta": delta_total,
                                "cycles_estimated": cycles,
                                "cycles_per_sec": cycles_per_sec,
                            },
                            log_file=str((run_dir / "benchmark.log").resolve()),
                        )
                    )

                print(
                    f"[{scenario}] {candidate.name} repeat={repeat} elapsed={elapsed:.2f}s "
                    f"timed_out={timed_out} exit={exit_code}"
                )

                if not args.keep_runs:
                    shutil.rmtree(run_dir, ignore_errors=True)

    _rank_and_print(all_results, "fill")
    _rank_and_print(all_results, "refine")

    json_ready = [
        {
            "scenario": r.scenario,
            "candidate": r.candidate,
            "repeat": r.repeat,
            "elapsed_seconds": round(r.elapsed_seconds, 6),
            "timed_out": r.timed_out,
            "exit_code": r.exit_code,
            "metric_name": r.metric_name,
            "metric_value": r.metric_value,
            "extra": r.extra,
            "log_file": r.log_file,
        }
        for r in all_results
    ]

    output_file = Path(args.results_json)
    output_file.write_text(json.dumps(json_ready, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nResults written to: {output_file.resolve()}")


if __name__ == "__main__":
    main()
