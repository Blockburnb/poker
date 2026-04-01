from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Literal

Decision = Literal["fold", "play", "raise"]


@dataclass(frozen=True)
class DecisionContext:
    """Information passed to a strategy for one decision."""

    hand: tuple[int, int]
    hand_str: str
    estimated_equity: float
    pot: int
    to_call: int
    hero_stack: int
    villain_stack: int
    hand_index: int
    total_hands: int
    opponents: int = 1


@dataclass(frozen=True)
class StrategyInfo:
    key: str
    name: str
    summary: str
    tags: tuple[str, ...] = field(default_factory=tuple)


class BotStrategy(ABC):
    """Base class for poker bot strategies."""

    info: StrategyInfo

    @abstractmethod
    def decide(self, ctx: DecisionContext) -> Decision:
        raise NotImplementedError

    def reset_match_state(self) -> None:
        """Hook used by the arena before a new match starts."""

    def config(self) -> Dict[str, Any]:
        """Optional strategy config displayed in the TUI table."""
        return {}

    def needs_equity(self) -> bool:
        """Whether this strategy needs estimated_equity in DecisionContext."""
        return True
