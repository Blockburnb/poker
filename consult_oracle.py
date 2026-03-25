"""
consult_oracle.py
----------------
Read-only consultation script for already stored River results.
No Monte Carlo simulation is executed in this script.
"""

from __future__ import annotations

import sys

import db
import simulator as sim
import ui


def _hand_key(hand: list[int]) -> str:
    return " ".join(sorted(sim.cards_to_str([c]).strip() for c in hand))


def _community_key(community_cards: list[int]) -> str:
    return " ".join(sim.cards_to_str([c]).strip() for c in community_cards)


def main() -> None:
    db.init_db()
    imported = db.import_snapshot()

    ui.display_title()
    ui.console.rule("[yellow]Consultation Mode (No Monte Carlo)[/yellow]")

    if imported > 0:
        ui.console.print(f"[dim]Loaded {imported} scenario(s) from db_snapshot.json.[/dim]")

    num_opponents = ui.ask_int("Number of opponents (1-9)", min_val=1, max_val=9, default=1)
    hand = ui.ask_cards("Enter your 2 hole cards (e.g. Ah Kd)", expected=2, already_used=[])
    used = list(hand)
    board = ui.ask_cards(
        "Enter full River board (5 cards, e.g. 2h 7d Qc Jc 5s)",
        expected=5,
        already_used=used,
    )

    row = db.fetch_by_exact(_hand_key(hand), "River", _community_key(board), num_opponents)
    if not row:
        ui.console.print("[red]No stored result found for this exact River scenario.[/red]")
        ui.console.print("[dim]Run produce_data.py to generate missing scenarios.[/dim]")
        return

    total = row["total"] or 1
    result = {
        "wins": row["wins"],
        "ties": row["ties"],
        "losses": row["losses"],
        "total": row["total"],
        "win_rate": row["wins"] / total,
        "tie_rate": row["ties"] / total,
        "equity": (row["wins"] + row["ties"] * 0.5) / total,
    }
    ui.display_results("River (DB)", hand, board, result, num_opponents)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
