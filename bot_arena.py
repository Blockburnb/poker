from __future__ import annotations

import math
import sys
import threading
import time
from dataclasses import dataclass

from rich.prompt import Confirm, IntPrompt, Prompt
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

import arena
import league_store
import ui
from bots.registry import create_strategy, list_strategies, strategy_keys


def _format_hms(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--:--:--"
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


@dataclass
class _AdaptiveEtaEstimator:
    start_ts: float
    last_ts: float
    last_completed: int = 0
    updates_with_progress: int = 0
    rate_fast: float = 0.0
    rate_slow: float = 0.0
    volatility_ewma: float = 0.0

    @classmethod
    def new(cls) -> "_AdaptiveEtaEstimator":
        now = time.perf_counter()
        return cls(start_ts=now, last_ts=now)

    def observe(self, completed: int, total: int) -> str:
        now = time.perf_counter()
        dt = max(1e-6, now - self.last_ts)
        dc = max(0, completed - self.last_completed)

        if dc > 0:
            self.updates_with_progress += 1
            inst_rate = dc / dt

            if self.rate_fast <= 0 or self.rate_slow <= 0:
                self.rate_fast = inst_rate
                self.rate_slow = inst_rate
                self.volatility_ewma = 0.0
            else:
                # Two time-scales: fast reacts quickly, slow stabilizes baseline.
                self.rate_fast = 0.35 * inst_rate + 0.65 * self.rate_fast
                self.rate_slow = 0.10 * inst_rate + 0.90 * self.rate_slow

                # Volatility is the normalized divergence between fast and slow trends.
                rel_gap = abs(self.rate_fast - self.rate_slow) / max(self.rate_slow, 1e-9)
                self.volatility_ewma = 0.20 * rel_gap + 0.80 * self.volatility_ewma

        self.last_ts = now
        self.last_completed = completed

        elapsed = now - self.start_ts
        remaining = max(0, total - completed)

        if self.rate_fast > 1e-9 and self.rate_slow > 1e-9:
            # In stable periods favor slow trend; when resources shift, favor fast trend.
            fast_weight = min(0.70, max(0.20, 0.20 + 0.80 * self.volatility_ewma))
            rate_blend = fast_weight * self.rate_fast + (1.0 - fast_weight) * self.rate_slow
        else:
            rate_blend = 0.0

        # Avoid noisy estimation at startup until we have enough signal.
        if self.updates_with_progress < 3 or rate_blend <= 1e-9:
            total_estimated = None
        else:
            eta = remaining / rate_blend
            total_estimated = elapsed + eta

        if completed >= total:
            total_estimated = elapsed

        return f"{_format_hms(elapsed)}/{_format_hms(total_estimated)}"


class _GracefulStopSignal:
    def __init__(self, stop_word: str = "stop") -> None:
        self.stop_word = stop_word.strip().lower()
        self._event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return

        ui.console.print(
            f"[dim]Continuous mode: tape '{self.stop_word}' puis Entrée pour arrêter proprement et sauvegarder.[/dim]"
        )

        def _reader() -> None:
            while not self._event.is_set():
                line = sys.stdin.readline()
                if not line:
                    break
                if line.strip().lower() == self.stop_word:
                    self._event.set()
                    break

        self._thread = threading.Thread(target=_reader, daemon=True)
        self._thread.start()

    def is_set(self) -> bool:
        return self._event.is_set()


def _merge_tournament_rows(rows: list[arena.TournamentRow], merged: dict[str, dict[str, int | str]]) -> None:
    for row in rows:
        bucket = merged.setdefault(
            row.strategy_key,
            {
                "strategy_name": row.strategy_name,
                "matches": 0,
                "hands": 0,
                "total_profit": 0,
            },
        )
        bucket["strategy_name"] = row.strategy_name
        bucket["matches"] = int(bucket["matches"]) + row.matches
        bucket["hands"] = int(bucket["hands"]) + row.hands
        bucket["total_profit"] = int(bucket["total_profit"]) + row.total_profit


def _build_tournament_rows(merged: dict[str, dict[str, int | str]]) -> list[arena.TournamentRow]:
    out: list[arena.TournamentRow] = []
    for key, row in merged.items():
        hands = int(row["hands"])
        profit = int(row["total_profit"])
        out.append(
            arena.TournamentRow(
                strategy_key=key,
                strategy_name=str(row["strategy_name"]),
                matches=int(row["matches"]),
                hands=hands,
                total_profit=profit,
                avg_profit_per_100=(profit / hands) * 100 if hands else 0.0,
            )
        )
    out.sort(key=lambda r: r.avg_profit_per_100, reverse=True)
    return out


def _merge_field_rows(rows: list[arena.FieldComparisonRow], merged: dict[str, dict[str, int | str | float]]) -> None:
    for row in rows:
        bucket = merged.setdefault(
            row.strategy_key,
            {
                "strategy_name": row.strategy_name,
                "tables_played": 0,
                "seat_appearances": 0,
                "hands": 0,
                "showdowns": 0,
                "total_profit": 0,
                "table_size_weighted_sum": 0.0,
            },
        )
        bucket["strategy_name"] = row.strategy_name
        bucket["tables_played"] = int(bucket["tables_played"]) + row.tables_played
        bucket["seat_appearances"] = int(bucket["seat_appearances"]) + row.seat_appearances
        bucket["hands"] = int(bucket["hands"]) + row.hands
        bucket["showdowns"] = int(bucket["showdowns"]) + row.showdowns
        bucket["total_profit"] = int(bucket["total_profit"]) + row.total_profit
        bucket["table_size_weighted_sum"] = float(bucket["table_size_weighted_sum"]) + (
            row.avg_table_size * row.seat_appearances
        )


def _build_field_rows(merged: dict[str, dict[str, int | str | float]]) -> list[arena.FieldComparisonRow]:
    out: list[arena.FieldComparisonRow] = []
    for key, row in merged.items():
        hands = int(row["hands"])
        profit = int(row["total_profit"])
        seats = int(row["seat_appearances"])
        weighted = float(row["table_size_weighted_sum"])
        out.append(
            arena.FieldComparisonRow(
                strategy_key=key,
                strategy_name=str(row["strategy_name"]),
                tables_played=int(row["tables_played"]),
                seat_appearances=seats,
                hands=hands,
                showdowns=int(row["showdowns"]),
                total_profit=profit,
                avg_profit_per_100=(profit / hands) * 100 if hands else 0.0,
                avg_table_size=(weighted / seats) if seats else 0.0,
            )
        )
    out.sort(key=lambda r: (r.hands > 0, r.avg_profit_per_100), reverse=True)
    return out


def _ask_positive_int(label: str, default: int, min_value: int = 1) -> int:
    while True:
        value = IntPrompt.ask(label, default=default, show_default=True)
        if value >= min_value:
            return value
        ui.console.print(f"[red]Value must be >= {min_value}.[/red]")


def _show_strategies(include_human: bool = True) -> None:
    table = Table(title="Available Strategies", header_style="bold cyan")
    table.add_column("Key", style="yellow")
    table.add_column("Name", style="bold")
    table.add_column("Summary")
    table.add_column("Tags", style="dim")

    for factory in list_strategies(include_human=include_human):
        tags = ", ".join(factory.info.tags)
        table.add_row(factory.info.key, factory.info.name, factory.info.summary, tags)

    ui.console.print(table)
    ui.console.print(
        "[dim]Tip: add exported GTO policy JSON files in 'gto_policies/' to auto-register GTO strategies.[/dim]"
    )


def _pick_strategy(prompt_text: str, include_human: bool) -> str:
    keys = strategy_keys(include_human=include_human)
    return Prompt.ask(prompt_text, choices=keys, default=keys[0])


def _render_match(result: arena.MatchResult) -> None:
    table = Table(title=f"Match: {result.hero_name} vs {result.villain_name}", header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")

    table.add_row("Hands", f"{result.hands:,}")
    table.add_row(f"{result.hero_name} profit", f"{result.hero_profit:+,}")
    table.add_row(f"{result.villain_name} profit", f"{result.villain_profit:+,}")
    table.add_row("Showdowns", f"{result.showdowns:,}")
    table.add_row("Ties", f"{result.ties:,}")
    table.add_row(f"{result.hero_name} play frequency", f"{(result.hero_plays / result.hands) * 100:.1f}%")
    table.add_row(f"{result.villain_name} play frequency", f"{(result.villain_plays / result.hands) * 100:.1f}%")

    ui.console.print(table)


def _render_tournament(rows: list[arena.TournamentRow], hands_per_match: int) -> None:
    table = Table(
        title=f"Round-Robin Ranking ({hands_per_match} hands/match)",
        header_style="bold magenta",
    )
    table.add_column("Rank", justify="right")
    table.add_column("Key", style="yellow")
    table.add_column("Strategy")
    table.add_column("Matches", justify="right")
    table.add_column("Hands", justify="right")
    table.add_column("Total Profit", justify="right")
    table.add_column("Profit/100", justify="right")

    for idx, row in enumerate(rows, start=1):
        color = "green" if idx == 1 else "white"
        table.add_row(
            str(idx),
            row.strategy_key,
            row.strategy_name,
            str(row.matches),
            f"{row.hands:,}",
            f"[{color}]{row.total_profit:+,}[/{color}]",
            f"[{color}]{row.avg_profit_per_100:+.3f}[/{color}]",
        )

    ui.console.print(table)


def _render_field_comparison(rows: list[arena.FieldComparisonRow], hands_per_table: int) -> None:
    table = Table(
        title=(
            "Field Comparison Ranking "
            f"(randomized field, 2-10 players, {hands_per_table} hands per table)"
        ),
        header_style="bold blue",
    )
    table.add_column("Rank", justify="right")
    table.add_column("Key", style="yellow")
    table.add_column("Strategy")
    table.add_column("Tables", justify="right")
    table.add_column("Seat Entries", justify="right")
    table.add_column("Avg Table Size", justify="right")
    table.add_column("Showdowns", justify="right")
    table.add_column("Hands", justify="right")
    table.add_column("Total Profit", justify="right")
    table.add_column("Profit/100", justify="right")

    for idx, row in enumerate(rows, start=1):
        color = "green" if idx == 1 else "white"
        table.add_row(
            str(idx),
            row.strategy_key,
            row.strategy_name,
            str(row.tables_played),
            str(row.seat_appearances),
            f"{row.avg_table_size:.2f}",
            str(row.showdowns),
            f"{row.hands:,}",
            f"[{color}]{row.total_profit:+,}[/{color}]",
            f"[{color}]{row.avg_profit_per_100:+.3f}[/{color}]",
        )

    ui.console.print(table)


def _render_field_cumulative(rows: list[league_store.FieldLeagueRow]) -> None:
    if not rows:
        ui.console.print("[dim]No cumulative field data yet.[/dim]")
        return

    table = Table(title="Cumulative Field Ranking", header_style="bold blue")
    table.add_column("Rank", justify="right")
    table.add_column("Key", style="yellow")
    table.add_column("Strategy")
    table.add_column("Simulations", justify="right")
    table.add_column("Tables", justify="right")
    table.add_column("Seat Entries", justify="right")
    table.add_column("Hands", justify="right")
    table.add_column("Total Profit", justify="right")
    table.add_column("Profit/100", justify="right")

    for idx, row in enumerate(rows, start=1):
        color = "green" if idx == 1 else "white"
        table.add_row(
            str(idx),
            row.strategy_key,
            row.strategy_name,
            str(row.simulations),
            str(row.tables),
            str(row.seat_appearances),
            f"{row.hands:,}",
            f"[{color}]{row.total_profit:+,}[/{color}]",
            f"[{color}]{row.avg_profit_per_100:+.3f}[/{color}]",
        )

    ui.console.print(table)


def _render_cumulative(rows: list[league_store.LeagueRow]) -> None:
    if not rows:
        ui.console.print("[dim]No cumulative league data yet.[/dim]")
        return

    table = Table(title="Cumulative League Ranking", header_style="bold green")
    table.add_column("Rank", justify="right")
    table.add_column("Key", style="yellow")
    table.add_column("Strategy")
    table.add_column("Tournaments", justify="right")
    table.add_column("Matches", justify="right")
    table.add_column("Hands", justify="right")
    table.add_column("Total Profit", justify="right")
    table.add_column("Profit/100", justify="right")

    for idx, row in enumerate(rows, start=1):
        color = "green" if idx == 1 else "white"
        table.add_row(
            str(idx),
            row.strategy_key,
            row.strategy_name,
            str(row.tournaments),
            str(row.matches),
            f"{row.hands:,}",
            f"[{color}]{row.total_profit:+,}[/{color}]",
            f"[{color}]{row.avg_profit_per_100:+.3f}[/{color}]",
        )

    ui.console.print(table)


def _run_bot_vs_bot() -> None:
    ui.console.rule("[yellow]Bot vs Bot[/yellow]")
    _show_strategies(include_human=False)

    hero_key = _pick_strategy("Hero strategy", include_human=False)
    villain_key = _pick_strategy("Villain strategy", include_human=False)

    hands = _ask_positive_int("Hands to play", default=300)
    equity_iterations = _ask_positive_int("Equity estimation iterations", default=1000)

    hero = create_strategy(hero_key)
    villain = create_strategy(villain_key)

    result = arena.run_heads_up_match(
        hero,
        villain,
        hands=hands,
        equity_iterations=equity_iterations,
        seed=42,
        keep_hand_logs=False,
    )
    _render_match(result)


def _run_human_vs_bot() -> None:
    ui.console.rule("[yellow]Human vs Bot[/yellow]")
    _show_strategies(include_human=False)

    bot_key = _pick_strategy("Choose opponent bot", include_human=False)
    hands = _ask_positive_int("Hands to play", default=20)
    equity_iterations = _ask_positive_int("Equity estimation iterations", default=1000)

    human = create_strategy("human")
    bot = create_strategy(bot_key)

    result = arena.run_heads_up_match(
        human,
        bot,
        hands=hands,
        equity_iterations=equity_iterations,
        seed=99,
        keep_hand_logs=False,
    )
    _render_match(result)


def _run_round_robin() -> None:
    ui.console.rule("[yellow]Round-Robin[/yellow]")
    _show_strategies(include_human=False)

    defaults = [k for k in strategy_keys(include_human=False)]
    raw = Prompt.ask(
        "Strategies to include (comma-separated keys)",
        default=", ".join(defaults),
    )
    selected = [s.strip() for s in raw.split(",") if s.strip()]
    deduped = []
    for key in selected:
        if key not in deduped:
            deduped.append(key)

    if len(deduped) < 2:
        ui.console.print("[red]Select at least 2 strategies.[/red]")
        return

    exec_mode = Prompt.ask(
        "Execution mode",
        choices=["finite", "continuous"],
        default="finite",
    )

    hands_per_match = _ask_positive_int("Hands per match", default=200)
    equity_iterations = _ask_positive_int("Equity estimation iterations", default=1000)

    entries = [(key, create_strategy(key)) for key in deduped]
    if exec_mode == "finite":
        runs = _ask_positive_int("Runs (to reduce variance)", default=100)
        matchups = math.comb(len(entries), 2)
        total_hands = runs * matchups * hands_per_match
        runs_completed = 0
        hands_completed = 0
        runs_eta = _AdaptiveEtaEstimator.new()
        hands_eta = _AdaptiveEtaEstimator.new()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed:,}/{task.total:,}"),
            TextColumn("{task.fields[clock]}"),
            console=ui.console,
            transient=True,
        ) as progress:
            runs_task = progress.add_task(
                "Rounds (runs)",
                total=runs,
                completed=0,
                clock=f"{_format_hms(0)}/--:--:--",
            )
            hands_task = progress.add_task(
                "Hands played",
                total=max(1, total_hands),
                completed=0,
                clock=f"{_format_hms(0)}/--:--:--",
            )

            def _on_hand_progress(delta: int) -> None:
                nonlocal hands_completed
                hands_completed += delta
                clock = hands_eta.observe(hands_completed, max(1, total_hands))
                progress.update(hands_task, completed=hands_completed, clock=clock)

            def _on_run_progress(delta: int) -> None:
                nonlocal runs_completed
                runs_completed += delta
                clock = runs_eta.observe(runs_completed, runs)
                progress.update(runs_task, completed=runs_completed, clock=clock)

            rows = arena.run_round_robin_series(
                entries,
                runs=runs,
                hands_per_match=hands_per_match,
                equity_iterations=equity_iterations,
                seed=123,
                hand_progress_callback=_on_hand_progress,
                run_progress_callback=_on_run_progress,
            )

        completed_runs_for_save = runs
    else:
        stop_signal = _GracefulStopSignal(stop_word="stop")
        stop_signal.start()

        start_ts = time.perf_counter()
        total_runs = 0
        hands_completed = 0
        merged: dict[str, dict[str, int | str]] = {}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed:,}"),
            TextColumn("{task.fields[clock]}"),
            console=ui.console,
            transient=True,
        ) as progress:
            runs_task = progress.add_task("Runs", total=None, completed=0, clock=f"{_format_hms(0)}/--:--:--")
            hands_task = progress.add_task("Hands played", total=None, completed=0, clock=f"{_format_hms(0)}/--:--:--")

            def _on_hand_progress(delta: int) -> None:
                nonlocal hands_completed
                hands_completed += delta
                elapsed = time.perf_counter() - start_ts
                clock = f"{_format_hms(elapsed)}/--:--:--"
                progress.update(hands_task, completed=hands_completed, clock=clock)

            while not stop_signal.is_set():
                run_rows = arena.run_round_robin(
                    entries,
                    hands_per_match=hands_per_match,
                    equity_iterations=equity_iterations,
                    seed=123 + total_runs,
                    hand_progress_callback=_on_hand_progress,
                )
                _merge_tournament_rows(run_rows, merged)
                total_runs += 1
                elapsed = time.perf_counter() - start_ts
                clock = f"{_format_hms(elapsed)}/--:--:--"
                progress.update(runs_task, completed=total_runs, clock=clock)

        rows = _build_tournament_rows(merged)
        completed_runs_for_save = total_runs
        ui.console.print(f"[cyan]Continuous RR stopped after {total_runs} run(s).[/cyan]")

    _render_tournament(rows, hands_per_match)

    league_store.record_tournament(rows, runs=max(1, completed_runs_for_save))
    cumulative_rows = league_store.load_leaderboard(strategy_keys=deduped)
    _render_cumulative(cumulative_rows)


