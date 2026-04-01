from __future__ import annotations

import atexit
import json
import random
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Callable, Iterable

from treys import Evaluator

import simulator as sim
from bots.base import BotStrategy, Decision, DecisionContext


SMALL_BLIND = 1
BIG_BLIND = 2
EQUITY_CACHE_FILE = "equity_cache.json"
_EQUITY_FLUSH_EVERY = 200
_equity_disk_cache: dict[str, float] | None = None
_equity_dirty_writes = 0


def _is_continue_action(action: Decision) -> bool:
    return action in {"play", "raise"}


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


@dataclass(frozen=True)
class FieldComparisonRow:
    strategy_key: str
    strategy_name: str
    tables_played: int
    seat_appearances: int
    hands: int
    showdowns: int
    total_profit: int
    avg_profit_per_100: float
    avg_table_size: float


@lru_cache(maxsize=50000)
def _estimate_preflop_equity_cached(sorted_hand: tuple[int, int], opponents: int, iterations: int) -> float:
    global _equity_dirty_writes

    disk_cache = _load_equity_disk_cache()
    key = f"{sorted_hand[0]}-{sorted_hand[1]}|{opponents}|{iterations}"
    cached = disk_cache.get(key)
    if cached is not None:
        return float(cached)

    result = sim.simulate(list(sorted_hand), [], max(1, opponents), iterations)
    equity = float(result["equity"])
    disk_cache[key] = equity
    _equity_dirty_writes += 1
    if _equity_dirty_writes >= _EQUITY_FLUSH_EVERY:
        _flush_equity_disk_cache(force=False)
    return equity


def _load_equity_disk_cache() -> dict[str, float]:
    global _equity_disk_cache

    if _equity_disk_cache is not None:
        return _equity_disk_cache

    path = Path(EQUITY_CACHE_FILE)
    if not path.exists():
        _equity_disk_cache = {}
        return _equity_disk_cache

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _equity_disk_cache = {}
        return _equity_disk_cache

    if not isinstance(payload, dict):
        _equity_disk_cache = {}
        return _equity_disk_cache

    _equity_disk_cache = {str(k): float(v) for k, v in payload.items()}
    return _equity_disk_cache


