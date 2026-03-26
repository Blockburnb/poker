from __future__ import annotations

import sys

from rich.prompt import Confirm, Prompt

import simulator as sim
import ui
from bots.base import BotStrategy, DecisionContext
from bots.registry import create_strategy, strategy_keys


def _run_stage(
    stage_name: str,
    hand: list[int],
    community_cards: list[int],
    num_opponents: int,
    num_iterations: int,
    model: BotStrategy,
) -> None:
    result = ui.run_simulation_with_progress(
        sim.simulate, hand, community_cards, num_opponents, num_iterations
    )

    ctx = DecisionContext(
        hand=tuple(hand),
        hand_str=" ".join(sim.cards_to_str([c]).strip() for c in hand),
        estimated_equity=float(result["equity"]),
        pot=0,
        to_call=0,
        hero_stack=0,
        villain_stack=0,
        hand_index=max(0, len(community_cards)),
        total_hands=5,
    )
    recommendation = model.decide(ctx)

    ui.display_results(stage_name, hand, community_cards, result, num_opponents, prior=None)
    ui.console.print(
        f"[bold magenta]Model recommendation ({model.info.key})[/bold magenta]: "
        f"[white]{recommendation}[/white]"
    )


def run_oracle_mode(show_title: bool = True) -> None:
    if show_title:
        ui.display_title()

    ui.console.rule("[yellow]Oracle Mode (No persistence)[/yellow]")

    model_keys = strategy_keys(include_human=False)
    default_model = "mc10k_51" if "mc10k_51" in model_keys else model_keys[0]
    model_key = Prompt.ask("Prediction model", choices=model_keys, default=default_model)
    model = create_strategy(model_key)

    num_opponents = ui.ask_int(
        "Number of opponents (1-9)", min_val=1, max_val=9, default=1
    )
    num_iterations = ui.ask_int(
        "Monte Carlo iterations (1 000-1 000 000)",
        min_val=1_000,
        max_val=1_000_000,
        default=50_000,
    )

    used_cards: list[int] = []
    hand = ui.ask_cards(
        "Enter your 2 hole cards (e.g. Ah Kd)", expected=2, already_used=used_cards
    )
    used_cards.extend(hand)

    _run_stage("Pre-Flop", hand, [], num_opponents, num_iterations, model)

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
    _run_stage("Flop", hand, community, num_opponents, num_iterations, model)

    if not Confirm.ask("\nContinue to [bold]Turn[/bold]?", default=True):
        _finish()
        return

    turn = ui.ask_cards("Enter the Turn card (e.g. Jc)", expected=1, already_used=used_cards)
    used_cards.extend(turn)
    community = community + turn
    _run_stage("Turn", hand, community, num_opponents, num_iterations, model)

    if not Confirm.ask("\nContinue to [bold]River[/bold]?", default=True):
        _finish()
        return

    river = ui.ask_cards("Enter the River card (e.g. 5s)", expected=1, already_used=used_cards)
    used_cards.extend(river)
    community = community + river
    _run_stage("River", hand, community, num_opponents, num_iterations, model)

    _finish()


def _finish() -> None:
    ui.console.rule()
    ui.console.print("[bold green]Oracle session completed.[/bold green]")


def main() -> None:
    run_oracle_mode(show_title=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(0)
