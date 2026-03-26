from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

import simulator as sim
from bots.base import BotStrategy, Decision, DecisionContext, StrategyInfo


def _canonical_hand_key(hand: tuple[int, int]) -> str:
    cards = [sim.cards_to_str([c]).strip() for c in hand]
    return " ".join(sorted(cards))


@dataclass
class ExternalPolicyBot(BotStrategy):
    info: StrategyInfo
    hand_play_probability: dict[str, float]
    default_play_probability: float = 0.5
    _rng: random.Random = field(default_factory=random.Random)

    def decide(self, ctx: DecisionContext) -> Decision:
        key = _canonical_hand_key(ctx.hand)
        p = float(self.hand_play_probability.get(key, self.default_play_probability))
        p = max(0.0, min(1.0, p))
        return "play" if self._rng.random() < p else "fold"

    def config(self) -> dict:
        return {
            "default_play_probability": self.default_play_probability,
            "mapped_hands": len(self.hand_play_probability),
        }


def load_external_gto_policies(policy_dir: str = "gto_policies") -> list[tuple[str, StrategyInfo, dict[str, float], float]]:
    directory = Path(policy_dir)
    if not directory.exists() or not directory.is_dir():
        return []

    loaded: list[tuple[str, StrategyInfo, dict[str, float], float]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        hand_map_raw = payload.get("hands", {})
        hand_map = {str(k): float(v) for k, v in hand_map_raw.items()}
        default_p = float(payload.get("default_play_probability", 0.5))
        title = str(payload.get("name") or path.stem)
        source = str(payload.get("source") or "external")

        key = f"gto_{path.stem}"
        info = StrategyInfo(
            key=key,
            name=f"GTO: {title}",
            summary=f"External policy from {source} ({path.name}).",
            tags=("gto", "external", source.lower()),
        )
        loaded.append((key, info, hand_map, default_p))

    return loaded
