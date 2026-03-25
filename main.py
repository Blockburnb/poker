"""
main.py
-------
PokerOracle – Texas Hold'em Monte Carlo Equity Simulator

Entry point: drives the interactive TUI session, ties together the simulation
engine (simulator.py), the persistence layer (db.py) and the display layer
(ui.py).

Usage:
    python main.py
"""

from __future__ import annotations

import sys

from rich.prompt import Confirm

import db
import simulator as sim
import ui


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hand_key(hand: list[int]) -> str:
    """Canonical, sorted string representation of hole cards used as DB key."""
    return " ".join(sorted(sim.cards_to_str([c]).strip() for c in hand))


def _community_key(community_cards: list[int]) -> str:
    """String representation of community cards (order preserved) for DB key."""
    return " ".join(sim.cards_to_str([c]).strip() for c in community_cards)


def _run_stage(
    stage_name: str,
    hand: list[int],
    community_cards: list[int],
    num_opponents: int,
    num_iterations: int,
) -> None:
    """Run one simulation stage, persist results and display them."""
    hand_key = _hand_key(hand)
    comm_key = _community_key(community_cards)

    # Check for prior data
    prior_row = db.fetch_existing(hand_key, stage_name, comm_key, num_opponents)
    prior_dict: dict | None = None
    if prior_row:
        total = prior_row["total"] or 1
        prior_dict = {
            "equity": (prior_row["wins"] + prior_row["ties"] * 0.5) / total,
            "win_rate": prior_row["wins"] / total,
            "tie_rate": prior_row["ties"] / total,
            "total": prior_row["total"],
        }
        ui.console.print(
            f"[dim]Found existing data for this scenario "
            f"({prior_row['total']:,} iterations). "
            f"New results will be merged.[/dim]"
        )

    # Simulate
    result = ui.run_simulation_with_progress(
        sim.simulate, hand, community_cards, num_opponents, num_iterations
    )

    # Persist (merge)
    db.save_or_update(
        hand_key,
        stage_name,
        comm_key,
        num_opponents,
        result["wins"],
        result["ties"],
        result["losses"],
        result["total"],
    )

    # Display
    ui.display_results(stage_name, hand, community_cards, result, num_opponents, prior_dict)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def main() -> None:
    db.init_db()
    ui.display_title()

    # ── Optional: show history ─────────────────────────────────────────────
    if Confirm.ask(
        "[dim]Show previously stored simulations?[/dim]", default=False
    ):
        ui.display_history(db.fetch_all())

    ui.console.rule("[yellow]New Simulation[/yellow]")

    # ── Number of opponents ────────────────────────────────────────────────
    num_opponents = ui.ask_int(
        "Number of opponents (1–9)", min_val=1, max_val=9, default=1
    )

    # ── Number of iterations ──────────────────────────────────────────────
    num_iterations = ui.ask_int(
        "Monte Carlo iterations (1 000–1 000 000)",
        min_val=1_000,
        max_val=1_000_000,
        default=50_000,
    )

    used_cards: list[int] = []

    # ── Player's hole cards ────────────────────────────────────────────────
    hand = ui.ask_cards(
        "Enter your 2 hole cards (e.g. Ah Kd)", expected=2, already_used=used_cards
    )
    used_cards.extend(hand)

    # ── Pre-Flop ──────────────────────────────────────────────────────────
    _run_stage("Pre-Flop", hand, [], num_opponents, num_iterations)

    # ── Flop ──────────────────────────────────────────────────────────────
    if not Confirm.ask("\nContinue to [bold]Flop[/bold]?", default=True):
        _finish()
        return

    flop = ui.ask_cards(
        "Enter the 3 Flop cards (e.g. 2h 7d Qc)",
        expected=3,
        already_used=used_cards,
    )
    used_cards.extend(flop)
    community = list(flop)
    _run_stage("Flop", hand, community, num_opponents, num_iterations)

    # ── Turn ──────────────────────────────────────────────────────────────
    if not Confirm.ask("\nContinue to [bold]Turn[/bold]?", default=True):
        _finish()
        return

    turn = ui.ask_cards(
        "Enter the Turn card (e.g. Jc)", expected=1, already_used=used_cards
    )
    used_cards.extend(turn)
    community = community + turn
    _run_stage("Turn", hand, community, num_opponents, num_iterations)

    # ── River ─────────────────────────────────────────────────────────────
    if not Confirm.ask("\nContinue to [bold]River[/bold]?", default=True):
        _finish()
        return

    river = ui.ask_cards(
        "Enter the River card (e.g. 5s)", expected=1, already_used=used_cards
    )
    used_cards.extend(river)
    community = community + river
    _run_stage("River", hand, community, num_opponents, num_iterations)

    _finish()


def _finish() -> None:
    ui.console.rule()
    ui.console.print(
        "[bold green]Thanks for using PokerOracle! Good luck at the tables 🃏[/bold green]"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
