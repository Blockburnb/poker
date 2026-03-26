from __future__ import annotations

import random
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from typing import Callable, Iterable

from treys import Evaluator

import simulator as sim
from bots.base import BotStrategy, DecisionContext


@dataclass(frozen=True)
class HandLog:
    hand_index: int
    hero_action: str
    villain_action: str
    hero_hand: str
    villain_hand: str
    board: str
    delta_hero: int
    reason: str


@dataclass(frozen=True)
class MatchResult:
    hero_name: str
    villain_name: str
    hands: int
    hero_profit: int
    villain_profit: int
    ties: int
    hero_plays: int
    villain_plays: int
    showdowns: int
    logs: list[HandLog]


@dataclass(frozen=True)
class TournamentRow:
    strategy_key: str
    strategy_name: str
    matches: int
    hands: int
    total_profit: int
    avg_profit_per_100: float


@lru_cache(maxsize=50000)
def _estimate_preflop_equity_cached(sorted_hand: tuple[int, int], iterations: int) -> float:
    result = sim.simulate(list(sorted_hand), [], 1, iterations)
    return float(result["equity"])


def _cards_str(cards: Iterable[int]) -> str:
    return " ".join(sim.cards_to_str([c]).strip() for c in cards)


def _dealt_hands_and_board(rng: random.Random) -> tuple[tuple[int, int], tuple[int, int], list[int]]:
    deck = list(sim.FULL_DECK)
    rng.shuffle(deck)
    hero = tuple(deck[0:2])
    villain = tuple(deck[2:4])
    board = list(deck[4:9])
    return hero, villain, board


def _build_context(
    hand: tuple[int, int],
    hand_index: int,
    total_hands: int,
    hero_stack: int,
    villain_stack: int,
    equity_iterations: int,
) -> DecisionContext:
    sorted_hand = tuple(sorted(hand))
    equity = _estimate_preflop_equity_cached(sorted_hand, equity_iterations)
    return DecisionContext(
        hand=hand,
        hand_str=_cards_str(hand),
        estimated_equity=equity,
        pot=2,
        to_call=1,
        hero_stack=hero_stack,
        villain_stack=villain_stack,
        hand_index=hand_index,
        total_hands=total_hands,
    )


def run_heads_up_match(
    hero: BotStrategy,
    villain: BotStrategy,
    hands: int = 200,
    equity_iterations: int = 300,
    seed: int | None = None,
    keep_hand_logs: bool = False,
    hand_progress_callback: Callable[[int], None] | None = None,
) -> MatchResult:
    if hands < 1:
        raise ValueError("hands must be >= 1")
    if equity_iterations < 50:
        raise ValueError("equity_iterations must be >= 50")

    rng = random.Random(seed)
    evaluator = Evaluator()

    hero.reset_match_state()
    villain.reset_match_state()

    hero_profit = 0
    villain_profit = 0
    ties = 0
    hero_plays = 0
    villain_plays = 0
    showdowns = 0
    logs: list[HandLog] = []

    for hand_index in range(1, hands + 1):
        hero_hand, villain_hand, board = _dealt_hands_and_board(rng)

        hero_ctx = _build_context(
            hero_hand,
            hand_index,
            hands,
            hero_stack=hero_profit,
            villain_stack=villain_profit,
            equity_iterations=equity_iterations,
        )
        villain_ctx = _build_context(
            villain_hand,
            hand_index,
            hands,
            hero_stack=villain_profit,
            villain_stack=hero_profit,
            equity_iterations=equity_iterations,
        )

        hero_action = hero.decide(hero_ctx)
        villain_action = villain.decide(villain_ctx)

        if hero_action == "play":
            hero_plays += 1
        if villain_action == "play":
            villain_plays += 1

        # Antes are 1 chip each, then each strategy decides fold/play.
        # If both play, each contributes +1 and goes to showdown (pot=4).
        if hero_action == "fold" and villain_action == "fold":
            ties += 1
            delta_hero = 0
            reason = "both_fold"
        elif hero_action == "fold":
            hero_profit -= 1
            villain_profit += 1
            delta_hero = -1
            reason = "hero_fold"
        elif villain_action == "fold":
            hero_profit += 1
            villain_profit -= 1
            delta_hero = 1
            reason = "villain_fold"
        else:
            showdowns += 1
            hero_score = evaluator.evaluate(board, list(hero_hand))
            villain_score = evaluator.evaluate(board, list(villain_hand))
            if hero_score < villain_score:
                hero_profit += 2
                villain_profit -= 2
                delta_hero = 2
                reason = "showdown_win"
            elif hero_score > villain_score:
                hero_profit -= 2
                villain_profit += 2
                delta_hero = -2
                reason = "showdown_loss"
            else:
                ties += 1
                delta_hero = 0
                reason = "showdown_tie"

        if keep_hand_logs:
            logs.append(
                HandLog(
                    hand_index=hand_index,
                    hero_action=hero_action,
                    villain_action=villain_action,
                    hero_hand=_cards_str(hero_hand),
                    villain_hand=_cards_str(villain_hand),
                    board=_cards_str(board),
                    delta_hero=delta_hero,
                    reason=reason,
                )
            )

        if hand_progress_callback is not None:
            hand_progress_callback(1)

    return MatchResult(
        hero_name=hero.info.name,
        villain_name=villain.info.name,
        hands=hands,
        hero_profit=hero_profit,
        villain_profit=villain_profit,
        ties=ties,
        hero_plays=hero_plays,
        villain_plays=villain_plays,
        showdowns=showdowns,
        logs=logs,
    )


