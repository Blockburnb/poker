"""
simulator.py
------------
Monte Carlo simulation engine for Texas Hold'em poker equity calculation.
Uses the `treys` library for fast hand evaluation.
"""

import random
from typing import List, Dict, Tuple

from treys import Card, Evaluator

# All valid ranks and suits in treys notation
ALL_RANKS = "23456789TJQKA"
ALL_SUITS = "hdsc"

# Build the full 52-card deck once at module level
FULL_DECK: List[int] = [Card.new(r + s) for r in ALL_RANKS for s in ALL_SUITS]


def parse_card(card_str: str) -> int:
    """
    Parse a human-readable card string (e.g. "Ah", "Kd", "2s") into a
    treys integer card.

    Raises:
        ValueError: if the format is invalid, the rank/suit is unknown, or the
                    resulting card cannot be created.
    """
    card_str = card_str.strip()
    if len(card_str) != 2:
        raise ValueError(
            f"Invalid card format '{card_str}': expected exactly 2 characters "
            f"(rank + suit), e.g. 'Ah', 'Kd', 'Ts'."
        )
    rank = card_str[0].upper()
    suit = card_str[1].lower()
    if rank not in ALL_RANKS:
        raise ValueError(
            f"Invalid rank '{rank}'. Valid ranks: {ALL_RANKS}"
        )
    if suit not in ALL_SUITS:
        raise ValueError(
            f"Invalid suit '{suit}'. Valid suits: {ALL_SUITS} "
            f"(h=hearts, d=diamonds, s=spades, c=clubs)"
        )
    return Card.new(rank + suit)


def cards_to_str(cards: List[int]) -> str:
    """Return a human-readable, canonical string for a list of treys cards."""
    return " ".join(Card.int_to_str(c) for c in cards)


def simulate(
    hand: List[int],
    community_cards: List[int],
    num_opponents: int,
    num_iterations: int,
) -> Dict:
    """
    Run a Monte Carlo simulation to estimate the equity of *hand* at the
    current board state.

    Args:
        hand:             2-card list (player's hole cards as treys ints).
        community_cards:  0–5 card list of known community cards.
        num_opponents:    Number of active opponents (1–9).
        num_iterations:   Number of random scenarios to simulate.

    Returns:
        A dict with keys:
            wins, ties, losses, total   – raw counts
            win_rate, tie_rate, equity  – floats in [0, 1]
    """
    evaluator = Evaluator()
    known = set(hand) | set(community_cards)
    remaining_deck = [c for c in FULL_DECK if c not in known]

    cards_needed_community = 5 - len(community_cards)
    cards_needed_per_opponent = 2
    total_cards_needed = cards_needed_community + num_opponents * cards_needed_per_opponent

    if len(remaining_deck) < total_cards_needed:
        raise ValueError(
            f"Not enough cards in the deck: need {total_cards_needed}, "
            f"have {len(remaining_deck)}."
        )

    wins = ties = losses = 0

    for _ in range(num_iterations):
        sample = random.sample(remaining_deck, total_cards_needed)

        full_community = community_cards + sample[:cards_needed_community]

        opponent_hands = [
            sample[
                cards_needed_community + i * 2 : cards_needed_community + i * 2 + 2
            ]
            for i in range(num_opponents)
        ]

        player_score = evaluator.evaluate(full_community, hand)
        best_opponent_score = min(
            evaluator.evaluate(full_community, opp) for opp in opponent_hands
        )

        if player_score < best_opponent_score:
            wins += 1
        elif player_score == best_opponent_score:
            ties += 1
        else:
            losses += 1

    total = wins + ties + losses
    return {
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "total": total,
        "win_rate": wins / total if total else 0.0,
        "tie_rate": ties / total if total else 0.0,
        "equity": (wins + ties * 0.5) / total if total else 0.0,
    }


def get_stage_name(community_count: int) -> str:
    """Return the poker stage name based on the number of community cards."""
    return {0: "Pre-Flop", 3: "Flop", 4: "Turn", 5: "River"}.get(
        community_count, "Unknown"
    )
