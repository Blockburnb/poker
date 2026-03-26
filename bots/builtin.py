from __future__ import annotations

import random
from functools import lru_cache
from dataclasses import dataclass, field

import simulator as sim
from bots.base import BotStrategy, Decision, DecisionContext, StrategyInfo


@lru_cache(maxsize=50000)
def _hand_equity_10k_cached(sorted_hand: tuple[int, int]) -> float:
    result = sim.simulate(list(sorted_hand), [], 1, 10_000)
    return float(result["equity"])


@dataclass
class MonteCarlo10KBot(BotStrategy):
    min_win_chance: float = 0.75
    info: StrategyInfo = StrategyInfo(
        key="mc10k_75",
        name="Monte Carlo 10K (75%)",
        summary="Runs 10k preflop sims; plays check/call only when equity >= 75%, else folds.",
        tags=("monte-carlo", "passive", "threshold"),
    )

    def decide(self, ctx: DecisionContext) -> Decision:
        equity = _hand_equity_10k_cached(tuple(sorted(ctx.hand)))
        return "play" if equity >= self.min_win_chance else "fold"

    def config(self) -> dict:
        return {"simulations": 10000, "min_win_chance": self.min_win_chance}


@dataclass
class MonteCarlo10K51Bot(BotStrategy):
    min_win_chance: float = 0.51
    info: StrategyInfo = StrategyInfo(
        key="mc10k_51",
        name="Monte Carlo 10K (51%)",
        summary="Runs 10k preflop sims; plays check/call only when equity >= 51%, else folds.",
        tags=("monte-carlo", "passive", "threshold"),
    )

    def decide(self, ctx: DecisionContext) -> Decision:
        equity = _hand_equity_10k_cached(tuple(sorted(ctx.hand)))
        return "play" if equity >= self.min_win_chance else "fold"

    def config(self) -> dict:
        return {"simulations": 10000, "min_win_chance": self.min_win_chance}


@dataclass
class TightAggressiveBot(BotStrategy):
    threshold: float = 0.62
    bluff_probability: float = 0.04
    _rng: random.Random = field(default_factory=random.Random)
    info: StrategyInfo = StrategyInfo(
        key="tag",
        name="Tight-Aggressive (TAG)",
        summary="Plays a tight value range with occasional bluffs.",
        tags=("style", "tight", "aggressive"),
    )

    def decide(self, ctx: DecisionContext) -> Decision:
        if ctx.estimated_equity >= self.threshold:
            return "play"
        return "play" if self._rng.random() < self.bluff_probability else "fold"

    def config(self) -> dict:
        return {
            "threshold": self.threshold,
            "bluff_probability": self.bluff_probability,
        }


@dataclass
class LooseAggressiveBot(BotStrategy):
    threshold: float = 0.49
    pressure_probability: float = 0.16
    _rng: random.Random = field(default_factory=random.Random)
    info: StrategyInfo = StrategyInfo(
        key="lag",
        name="Loose-Aggressive (LAG)",
        summary="Plays wide and applies pressure with frequent speculative plays.",
        tags=("style", "loose", "aggressive"),
    )

    def decide(self, ctx: DecisionContext) -> Decision:
        if ctx.estimated_equity >= self.threshold:
            return "play"
        return "play" if self._rng.random() < self.pressure_probability else "fold"

    def config(self) -> dict:
        return {
            "threshold": self.threshold,
            "pressure_probability": self.pressure_probability,
        }


@dataclass
class CallingStationBot(BotStrategy):
    threshold: float = 0.36
    call_probability_below_threshold: float = 0.70
    _rng: random.Random = field(default_factory=random.Random)
    info: StrategyInfo = StrategyInfo(
        key="calling_station",
        name="Calling Station",
        summary="Rarely folds and often continues even with weak equity.",
        tags=("style", "passive", "loose"),
    )

    def config(self) -> dict:
        return {
            "threshold": self.threshold,
            "call_probability_below_threshold": self.call_probability_below_threshold,
        }

    def decide(self, ctx: DecisionContext) -> Decision:
        if ctx.estimated_equity >= self.threshold:
            return "play"
        return "play" if self._rng.random() < self.call_probability_below_threshold else "fold"


@dataclass
class ManiacBot(BotStrategy):
    play_probability: float = 0.94
    _rng: random.Random = field(default_factory=random.Random)
    info: StrategyInfo = StrategyInfo(
        key="maniac",
        name="Maniac",
        summary="Hyper-loose profile that pushes action almost every hand.",
        tags=("style", "very-loose", "very-aggressive"),
    )

    def decide(self, ctx: DecisionContext) -> Decision:
        return "play" if self._rng.random() < self.play_probability else "fold"

    def config(self) -> dict:
        return {"play_probability": self.play_probability}


@dataclass
class RandomBot(BotStrategy):
    play_probability: float = 0.55
    _rng: random.Random = field(default_factory=random.Random)
    info: StrategyInfo = StrategyInfo(
        key="random",
        name="Random",
        summary="Chooses play/fold randomly according to a fixed probability.",
        tags=("style", "stochastic"),
    )

    def decide(self, ctx: DecisionContext) -> Decision:
        return "play" if self._rng.random() < self.play_probability else "fold"

    def reset_match_state(self) -> None:
        # Keep deterministic behavior per run only when seeded externally.
        return None

    def config(self) -> dict:
        return {"play_probability": self.play_probability}
