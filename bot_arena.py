from __future__ import annotations

import math
import sys
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

        # Avoid noisy ETA at startup until we have enough signal.
        if self.updates_with_progress < 3 or rate_blend <= 1e-9:
            eta = None
        else:
            eta = remaining / rate_blend

        return f"{_format_hms(elapsed)}/{_format_hms(eta)}"


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

    runs = _ask_positive_int("Runs (to reduce variance)", default=100)
    hands_per_match = _ask_positive_int("Hands per match", default=200)
    equity_iterations = _ask_positive_int("Equity estimation iterations", default=1000)

    entries = [(key, create_strategy(key)) for key in deduped]
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

    _render_tournament(rows, hands_per_match)

    league_store.record_tournament(rows, runs=runs)
    cumulative_rows = league_store.load_leaderboard(strategy_keys=deduped)
    _render_cumulative(cumulative_rows)


def main() -> None:
    ui.display_title()
    ui.console.rule("[bold cyan]Bot Strategy Arena[/bold cyan]")
    ui.console.print(
        "[dim]Compare strategies quickly, run round-robin, and optionally play as Human.[/dim]"
    )

    league_store.init_store()

    while True:
        choice = Prompt.ask(
            "Mode",
            choices=["list", "bot", "rr", "human", "quit"],
            default="list",
        )

        if choice == "list":
            _show_strategies(include_human=True)
        elif choice == "bot":
            _run_bot_vs_bot()
        elif choice == "rr":
            _run_round_robin()
        elif choice == "human":
            _run_human_vs_bot()
        elif choice == "quit":
            break

        if not Confirm.ask("Run another arena action?", default=True):
            break

    ui.console.print("[green]Bye from Bot Strategy Arena.[/green]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
