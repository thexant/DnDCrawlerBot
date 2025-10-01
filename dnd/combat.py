"""Combat utilities for initiative, attack rolls, and saving throws."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from .characters import AbilityScores
from .dungeon import MonsterDefinition

__all__ = [
    "AdvantageState",
    "Attack",
    "AttackOutcome",
    "AttackRollResult",
    "Combatant",
    "DamagePacket",
    "InitiativeResult",
    "MultiattackResult",
    "SavingThrowResult",
    "ability_modifier",
    "apply_damage",
    "attack_roll",
    "compute_spell_save_dc",
    "resolve_advantage_state",
    "resolve_attack",
    "resolve_multiattack",
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
class AdvantageState:
    """Collapsed representation of advantage/disadvantage sources."""

    advantage: bool
    disadvantage: bool
    advantage_sources: Tuple[str, ...] = ()
    disadvantage_sources: Tuple[str, ...] = ()

    @property
    def is_neutral(self) -> bool:
        return not self.advantage and not self.disadvantage


def resolve_advantage_state(
    advantage_sources: Sequence[str] | None = None,
    disadvantage_sources: Sequence[str] | None = None,
) -> AdvantageState:
    """Collapse the provided sources into the final advantage state."""

    advantages = tuple(advantage_sources or ())
    disadvantages = tuple(disadvantage_sources or ())
    has_advantage = bool(advantages)
    has_disadvantage = bool(disadvantages)
    if has_advantage and has_disadvantage:
        # Advantage and disadvantage cancel out.
        return AdvantageState(
            advantage=False,
            disadvantage=False,
            advantage_sources=advantages,
            disadvantage_sources=disadvantages,
        )
    return AdvantageState(
        advantage=has_advantage,
        disadvantage=has_disadvantage,
        advantage_sources=advantages,
        disadvantage_sources=disadvantages,
    )


@dataclass(frozen=True)
class AttackRollResult:
    total: int
    roll: int
    natural: int
    is_critical_hit: bool
    is_automatic_miss: bool
    hits: bool


@dataclass(frozen=True)
class DamagePacket:
    amount: int
    damage_type: str | None = None


@dataclass(frozen=True)
class Attack:
    name: str
    attack_bonus: int
    damage_packets: Tuple[DamagePacket, ...]
    advantage_sources: Tuple[str, ...] = ()
    disadvantage_sources: Tuple[str, ...] = ()
    critical_double: bool = True


@dataclass(frozen=True)
class AttackOutcome:
    attack: Attack
    roll_result: AttackRollResult
    damage: int


@dataclass(frozen=True)
class MultiattackResult:
    outcomes: Tuple[AttackOutcome, ...]
    total_damage: int


def attack_roll(
    attacker_bonus: int,
    target_armor_class: int,
    *,
    rng: random.Random | None = None,
    advantage: bool = False,
    disadvantage: bool = False,
    advantage_state: AdvantageState | None = None,
) -> AttackRollResult:
    """Resolve an attack roll against a target's armor class."""

    if advantage_state is not None:
        advantage = advantage_state.advantage
        disadvantage = advantage_state.disadvantage

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
    advantage_state: AdvantageState | None = None,
) -> SavingThrowResult:
    """Resolve a saving throw against a difficulty class (DC)."""

    if advantage_state is not None:
        advantage = advantage_state.advantage
        disadvantage = advantage_state.disadvantage

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


def apply_damage(
    packets: Sequence[DamagePacket],
    *,
    resistances: Iterable[str] | None = None,
    vulnerabilities: Iterable[str] | None = None,
    immunities: Iterable[str] | None = None,
) -> int:
    """Calculate final damage after applying resistances and vulnerabilities."""

    resistance_set = {damage_type.lower() for damage_type in (resistances or [])}
    vulnerability_set = {damage_type.lower() for damage_type in (vulnerabilities or [])}
    immunity_set = {damage_type.lower() for damage_type in (immunities or [])}

    total = 0
    for packet in packets:
        amount = max(0, packet.amount)
        if amount == 0:
            continue
        damage_type = packet.damage_type.lower() if packet.damage_type else None
        if damage_type and damage_type in immunity_set:
            continue
        adjusted = amount
        if damage_type:
            resistant = damage_type in resistance_set
            vulnerable = damage_type in vulnerability_set
            if resistant and vulnerable:
                # They cancel out.
                resistant = vulnerable = False
            if resistant:
                adjusted = amount // 2
            elif vulnerable:
                adjusted = amount * 2
        total += adjusted
    return total


def resolve_attack(
    attack: Attack,
    target_armor_class: int,
    *,
    rng: random.Random | None = None,
    resistances: Iterable[str] | None = None,
    vulnerabilities: Iterable[str] | None = None,
    immunities: Iterable[str] | None = None,
) -> AttackOutcome:
    """Resolve a single attack and return the outcome along with damage dealt."""

    advantage_state = resolve_advantage_state(
        attack.advantage_sources, attack.disadvantage_sources
    )
    roll_result = attack_roll(
        attack.attack_bonus,
        target_armor_class,
        rng=rng,
        advantage_state=advantage_state,
    )
    damage = 0
    if roll_result.hits:
        damage = apply_damage(
            attack.damage_packets,
            resistances=resistances,
            vulnerabilities=vulnerabilities,
            immunities=immunities,
        )
        if roll_result.is_critical_hit and attack.critical_double:
            damage *= 2

    return AttackOutcome(attack=attack, roll_result=roll_result, damage=damage)


def resolve_multiattack(
    attacks: Sequence[Attack],
    target_armor_class: int,
    *,
    rng: random.Random | None = None,
    resistances: Iterable[str] | None = None,
    vulnerabilities: Iterable[str] | None = None,
    immunities: Iterable[str] | None = None,
) -> MultiattackResult:
    """Resolve a sequence of attacks and return the aggregated result."""

    generator = rng or random
    outcomes: List[AttackOutcome] = []
    total_damage = 0
    for attack in attacks:
        outcome = resolve_attack(
            attack,
            target_armor_class,
            rng=generator,
            resistances=resistances,
            vulnerabilities=vulnerabilities,
            immunities=immunities,
        )
        outcomes.append(outcome)
        total_damage += outcome.damage

    return MultiattackResult(outcomes=tuple(outcomes), total_damage=total_damage)


def compute_spell_save_dc(ability_mod: int, proficiency_bonus: int, *, base: int = 8) -> int:
    """Compute the spell save DC using the provided ability modifier."""

    return base + ability_mod + proficiency_bonus
