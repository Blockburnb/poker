"""
ui.py
-----
Rich-powered Terminal User Interface components for PokerOracle.
"""

from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich import box
from treys import Card

console = Console()

# Suit symbols and colours
_SUIT_STYLE = {"h": "red", "d": "red", "s": "white", "c": "green"}
_SUIT_SYMBOL = {"h": "♥", "d": "♦", "s": "♠", "c": "♣"}


def _card_text(card_int: int) -> Text:
    """Return a styled Rich Text object for a single treys card integer."""
    s = Card.int_to_str(card_int)  # e.g. "Ah"
    rank, suit = s[0], s[1]
    symbol = _SUIT_SYMBOL.get(suit, suit)
    style = _SUIT_STYLE.get(suit, "white")
    t = Text()
    t.append(f"[{rank}{symbol}]", style=f"bold {style}")
    return t


def _cards_rich(cards: List[int]) -> Text:
    """Join multiple _card_text objects with spaces."""
    result = Text()
    for i, c in enumerate(cards):
        if i:
            result.append(" ")
        result.append_text(_card_text(c))
    return result


# ---------------------------------------------------------------------------
# Public display functions
# ---------------------------------------------------------------------------


def display_title() -> None:
    """Print the PokerOracle banner."""
    console.print(
        Panel.fit(
            "[bold red]🃏  PokerOracle[/bold red]\n"
            "[dim]Texas Hold'em Monte Carlo Equity Simulator[/dim]",
            border_style="red",
            padding=(1, 4),
        )
    )


def display_results(
    stage: str,
    hand: List[int],
    community_cards: List[int],
    result: Dict,
    num_opponents: int,
    prior: Optional[Dict] = None,
) -> None:
    """
    Print a summary table for the current simulation results.

    Args:
        stage:           Poker stage name (e.g. "Pre-Flop").
        hand:            Player's 2 hole cards (treys ints).
        community_cards: Known community cards (treys ints).
        result:          Dict returned by simulator.simulate().
        num_opponents:   Number of opponents simulated.
        prior:           Optional previously stored result dict (for delta).
    """
    equity_pct = result["equity"] * 100
    win_pct = result["win_rate"] * 100
    tie_pct = result["tie_rate"] * 100

    # Equity bar  ─────────────────────────────────────────────────────────────
    bar_width = 40
    filled = int(equity_pct / 100 * bar_width)
    bar_colour = "green" if equity_pct >= 50 else "red"
    bar = Text()
    bar.append("█" * filled, style=f"bold {bar_colour}")
    bar.append("░" * (bar_width - filled), style="dim")
    bar.append(f"  {equity_pct:.1f}%", style="bold")

    # Build table  ─────────────────────────────────────────────────────────────
    table = Table(
        title=f"[bold yellow]Stage: {stage}[/bold yellow]",
        box=box.ROUNDED,
        border_style="yellow",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Metric", style="dim", width=22)
    table.add_column("Value", justify="right")

    hand_text = _cards_rich(hand)
    community_text = _cards_rich(community_cards) if community_cards else Text("–", style="dim")

    table.add_row("Your hand", hand_text)
    table.add_row("Community cards", community_text)
    table.add_row("Opponents", str(num_opponents))
    table.add_row("Iterations", f"{result['total']:,}")
    table.add_row("Equity bar", bar)
    equity_text = Text(f"{equity_pct:.2f}%", style=f"bold {'green' if equity_pct >= 50 else 'red'}")
    table.add_row(
        "Equity (win + ½ tie)",
        equity_text,
    )
    table.add_row("Win rate", f"{win_pct:.2f}%")
    table.add_row("Tie rate", f"{tie_pct:.2f}%")
    table.add_row(
        "Loss rate",
        f"{(1 - result['win_rate'] - result['tie_rate']) * 100:.2f}%",
    )

    if prior:
        prior_equity = prior["equity"] * 100
        delta = equity_pct - prior_equity
        delta_text = Text()
        delta_text.append(f"prior={prior_equity:.2f}% → now={equity_pct:.2f}%  (")
        if delta >= 0:
            delta_text.append(f"+{delta:.2f}%", style="green")
        else:
            delta_text.append(f"{delta:.2f}%", style="red")
        delta_text.append(")")
        table.add_row("Δ vs stored data", delta_text)

    console.print(table)


def display_history(rows: list) -> None:
    """Print all stored simulations from the database."""
    if not rows:
        console.print("[dim]No simulations stored yet.[/dim]")
        return

    table = Table(
        title="[bold cyan]Stored Simulations[/bold cyan]",
        box=box.SIMPLE_HEAVY,
        header_style="bold magenta",
    )
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Hand")
    table.add_column("Stage")
    table.add_column("Community")
    table.add_column("Opponents", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Equity", justify="right")
    table.add_column("Updated at", style="dim")

    for row in rows:
        total = row["total"] or 1
        equity = (row["wins"] + row["ties"] * 0.5) / total * 100
        colour = "green" if equity >= 50 else "red"
        equity_cell = Text(f"{equity:.1f}%", style=colour)
        table.add_row(
            str(row["id"]),
            row["hand"],
            row["stage"],
            row["community_cards"] or "–",
            str(row["num_opponents"]),
            f"{row['total']:,}",
            equity_cell,
            row["updated_at"],
        )

    console.print(table)


def run_simulation_with_progress(
    simulate_fn,
    hand: List[int],
    community_cards: List[int],
    num_opponents: int,
    num_iterations: int,
) -> Dict:
    """
    Run simulate_fn while displaying a Rich progress bar.

    simulate_fn must accept (hand, community_cards, num_opponents, iterations).
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(
            f"[cyan]Simulating {num_iterations:,} iterations…", total=num_iterations
        )

        # Run in one shot (treys is fast enough at typical iteration counts)
        result = simulate_fn(hand, community_cards, num_opponents, num_iterations)
        progress.update(task, advance=num_iterations)

    return result


def ask_cards(prompt_text: str, expected: int, already_used: List[int]) -> List[int]:
    """
    Interactively prompt the user to enter `expected` cards, validating each
    one and ensuring no duplicates with `already_used`.

    Returns a list of treys card integers.
    """
    from simulator import parse_card  # local import to avoid circular deps

    while True:
        raw = Prompt.ask(f"[bold]{prompt_text}[/bold]")
        parts = raw.strip().split()
        if len(parts) != expected:
            console.print(
                f"[red]Please enter exactly {expected} card(s) "
                f"separated by spaces (e.g. Ah Kd).[/red]"
            )
            continue

        parsed: List[int] = []
        error = False
        used_in_input: set = set()
        for p in parts:
            try:
                card = parse_card(p)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                error = True
                break
            if card in already_used:
                console.print(
                    f"[red]Card {p.upper()} has already been dealt.[/red]"
                )
                error = True
                break
            if card in used_in_input:
                console.print(
                    f"[red]Card {p.upper()} appears twice in your input.[/red]"
                )
                error = True
                break
            used_in_input.add(card)
            parsed.append(card)

        if not error:
            return parsed


def ask_int(prompt_text: str, min_val: int, max_val: int, default: int) -> int:
    """Prompt the user for an integer within [min_val, max_val]."""
    while True:
        raw = Prompt.ask(
            f"[bold]{prompt_text}[/bold]",
            default=str(default),
        )
        try:
            value = int(raw)
        except ValueError:
            console.print(f"[red]Please enter a whole number.[/red]")
            continue
        if not (min_val <= value <= max_val):
            console.print(
                f"[red]Value must be between {min_val} and {max_val}.[/red]"
            )
            continue
        return value
