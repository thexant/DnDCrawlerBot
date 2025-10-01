"""Utility helpers for distributing dungeon rewards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from dnd.content import Item, Trap

# Default gold values used when converting magical loot into coin rewards.
RARITY_REWARD_VALUES: Mapping[str, int] = {
    "common": 25,
    "uncommon": 75,
    "rare": 200,
    "very rare": 750,
    "legendary": 2500,
    "artifact": 7500,
}


@dataclass(frozen=True)
class RewardShare:
    """Represents the loot and coin assigned to a single adventurer."""

    user_id: int
    items: tuple[Item, ...] = ()
    gold: int = 0


def loot_value(item: Item, *, rarity_values: Mapping[str, int] | None = None) -> int:
    """Return the notional gold value for ``item`` based on its rarity."""

    mapping = rarity_values or RARITY_REWARD_VALUES
    if not mapping:
        return 25
    default = mapping.get("common", next(iter(mapping.values())))
    return max(1, int(mapping.get(item.rarity.lower(), default)))


def format_item_label(item: Item) -> str:
    """Return a user-facing label for storing a loot item in an inventory."""

    return f"{item.name} ({item.rarity})"


def rotate_party(party_order: Sequence[int], start_index: int) -> list[int]:
    """Return ``party_order`` rotated so ``start_index`` becomes the first element."""

    if not party_order:
        return []
    size = len(party_order)
    start = start_index % size if size else 0
    return list(party_order[start:]) + list(party_order[:start])


def eligible_order(
    party_order: Sequence[int], start_index: int, eligible: Iterable[int]
) -> list[int]:
    """Return party members eligible for rewards preserving rotation order."""

    eligible_set = {int(user_id) for user_id in eligible}
    if not eligible_set:
        return []
    rotated = rotate_party(party_order, start_index) if party_order else list(eligible_set)
    order: list[int] = []
    seen: set[int] = set()
    for user_id in rotated:
        if user_id in eligible_set and user_id not in seen:
            order.append(user_id)
            seen.add(user_id)
    for user_id in eligible_set:
        if user_id not in seen:
            order.append(user_id)
            seen.add(user_id)
    return order


def split_gold(amount: int, order: Sequence[int]) -> list[RewardShare]:
    """Split ``amount`` of gold coins across ``order`` fairly."""

    if amount <= 0 or not order:
        return []
    count = len(order)
    base, remainder = divmod(int(amount), count)
    shares: list[RewardShare] = []
    for index, user_id in enumerate(order):
        gold = base
        if remainder and index < remainder:
            gold += 1
        if gold:
            shares.append(RewardShare(user_id=user_id, gold=gold))
    return shares


def allocate_loot(
    items: Sequence[Item],
    party_order: Sequence[int],
    start_index: int,
    eligible: Iterable[int],
) -> list[RewardShare]:
    """Distribute ``items`` and their value among ``eligible`` party members."""

    order = eligible_order(party_order, start_index, eligible)
    if not order:
        return []
    if not items:
        return []

    distribution: dict[int, list[Item]] = {user_id: [] for user_id in order}
    for index, item in enumerate(items):
        recipient = order[index % len(order)]
        distribution[recipient].append(item)

    total_value = sum(loot_value(item) for item in items)
    gold_shares = split_gold(total_value, order)
    gold_map = {share.user_id: share.gold for share in gold_shares}

    shares: list[RewardShare] = []
    for user_id in order:
        user_items = distribution.get(user_id, [])
        gold = gold_map.get(user_id, 0)
        if user_items or gold:
            shares.append(RewardShare(user_id=user_id, items=tuple(user_items), gold=gold))
    return shares


def trap_reward_value(trap: Trap, dc: int) -> int:
    """Return the gold reward granted for safely disarming ``trap``."""

    baseline = 10
    difficulty_bonus = max(0, dc - 10) * 3
    danger_bonus = 5 if trap.damage else 0
    tag_bonus = min(10, len(trap.tags) * 2)
    return max(10, baseline + difficulty_bonus + danger_bonus + tag_bonus)
