from __future__ import annotations

from dataclasses import dataclass

from rich.prompt import Prompt

import ui
from bots.base import BotStrategy, Decision, DecisionContext, StrategyInfo


@dataclass
class HumanBot(BotStrategy):
    info: StrategyInfo = StrategyInfo(
        key="human",
        name="Human",
        summary="Interactive decision in TUI for each hand.",
        tags=("interactive",),
    )

    def decide(self, ctx: DecisionContext) -> Decision:
        ui.console.print(
            f"[bold]Hand {ctx.hand_index}/{ctx.total_hands}[/bold] "
            f"{ctx.hand_str} | estimated equity={ctx.estimated_equity * 100:.1f}%"
        )
        choice = Prompt.ask("Action", choices=["fold", "play"], default="play")
        return "play" if choice == "play" else "fold"