def _flush_equity_disk_cache(force: bool) -> None:
    global _equity_dirty_writes

    if _equity_disk_cache is None:
        return
    if not force and _equity_dirty_writes < _EQUITY_FLUSH_EVERY:
        return

    try:
        Path(EQUITY_CACHE_FILE).write_text(
            json.dumps(_equity_disk_cache, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        _equity_dirty_writes = 0
    except OSError:
        # Keep simulation running even when cache file cannot be written.
        return


atexit.register(lambda: _flush_equity_disk_cache(force=True))


def _cards_str(cards: Iterable[int]) -> str:
    return _cards_str_cached(tuple(cards))


@lru_cache(maxsize=100000)
def _cards_str_cached(cards: tuple[int, ...]) -> str:
    return " ".join(sim.cards_to_str([c]).strip() for c in cards)


def _dealt_hands_and_board(rng: random.Random) -> tuple[tuple[int, int], tuple[int, int], list[int]]:
    deck = rng.sample(sim.FULL_DECK, 9)
    hero = tuple(deck[0:2])
    villain = tuple(deck[2:4])
    board = list(deck[4:9])
    return hero, villain, board


def _compute_positions(table_size: int, button_index: int) -> tuple[int, int]:
    """Return (sb_index, bb_index) for a given button seat.

    Heads-up convention: button posts SB and acts first preflop.
    """
    if table_size < 2:
        raise ValueError("table_size must be >= 2")
    if table_size == 2:
        sb_index = button_index % 2
        bb_index = (button_index + 1) % 2
        return sb_index, bb_index

    sb_index = (button_index + 1) % table_size
    bb_index = (button_index + 2) % table_size
    return sb_index, bb_index


def _preflop_order(table_size: int, button_index: int) -> list[int]:
    """Action order for one preflop pass."""
    sb_index, bb_index = _compute_positions(table_size, button_index)
    if table_size == 2:
        return [sb_index, bb_index]

    first_to_act = (bb_index + 1) % table_size
    order: list[int] = []
    seat = first_to_act
    for _ in range(table_size):
        order.append(seat)
        seat = (seat + 1) % table_size
    return order


def _order_after_seat(table_size: int, seat_index: int) -> list[int]:
    return [((seat_index + offset) % table_size) for offset in range(1, table_size)]


def _award_pot(winners: list[int], pot: int, seat_profit: list[float], anchor_index: int = 0) -> None:
    """Distribute pot while conserving chips, even when split is uneven."""
    if not winners:
        return

    ordered = [winners[(anchor_index + offset) % len(winners)] for offset in range(len(winners))]
    base = pot // len(ordered)
    remainder = pot % len(ordered)
    for idx in ordered:
        seat_profit[idx] += base
    for i in range(remainder):
        seat_profit[ordered[i]] += 1


def _run_preflop_betting_round(
    bots: list[BotStrategy],
    hands: list[tuple[int, int]],
    hand_strs: list[str],
    seat_profit: list[float],
    hand_index: int,
    total_hands: int,
    button_index: int,
    equity_iterations: int,
) -> tuple[list[bool], list[int], int]:
    """Run a simplified preflop-only betting round with one raise cap."""
    table_size = len(bots)
    sb_index, bb_index = _compute_positions(table_size, button_index)

    active = [True] * table_size
    active_count = table_size
    table_total_stack = float(sum(seat_profit))
    contributions = [0] * table_size
    contributions[sb_index] = SMALL_BLIND
    contributions[bb_index] = BIG_BLIND
    max_bet = BIG_BLIND

    raised_by: int | None = None
    raise_used = False

    def _act(seat: int, allow_raise: bool) -> None:
        nonlocal max_bet, raised_by, raise_used, active_count
        if not active[seat]:
            return

        to_call = max(0, max_bet - contributions[seat])
        if to_call <= 0 and not allow_raise:
            return

        opponents_alive = max(1, active_count - 1)
        hero_stack = int(seat_profit[seat])
        villain_stack = int(table_total_stack - seat_profit[seat])
        if bots[seat].needs_equity():
            equity = _estimate_preflop_equity_cached(tuple(sorted(hands[seat])), opponents_alive, equity_iterations)
        else:
            equity = 0.0

        ctx = DecisionContext(
            hand=hands[seat],
            hand_str=hand_strs[seat],
            estimated_equity=equity,
            pot=max_bet,
            to_call=int(to_call),
            hero_stack=hero_stack,
            villain_stack=villain_stack,
            hand_index=hand_index,
            total_hands=total_hands,
            opponents=opponents_alive,
        )

        action = bots[seat].decide(ctx)
        if action == "fold":
            active[seat] = False
            active_count -= 1
            return

        if action == "raise" and allow_raise and not raise_used:
            contributions[seat] += int(to_call + BIG_BLIND)
            max_bet += BIG_BLIND
            raise_used = True
            raised_by = seat
            return

        # "play" and capped "raise" both become call/check here.
        contributions[seat] += int(to_call)

    # First preflop pass in positional order.
    for seat in _preflop_order(table_size, button_index):
        _act(seat, allow_raise=True)
        if active_count <= 1:
            break

    # If there was a raise, allow one response orbit with raises capped.
    if raised_by is not None and active_count > 1:
        for seat in _order_after_seat(table_size, raised_by):
            _act(seat, allow_raise=False)
            if active_count <= 1:
                break

    pot = int(sum(contributions))
    return active, contributions, pot


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

        button_index = (hand_index - 1) % 2
        bots = [hero, villain]
        hole = [hero_hand, villain_hand]
        seat_profit = [float(hero_profit), float(villain_profit)]
        hand_strs = [_cards_str(hero_hand), _cards_str(villain_hand)]
        active, contributions, pot = _run_preflop_betting_round(
            bots=bots,
            hands=hole,
            hand_strs=hand_strs,
            seat_profit=seat_profit,
            hand_index=hand_index,
            total_hands=hands,
            button_index=button_index,
            equity_iterations=equity_iterations,
        )

        hero_action = "play" if active[0] else "fold"
        villain_action = "play" if active[1] else "fold"

        if _is_continue_action(hero_action):
            hero_plays += 1
        if _is_continue_action(villain_action):
            villain_plays += 1

        hero_profit -= contributions[0]
        villain_profit -= contributions[1]

        winners: list[int]
        if active[0] and not active[1]:
            winners = [0]
            reason = "villain_fold"
        elif active[1] and not active[0]:
            winners = [1]
            reason = "hero_fold"
        elif not active[0] and not active[1]:
            # Degenerate case: if both fold, award to BB.
            winners = [((button_index + 1) % 2)]
            reason = "both_fold"
        else:
            showdowns += 1
            hero_score = evaluator.evaluate(board, list(hero_hand))
            villain_score = evaluator.evaluate(board, list(villain_hand))
            if hero_score < villain_score:
                winners = [0]
                reason = "showdown_win"
            elif hero_score > villain_score:
                winners = [1]
                reason = "showdown_loss"
            else:
                winners = [0, 1]
                ties += 1
                reason = "showdown_tie"

        seat_profit_after = [0.0, 0.0]
        _award_pot(winners, pot, seat_profit_after, anchor_index=button_index)
        hero_profit += int(seat_profit_after[0])
        villain_profit += int(seat_profit_after[1])

        delta_hero = int(seat_profit_after[0]) - contributions[0]

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


def run_field_comparison_series(
    strategy_keys: list[str],
    create_strategy_fn: Callable[[str], BotStrategy],
    strategy_name_fn: Callable[[str], str] | None = None,
    tables: int = 300,
    table_size_min: int = 2,
    table_size_max: int = 10,
    hands_per_table: int = 120,
    equity_iterations: int = 300,
    seed: int | None = None,
    hand_progress_callback: Callable[[int], None] | None = None,
    table_progress_callback: Callable[[int], None] | None = None,
) -> list[FieldComparisonRow]:
    """
    Compare strategies in randomized table compositions.

    Field simulation with random table size/composition and true multiway showdowns.
    Seats are sampled with replacement, so the same strategy can appear multiple
    times on the same table.
    """
    if len(strategy_keys) < 2:
        raise ValueError("Need at least 2 strategies for field comparison")
    if tables < 1:
        raise ValueError("tables must be >= 1")
    if hands_per_table < 1:
        raise ValueError("hands_per_table must be >= 1")

    if table_size_min < 2:
        raise ValueError("table_size_min must be >= 2")
    if table_size_max < table_size_min:
        raise ValueError("table_size_max must be >= table_size_min")
    if table_size_max > 10:
        raise ValueError("table_size_max must be <= 10")

    unique_keys = sorted(set(strategy_keys))

    if strategy_name_fn is None:
        def strategy_name_fn(key: str) -> str:
            return create_strategy_fn(key).info.name

    rng = random.Random(seed)
    stats: dict[str, dict[str, float]] = {
        key: {
            "name": strategy_name_fn(key),
            "tables": 0.0,
            "seats": 0.0,
            "hands": 0.0,
            "showdowns": 0.0,
            "profit": 0.0,
            "table_size_sum": 0.0,
        }
        for key in unique_keys
    }

    evaluator = Evaluator()
    for _ in range(tables):
        table_size = rng.randint(table_size_min, table_size_max)
        seated_keys = [rng.choice(strategy_keys) for _ in range(table_size)]
        seated_bots = [create_strategy_fn(key) for key in seated_keys]

        for bot in seated_bots:
            bot.reset_match_state()

        table_presence = set(seated_keys)
        for key in table_presence:
            stats[key]["tables"] += 1
        for key in seated_keys:
            stats[key]["seats"] += 1
            stats[key]["table_size_sum"] += table_size

        seat_profit: list[float] = [0.0] * table_size
        for hand_index in range(1, hands_per_table + 1):
            drawn = rng.sample(sim.FULL_DECK, table_size * 2 + 5)
            board = list(drawn[(table_size * 2):(table_size * 2 + 5)])
            hole_cards: list[tuple[int, int]] = [
                (drawn[idx * 2], drawn[idx * 2 + 1]) for idx in range(table_size)
            ]
            hand_strs = [_cards_str(hole_cards[idx]) for idx in range(table_size)]

            button_index = (hand_index - 1) % table_size
            active, contributions, pot = _run_preflop_betting_round(
                bots=seated_bots,
                hands=hole_cards,
                hand_strs=hand_strs,
                seat_profit=seat_profit,
                hand_index=hand_index,
                total_hands=hands_per_table,
                button_index=button_index,
                equity_iterations=equity_iterations,
            )

            active_indices = [idx for idx in range(table_size) if active[idx]]
            for idx in range(table_size):
                seat_profit[idx] -= contributions[idx]

            winners: list[int]
            if not active_indices:
                _, bb_index = _compute_positions(table_size, button_index)
                winners = [bb_index]
            elif len(active_indices) == 1:
                winners = [active_indices[0]]
            else:
                scores = [
                    (idx, evaluator.evaluate(board, list(hole_cards[idx])))
                    for idx in active_indices
                ]
                best_score = min(score for _, score in scores)
                winners = [idx for idx, score in scores if score == best_score]
                for idx in active_indices:
                    stats[seated_keys[idx]]["showdowns"] += 1

            _award_pot(winners, pot, seat_profit, anchor_index=button_index)

            for idx, key in enumerate(seated_keys):
                stats[key]["hands"] += 1

            if hand_progress_callback is not None:
                hand_progress_callback(table_size)

        for idx, key in enumerate(seated_keys):
            stats[key]["profit"] += seat_profit[idx]

        if table_progress_callback is not None:
            table_progress_callback(1)

    rows: list[FieldComparisonRow] = []
    for key, values in stats.items():
        tables_played = int(values["tables"])
        seat_appearances = int(values["seats"])
        hands = int(values["hands"])
        profit = int(values["profit"])
        table_size_sum = float(values["table_size_sum"])
        avg_table_size = (table_size_sum / seat_appearances) if seat_appearances else 0.0
        rows.append(
            FieldComparisonRow(
                strategy_key=key,
                strategy_name=str(values["name"]),
                tables_played=tables_played,
            seat_appearances=seat_appearances,
                hands=hands,
                showdowns=int(values["showdowns"]),
                total_profit=profit,
                avg_profit_per_100=(profit / hands) * 100 if hands else 0.0,
                avg_table_size=avg_table_size,
            )
        )

    # Keep inactive strategies at the bottom when random sampling never seated them.
    rows.sort(key=lambda r: (r.hands > 0, r.avg_profit_per_100), reverse=True)
    return rows