def run_round_robin(
    entries: list[tuple[str, BotStrategy]],
    hands_per_match: int = 200,
    equity_iterations: int = 300,
    seed: int | None = None,
    hand_progress_callback: Callable[[int], None] | None = None,
) -> list[TournamentRow]:
    if len(entries) < 2:
        raise ValueError("Need at least 2 strategies for round-robin")

    base_seed = 0 if seed is None else int(seed)
    stats: dict[str, dict[str, float]] = {}
    for key, strat in entries:
        stats[key] = {
            "name": strat.info.name,
            "matches": 0,
            "hands": 0,
            "profit": 0.0,
        }

    match_index = 0
    for (_, (key_a, strat_a)), (_, (key_b, strat_b)) in combinations(enumerate(entries), 2):
        result = run_heads_up_match(
            strat_a,
            strat_b,
            hands=hands_per_match,
            equity_iterations=equity_iterations,
            seed=base_seed + match_index,
            keep_hand_logs=False,
            hand_progress_callback=hand_progress_callback,
        )
        match_index += 1

        stats[key_a]["matches"] += 1
        stats[key_b]["matches"] += 1
        stats[key_a]["hands"] += hands_per_match
        stats[key_b]["hands"] += hands_per_match
        stats[key_a]["profit"] += result.hero_profit
        stats[key_b]["profit"] += result.villain_profit

    rows: list[TournamentRow] = []
    for key, values in stats.items():
        hands = int(values["hands"])
        profit = int(values["profit"])
        per_100 = (profit / hands) * 100 if hands else 0.0
        rows.append(
            TournamentRow(
                strategy_key=key,
                strategy_name=str(values["name"]),
                matches=int(values["matches"]),
                hands=hands,
                total_profit=profit,
                avg_profit_per_100=per_100,
            )
        )

    rows.sort(key=lambda r: r.avg_profit_per_100, reverse=True)
    return rows


def run_round_robin_series(
    entries: list[tuple[str, BotStrategy]],
    runs: int = 100,
    hands_per_match: int = 200,
    equity_iterations: int = 300,
    seed: int | None = None,
    hand_progress_callback: Callable[[int], None] | None = None,
    run_progress_callback: Callable[[int], None] | None = None,
) -> list[TournamentRow]:
    if runs < 1:
        raise ValueError("runs must be >= 1")

    base_seed = 0 if seed is None else int(seed)
    merged: dict[str, dict[str, int | str]] = {
        key: {
            "name": strat.info.name,
            "matches": 0,
            "hands": 0,
            "profit": 0,
        }
        for key, strat in entries
    }

    for run_index in range(runs):
        run_rows = run_round_robin(
            entries,
            hands_per_match=hands_per_match,
            equity_iterations=equity_iterations,
            seed=base_seed + run_index,
            hand_progress_callback=hand_progress_callback,
        )
        for row in run_rows:
            agg = merged[row.strategy_key]
            agg["matches"] = int(agg["matches"]) + row.matches
            agg["hands"] = int(agg["hands"]) + row.hands
            agg["profit"] = int(agg["profit"]) + row.total_profit

        if run_progress_callback is not None:
            run_progress_callback(1)

    rows: list[TournamentRow] = []
    for key, data in merged.items():
        hands = int(data["hands"])
        profit = int(data["profit"])
        rows.append(
            TournamentRow(
                strategy_key=key,
                strategy_name=str(data["name"]),
                matches=int(data["matches"]),
                hands=hands,
                total_profit=profit,
                avg_profit_per_100=(profit / hands) * 100 if hands else 0.0,
            )
        )

    rows.sort(key=lambda r: r.avg_profit_per_100, reverse=True)
    return rows