def _run_field_comparison() -> None:
    ui.console.rule("[yellow]Field Comparison[/yellow]")
    _show_strategies(include_human=False)

    defaults = [k for k in strategy_keys(include_human=False)]
    raw = Prompt.ask(
        "Strategies to include (comma-separated keys)",
        default=", ".join(defaults),
    )
    selected = [s.strip() for s in raw.split(",") if s.strip()]
    deduped = []
    for key in selected:
        if key not in deduped:
            deduped.append(key)

    if len(deduped) < 2:
        ui.console.print("[red]Select at least 2 strategies.[/red]")
        return

    exec_mode = Prompt.ask(
        "Execution mode",
        choices=["finite", "continuous"],
        default="finite",
    )

    table_size_min = _ask_positive_int("Min players per table (>=2)", default=2, min_value=2)
    table_size_max = _ask_positive_int("Max players per table (<=10)", default=10, min_value=table_size_min)
    if table_size_max > 10:
        ui.console.print("[red]Max players must be <= 10.[/red]")
        return

    hands_per_table = _ask_positive_int("Hands per table", default=120)
    equity_iterations = _ask_positive_int("Equity estimation iterations", default=1000)

    if exec_mode == "finite":
        tables = _ask_positive_int("Random tables to simulate", default=300)

        # Each table gets random seats with replacement (same strategy can appear multiple times).
        # Hands are simulated as true multiway pots on a shared board.
        avg_table_size = (table_size_min + table_size_max) / 2.0
        approx_total_hands = max(1, int(round(tables * avg_table_size * hands_per_table)))

        tables_completed = 0
        hands_completed = 0
        tables_eta = _AdaptiveEtaEstimator.new()
        hands_eta = _AdaptiveEtaEstimator.new()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed:,}/{task.total:,}"),
            TextColumn("{task.fields[clock]}"),
            console=ui.console,
            transient=True,
        ) as progress:
            tables_task = progress.add_task(
                "Random tables",
                total=tables,
                completed=0,
                clock=f"{_format_hms(0)}/--:--:--",
            )
            hands_task = progress.add_task(
                "Hands played (approx target)",
                total=approx_total_hands,
                completed=0,
                clock=f"{_format_hms(0)}/--:--:--",
            )

            def _on_hand_progress(delta: int) -> None:
                nonlocal hands_completed
                hands_completed += delta
                shown_completed = min(hands_completed, approx_total_hands)
                clock = hands_eta.observe(shown_completed, approx_total_hands)
                progress.update(hands_task, completed=shown_completed, clock=clock)

            def _on_table_progress(delta: int) -> None:
                nonlocal tables_completed
                tables_completed += delta
                clock = tables_eta.observe(tables_completed, tables)
                progress.update(tables_task, completed=tables_completed, clock=clock)

            rows = arena.run_field_comparison_series(
                deduped,
                create_strategy_fn=create_strategy,
                tables=tables,
                table_size_min=table_size_min,
                table_size_max=table_size_max,
                hands_per_table=hands_per_table,
                equity_iterations=equity_iterations,
                seed=321,
                hand_progress_callback=_on_hand_progress,
                table_progress_callback=_on_table_progress,
            )
        completed_tables_for_save = tables
    else:
        tables_batch = _ask_positive_int("Tables per cycle (granularity of stop)", default=5)
        stop_signal = _GracefulStopSignal(stop_word="stop")
        stop_signal.start()

        start_ts = time.perf_counter()
        total_tables = 0
        hands_completed = 0
        merged: dict[str, dict[str, int | str | float]] = {}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed:,}"),
            TextColumn("{task.fields[clock]}"),
            console=ui.console,
            transient=True,
        ) as progress:
            tables_task = progress.add_task("Random tables", total=None, completed=0, clock=f"{_format_hms(0)}/--:--:--")
            hands_task = progress.add_task("Hands played", total=None, completed=0, clock=f"{_format_hms(0)}/--:--:--")

            def _on_hand_progress(delta: int) -> None:
                nonlocal hands_completed
                hands_completed += delta
                elapsed = time.perf_counter() - start_ts
                progress.update(hands_task, completed=hands_completed, clock=f"{_format_hms(elapsed)}/--:--:--")

            while not stop_signal.is_set():
                batch_rows = arena.run_field_comparison_series(
                    deduped,
                    create_strategy_fn=create_strategy,
                    tables=tables_batch,
                    table_size_min=table_size_min,
                    table_size_max=table_size_max,
                    hands_per_table=hands_per_table,
                    equity_iterations=equity_iterations,
                    seed=321 + total_tables,
                    hand_progress_callback=_on_hand_progress,
                    table_progress_callback=None,
                )
                _merge_field_rows(batch_rows, merged)
                total_tables += tables_batch
                elapsed = time.perf_counter() - start_ts
                progress.update(tables_task, completed=total_tables, clock=f"{_format_hms(elapsed)}/--:--:--")

        rows = _build_field_rows(merged)
        completed_tables_for_save = total_tables
        ui.console.print(f"[cyan]Continuous Field stopped after {total_tables} table(s).[/cyan]")

    _render_field_comparison(rows, hands_per_table=hands_per_table)
    league_store.record_field_tournament(rows, simulations=max(1, completed_tables_for_save))
    cumulative_rows = league_store.load_field_leaderboard(strategy_keys=deduped)
    _render_field_cumulative(cumulative_rows)
    ui.console.print(
        "[dim]Note: field ranking is stored separately from round-robin in bot_league_field.json.[/dim]"
    )


def run_arena_mode(show_title: bool = True) -> None:
    if show_title:
        ui.display_title()
    ui.console.rule("[bold cyan]Bot Strategy Arena[/bold cyan]")
    ui.console.print(
        "[dim]Compare strategies quickly with round-robin or realistic field mode, and optionally play as Human.[/dim]"
    )

    league_store.init_store()
    league_store.init_field_store()

    while True:
        choice = Prompt.ask(
            "Mode",
            choices=["list", "bot", "rr", "field", "human", "quit"],
            default="list",
        )

        if choice == "list":
            _show_strategies(include_human=True)
        elif choice == "bot":
            _run_bot_vs_bot()
        elif choice == "rr":
            _run_round_robin()
        elif choice == "field":
            _run_field_comparison()
        elif choice == "human":
            _run_human_vs_bot()
        elif choice == "quit":
            break

        if not Confirm.ask("Run another arena action?", default=True):
            break

    ui.console.print("[green]Bye from Bot Strategy Arena.[/green]")


def main() -> None:
    run_arena_mode(show_title=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
