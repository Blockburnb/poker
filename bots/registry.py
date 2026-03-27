from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bots.base import BotStrategy, StrategyInfo
from bots.builtin import (
    AlwaysCallBot,
    AlwaysRaiseBot,
    CallingStationBot,
    LooseAggressiveBot,
    ManiacBot,
    MonteCarlo10K1Bot,
    MonteCarlo10K10Bot,
    MonteCarlo10K15Bot,
    MonteCarlo10K20Bot,
    MonteCarlo10K25Bot,
    MonteCarlo10K5Bot,
    MonteCarlo10K51Bot,
    MonteCarlo10KBot,
    RandomBot,
    TightAggressiveBot,
)
from bots.gto import ExternalPolicyBot, load_external_gto_policies
from bots.human import HumanBot


@dataclass(frozen=True)
class StrategyFactory:
    info: StrategyInfo
    create: Callable[[], BotStrategy]


_BASE_REGISTRY: dict[str, StrategyFactory] = {
    "always_call": StrategyFactory(AlwaysCallBot.info, AlwaysCallBot),
    "always_raise": StrategyFactory(AlwaysRaiseBot.info, AlwaysRaiseBot),
    "mc10k_1": StrategyFactory(MonteCarlo10K1Bot.info, MonteCarlo10K1Bot),
    "mc10k_5": StrategyFactory(MonteCarlo10K5Bot.info, MonteCarlo10K5Bot),
    "mc10k_10": StrategyFactory(MonteCarlo10K10Bot.info, MonteCarlo10K10Bot),
    "mc10k_15": StrategyFactory(MonteCarlo10K15Bot.info, MonteCarlo10K15Bot),
    "mc10k_20": StrategyFactory(MonteCarlo10K20Bot.info, MonteCarlo10K20Bot),
    "mc10k_25": StrategyFactory(MonteCarlo10K25Bot.info, MonteCarlo10K25Bot),
    "mc10k_51": StrategyFactory(MonteCarlo10K51Bot.info, MonteCarlo10K51Bot),
    "mc10k_75": StrategyFactory(MonteCarlo10KBot.info, MonteCarlo10KBot),
    "tag": StrategyFactory(TightAggressiveBot.info, TightAggressiveBot),
    "lag": StrategyFactory(LooseAggressiveBot.info, LooseAggressiveBot),
    "calling_station": StrategyFactory(CallingStationBot.info, CallingStationBot),
    "maniac": StrategyFactory(ManiacBot.info, ManiacBot),
    "random": StrategyFactory(RandomBot.info, RandomBot),
    "human": StrategyFactory(HumanBot.info, HumanBot),
}


def _build_registry() -> dict[str, StrategyFactory]:
    registry = dict(_BASE_REGISTRY)
    for key, info, hand_map, default_p in load_external_gto_policies():
        if key in registry:
            continue

        def _factory(info=info, hand_map=hand_map, default_p=default_p) -> BotStrategy:
            return ExternalPolicyBot(
                info=info,
                hand_play_probability=hand_map,
                default_play_probability=default_p,
            )

        registry[key] = StrategyFactory(info=info, create=_factory)
    return registry


def strategy_keys(include_human: bool = True) -> list[str]:
    keys = sorted(_build_registry().keys())
    if include_human:
        return keys
    return [k for k in keys if k != "human"]


def get_factory(key: str) -> StrategyFactory:
    registry = _build_registry()
    if key not in registry:
        available = ", ".join(strategy_keys(include_human=True))
        raise KeyError(f"Unknown strategy '{key}'. Available: {available}")
    return registry[key]


def create_strategy(key: str) -> BotStrategy:
    return get_factory(key).create()


def list_strategies(include_human: bool = True) -> list[StrategyFactory]:
    return [get_factory(k) for k in strategy_keys(include_human=include_human)]
