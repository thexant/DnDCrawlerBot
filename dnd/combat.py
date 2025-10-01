"""Combat utilities for initiative, attack rolls, and saving throws."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, List

from .characters import AbilityScores
from .dungeon import MonsterDefinition

__all__ = [
    "AttackRollResult",
    "Combatant",
    "InitiativeResult",
    "SavingThrowResult",
    "ability_modifier",
    "attack_roll",
    "roll_d20",
    "roll_initiative",
    "saving_throw",
]


def ability_modifier(score: int) -> int:
    """Return the D&D ability modifier for a given score."""

    return (score - 10) // 2


@dataclass(frozen=True)
class Combatant:
    """Entity participating in combat, either a character or monster."""

    name: str
    initiative_bonus: int

    @classmethod
    def from_scores(cls, name: str, ability_scores: AbilityScores, *, bonus: int = 0) -> "Combatant":
        dex = ability_scores.values.get("DEX", 10)
        return cls(name=name, initiative_bonus=ability_modifier(dex) + bonus)

    @classmethod
    def from_monster(cls, monster: MonsterDefinition, *, bonus: int = 0) -> "Combatant":
        dex = monster.ability_scores.get("DEX", 10)
        return cls(name=monster.name, initiative_bonus=ability_modifier(int(dex)) + bonus)


@dataclass(frozen=True)
class InitiativeResult:
    name: str
    roll: int
    total: int


def roll_d20(rng: random.Random | None = None) -> int:
    """Roll a single d20 using the provided RNG or the global generator."""

    generator = rng or random
    return generator.randint(1, 20)


def roll_initiative(
    participants: Iterable[Combatant],
    *,
    rng: random.Random | None = None,
) -> List[InitiativeResult]:
    """Roll initiative for the supplied participants."""

    generator = rng or random
    results: List[InitiativeResult] = []
    for combatant in participants:
        roll = roll_d20(generator)
        total = roll + combatant.initiative_bonus
        results.append(InitiativeResult(name=combatant.name, roll=roll, total=total))
    results.sort(key=lambda result: (result.total, result.roll), reverse=True)
    return results


@dataclass(frozen=True)
class AttackRollResult:
    total: int
    roll: int
    natural: int
    is_critical_hit: bool
    is_automatic_miss: bool
    hits: bool


def attack_roll(
    attacker_bonus: int,
    target_armor_class: int,
    *,
    rng: random.Random | None = None,
    advantage: bool = False,
    disadvantage: bool = False,
) -> AttackRollResult:
    """Resolve an attack roll against a target's armor class."""

    if advantage and disadvantage:
        advantage = disadvantage = False

    generator = rng or random

    rolls = [roll_d20(generator)]
    if advantage:
        rolls.append(roll_d20(generator))
    elif disadvantage:
        rolls.append(roll_d20(generator))

    natural = max(rolls) if advantage else min(rolls) if disadvantage else rolls[0]
    roll = natural
    total = roll + attacker_bonus

    is_critical = natural == 20
    is_automatic_miss = natural == 1

    hits = False
    if is_critical:
        hits = True
    elif not is_automatic_miss:
        hits = total >= target_armor_class

    return AttackRollResult(
        total=total,
        roll=roll,
        natural=natural,
        is_critical_hit=is_critical,
        is_automatic_miss=is_automatic_miss,
        hits=hits,
    )


@dataclass(frozen=True)
class SavingThrowResult:
    total: int
    roll: int
    natural: int
    success: bool


def saving_throw(
    save_bonus: int,
    dc: int,
    *,
    rng: random.Random | None = None,
    advantage: bool = False,
    disadvantage: bool = False,
) -> SavingThrowResult:
    """Resolve a saving throw against a difficulty class (DC)."""

    if advantage and disadvantage:
        advantage = disadvantage = False

    generator = rng or random
    rolls = [roll_d20(generator)]
    if advantage:
        rolls.append(roll_d20(generator))
    elif disadvantage:
        rolls.append(roll_d20(generator))

    natural = max(rolls) if advantage else min(rolls) if disadvantage else rolls[0]
    roll = natural
    total = roll + save_bonus
    success = total >= dc or natural == 20

    return SavingThrowResult(total=total, roll=roll, natural=natural, success=success)
