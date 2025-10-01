"""Unit coverage for combat helpers."""

from __future__ import annotations

import random

import pytest

from dnd.combat import (
    AdvantageState,
    Attack,
    DamagePacket,
    apply_damage,
    attack_roll,
    compute_spell_save_dc,
    resolve_advantage_state,
    resolve_multiattack,
)


def test_resolve_advantage_state_collapses_sources() -> None:
    state = resolve_advantage_state(["pack tactics"], [])
    assert isinstance(state, AdvantageState)
    assert state.advantage is True
    assert state.disadvantage is False

    neutral = resolve_advantage_state(["bless"], ["poisoned"])
    assert neutral.is_neutral is True


def test_attack_roll_uses_advantage_state() -> None:
    rng = random.Random(1)
    state = resolve_advantage_state(["ally help"], [])
    result = attack_roll(0, 10, rng=rng, advantage_state=state)
    # With the seeded RNG the rolls are 5 and 19; advantage selects the highest.
    assert result.natural == 19

    rng = random.Random(2)
    disadvantage = resolve_advantage_state([], ["blinded"])
    result = attack_roll(0, 10, rng=rng, advantage_state=disadvantage)
    # Rolls are 2 and 3; disadvantage keeps the lowest.
    assert result.natural == 2


def test_apply_damage_with_resistance_and_vulnerability() -> None:
    packets = [
        DamagePacket(amount=10, damage_type="Fire"),
        DamagePacket(amount=5, damage_type="Cold"),
    ]
    total = apply_damage(
        packets,
        resistances={"fire"},
        vulnerabilities={"cold"},
    )
    # Fire is halved (rounded down) and cold is doubled.
    assert total == 5 + 10

    # Resistance and vulnerability of the same type cancel each other out.
    neutral_total = apply_damage(
        [DamagePacket(amount=12, damage_type="lightning")],
        resistances={"lightning"},
        vulnerabilities={"lightning"},
    )
    assert neutral_total == 12


def test_resolve_multiattack_aggregates_damage() -> None:
    rng = random.Random(0)
    attacks = [
        Attack(
            name="Claw",
            attack_bonus=5,
            damage_packets=(DamagePacket(amount=7, damage_type="slashing"),),
        ),
        Attack(
            name="Bite",
            attack_bonus=5,
            damage_packets=(DamagePacket(amount=10, damage_type="piercing"),),
            advantage_sources=("pack tactics",),
        ),
    ]

    result = resolve_multiattack(
        attacks,
        15,
        rng=rng,
        resistances={"slashing"},
        vulnerabilities={"piercing"},
    )

    # First attack hits (13+5) and deals 7 -> 3 after resistance.
    # Second attack rolls with advantage taking 14 over 2 for a hit and deals 10 -> 20.
    assert result.total_damage == 23
    assert len(result.outcomes) == 2
    assert [outcome.damage for outcome in result.outcomes] == [3, 20]


@pytest.mark.parametrize(
    "ability_score, proficiency_bonus, expected",
    [(16, 3, 14), (18, 4, 16)],
)
def test_compute_spell_save_dc(ability_score: int, proficiency_bonus: int, expected: int) -> None:
    from dnd.combat import ability_modifier

    ability_mod = ability_modifier(ability_score)
    assert compute_spell_save_dc(ability_mod, proficiency_bonus) == expected
