from dnd.content import Item, Trap
from dnd.dungeon.rewards import (
    RewardShare,
    allocate_loot,
    eligible_order,
    format_item_label,
    loot_value,
    rotate_party,
    split_gold,
    trap_reward_value,
)


def make_item(name: str, rarity: str) -> Item:
    return Item(key=name.lower(), name=name, rarity=rarity)


def make_trap(name: str, dc: int, damage: str | None = None) -> Trap:
    return Trap(
        key=name.lower(),
        name=name,
        description="",
        saving_throw={"dc": dc},
        damage=damage,
        tags=("test",),
    )


def test_rotate_party_wraps_start_index() -> None:
    order = rotate_party([1, 2, 3, 4], start_index=2)
    assert order == [3, 4, 1, 2]


def test_eligible_order_filters_and_preserves_rotation() -> None:
    order = eligible_order([10, 20, 30, 40], start_index=1, eligible=[10, 30])
    assert order == [30, 10]


def test_allocate_loot_round_robin_distribution() -> None:
    items = [
        make_item("Wand", "Uncommon"),
        make_item("Gem", "Rare"),
        make_item("Scroll", "Common"),
    ]
    shares = allocate_loot(items, [1, 2, 3, 4], start_index=1, eligible=[1, 2, 3])
    assert [share.user_id for share in shares] == [2, 3, 1]
    assert len(shares[0].items) == 1
    assert shares[0].items[0].name == "Wand"
    assert len(shares[1].items) == 1
    assert shares[1].items[0].name == "Gem"
    assert len(shares[2].items) == 1
    assert shares[2].items[0].name == "Scroll"
    assert all(share.gold == 100 for share in shares)


def test_split_gold_handles_remainder() -> None:
    shares = split_gold(7, [100, 200, 300])
    assert shares == [
        RewardShare(user_id=100, gold=3),
        RewardShare(user_id=200, gold=2),
        RewardShare(user_id=300, gold=2),
    ]


def test_loot_value_defaults_to_common_when_unknown() -> None:
    mysterious = make_item("Relic", "Mythic")
    assert loot_value(mysterious) == loot_value(make_item("Token", "Common"))


def test_format_item_label_includes_rarity() -> None:
    label = format_item_label(make_item("Orb", "Rare"))
    assert label == "Orb (Rare)"


def test_trap_reward_value_scales_with_dc_and_damage() -> None:
    easy_trap = make_trap("Snare", 12)
    hard_trap = make_trap("Obliterator", 17, damage="6d6 fire")
    assert trap_reward_value(hard_trap, 17) > trap_reward_value(easy_trap, 12)
