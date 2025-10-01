import pytest

from dnd import AbilityScores, Character, InsufficientFunds, ItemNotCarried, ShopItem, TavernShop


def _make_character(*, inventory: tuple[str, ...] = (), gold_coins: int = 0) -> Character:
    assignments = {
        "STR": 15,
        "DEX": 14,
        "CON": 13,
        "INT": 12,
        "WIS": 10,
        "CHA": 8,
    }
    scores = AbilityScores.from_assignments(assignments, method="standard_array")
    return Character(
        guild_id=123,
        user_id=456,
        race_key="human",
        class_key="fighter",
        background_key="acolyte",
        ability_method="standard_array",
        base_ability_scores=scores,
        ability_scores=scores,
        racial_bonuses={"STR": 1},
        proficiencies=tuple(),
        inventory=inventory,
        gold_coins=gold_coins,
        name="Hero",
    )


def test_purchase_adds_item_and_deducts_gold() -> None:
    shop = TavernShop((ShopItem("potion", "Potion of Healing", 50),))
    hero = _make_character(gold_coins=60)
    updated = shop.purchase(hero, "potion")
    assert hero.gold_coins == 60  # original unchanged
    assert updated.gold_coins == 10
    assert "Potion of Healing" in updated.inventory
    with pytest.raises(InsufficientFunds):
        shop.purchase(_make_character(gold_coins=40), "potion")


def test_sell_removes_item_and_grants_gold() -> None:
    shop = TavernShop((ShopItem("potion", "Potion of Healing", 50),))
    hero = _make_character(inventory=("Potion of Healing",), gold_coins=5)
    updated = shop.sell(hero, "potion")
    assert hero.gold_coins == 5
    assert updated.gold_coins == 30  # 5 + 25 resale value
    assert updated.inventory == ()
    with pytest.raises(ItemNotCarried):
        shop.sell(updated, "potion")


def test_items_from_inventory_counts_duplicates() -> None:
    stock = (
        ShopItem("potion", "Potion of Healing", 50),
        ShopItem("shield", "Shield", 10),
    )
    shop = TavernShop(stock)
    hero = _make_character(inventory=("Potion of Healing", "Shield", "Potion of Healing"), gold_coins=0)
    entries = dict((item.key, count) for item, count in shop.items_from_inventory(hero.inventory))
    assert entries["potion"] == 2
    assert entries["shield"] == 1
