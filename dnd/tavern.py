"""Persistence helpers for per-guild tavern configuration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

from .characters import Character


@dataclass
class TavernConfig:
    """Stored information about a guild's tavern channel."""

    guild_id: int
    channel_id: int
    message_id: int | None = None


class TavernConfigStore:
    """Concurrency-safe storage for tavern configuration."""

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = asyncio.Lock()
        self._loaded = False
        self._cache: Dict[str, TavernConfig] = {}

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if not self._storage_path.exists():
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache = {}
            self._loaded = True
            return
        text = await asyncio.to_thread(self._storage_path.read_text, encoding="utf-8")
        if text.strip():
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                self._cache = {}
            else:
                cache: Dict[str, TavernConfig] = {}
                if isinstance(raw, dict):
                    for guild_id, payload in raw.items():
                        try:
                            numeric_id = int(guild_id)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(payload, dict):
                            continue
                        channel_id = payload.get("channel_id")
                        message_id = payload.get("message_id")
                        if not isinstance(channel_id, int):
                            continue
                        config = TavernConfig(
                            guild_id=numeric_id,
                            channel_id=channel_id,
                            message_id=message_id if isinstance(message_id, int) else None,
                        )
                        cache[str(numeric_id)] = config
                self._cache = cache
        self._loaded = True

    async def _persist(self) -> None:
        serialised = {
            guild_id: {
                "channel_id": config.channel_id,
                **({"message_id": config.message_id} if config.message_id is not None else {}),
            }
            for guild_id, config in self._cache.items()
        }
        text = json.dumps(serialised, indent=2, sort_keys=True)
        await asyncio.to_thread(self._storage_path.write_text, text, encoding="utf-8")

    async def get_config(self, guild_id: int) -> Optional[TavernConfig]:
        async with self._lock:
            await self._ensure_loaded()
            config = self._cache.get(str(guild_id))
            if config is None:
                return None
            return TavernConfig(
                guild_id=config.guild_id,
                channel_id=config.channel_id,
                message_id=config.message_id,
            )

    async def set_channel(self, guild_id: int, channel_id: int) -> TavernConfig:
        async with self._lock:
            await self._ensure_loaded()
            config = TavernConfig(guild_id=guild_id, channel_id=channel_id)
            self._cache[str(guild_id)] = config
            await self._persist()
            return config

    async def update_message(self, guild_id: int, message_id: Optional[int]) -> Optional[TavernConfig]:
        async with self._lock:
            await self._ensure_loaded()
            config = self._cache.get(str(guild_id))
            if config is None:
                return None
            config.message_id = message_id
            await self._persist()
            return TavernConfig(
                guild_id=config.guild_id,
                channel_id=config.channel_id,
                message_id=config.message_id,
            )

    async def all_configs(self) -> Tuple[TavernConfig, ...]:
        async with self._lock:
            await self._ensure_loaded()
            return tuple(
                TavernConfig(
                    guild_id=config.guild_id,
                    channel_id=config.channel_id,
                    message_id=config.message_id,
                )
                for config in self._cache.values()
            )

    async def clear(self, guild_id: int) -> bool:
        async with self._lock:
            await self._ensure_loaded()
            try:
                del self._cache[str(guild_id)]
            except KeyError:
                return False
            await self._persist()
            return True


@dataclass(frozen=True)
class ShopItem:
    """An item available for sale at the tavern shop."""

    key: str
    name: str
    price: int
    description: str = ""
    inventory_name: Optional[str] = None
    sell_price: Optional[int] = None

    @property
    def stored_name(self) -> str:
        return (self.inventory_name or self.name).strip()

    @property
    def resale_value(self) -> int:
        value = self.sell_price if self.sell_price is not None else self.price // 2
        return max(1, int(value))


class ShopError(RuntimeError):
    """Base error raised when a shop interaction fails."""


class InsufficientFunds(ShopError):
    """Raised when a character cannot afford an item."""


class ItemNotCarried(ShopError):
    """Raised when attempting to sell an item not in the inventory."""


class TavernShop:
    """Manage the stock and transactions for the tavern trader."""

    def __init__(self, stock: Sequence[ShopItem]) -> None:
        if not stock:
            raise ValueError("TavernShop requires at least one stocked item")
        self._stock_order = tuple(stock)
        self._stock: Dict[str, ShopItem] = {item.key: item for item in stock}
        self._inventory_lookup: Dict[str, ShopItem] = {
            item.stored_name.lower(): item for item in stock
        }

    def list_items(self) -> Tuple[ShopItem, ...]:
        return self._stock_order

    def get(self, item_key: str) -> Optional[ShopItem]:
        return self._stock.get(item_key)

    def items_from_inventory(self, inventory: Iterable[str]) -> Tuple[Tuple[ShopItem, int], ...]:
        counts: Dict[str, int] = {}
        for entry in inventory:
            key = entry.strip().lower()
            item = self._inventory_lookup.get(key)
            if item is None:
                continue
            counts[item.key] = counts.get(item.key, 0) + 1
        return tuple((self._stock[item_key], count) for item_key, count in counts.items())

    def purchase(self, character: Character, item_key: str) -> Character:
        item = self._stock.get(item_key)
        if item is None:
            raise ItemNotCarried(f"Unknown item '{item_key}'")
        if character.gold_coins < item.price:
            raise InsufficientFunds(
                f"Not enough Gold Coins to purchase {item.name}."
            )
        updated_inventory = tuple((*character.inventory, item.stored_name))
        return replace(
            character,
            inventory=updated_inventory,
            gold_coins=character.gold_coins - item.price,
        )

    def sell(self, character: Character, item_key: str) -> Character:
        item = self._stock.get(item_key)
        if item is None:
            raise ItemNotCarried(f"Unknown item '{item_key}'")
        stored = item.stored_name.lower()
        items = list(character.inventory)
        index = None
        for idx, entry in enumerate(items):
            if entry.strip().lower() == stored:
                index = idx
                break
        if index is None:
            raise ItemNotCarried(f"{item.name} is not in the character's inventory")
        del items[index]
        return replace(
            character,
            inventory=tuple(items),
            gold_coins=character.gold_coins + item.resale_value,
        )

    @classmethod
    def default_shop(cls) -> "TavernShop":
        """Return the standard tavern stock list."""

        stock = (
            ShopItem("potion_healing", "Potion of Healing", 50, "Restores 2d4+2 HP."),
            ShopItem("torch_bundle", "Torches (10)", 2, "A bundle of torches to light the dark."),
            ShopItem("rations", "Travel Rations (5)", 5, "Simple provisions for the road."),
            ShopItem("rope_hemp", "50 ft. Hemp Rope", 1, "Sturdy rope for climbing and hauling."),
            ShopItem("chain_mail", "Chain Mail", 75, "Sturdy protection for the front line."),
            ShopItem("leather_armor", "Leather Armor", 10, "Light armor for agile adventurers."),
            ShopItem("scale_mail", "Scale Mail", 50, "Balanced defense with mobility."),
            ShopItem("shield", "Shield", 10, "Bolster your defenses with a shield."),
            ShopItem("longsword", "Longsword", 15, "A reliable blade for warriors."),
            ShopItem("rapier", "Rapier", 25, "Favoured weapon of duelists.", inventory_name="Rapier"),
            ShopItem("shortbow", "Shortbow", 25, "A ranged option for precise shots."),
            ShopItem("longbow", "Longbow", 50, "Great for keeping foes at bay."),
            ShopItem("arrows", "Arrows (20)", 1, "A quiver of twenty arrows."),
            ShopItem("bolts", "Crossbow Bolts (20)", 1, "Standard bolts for a crossbow."),
            ShopItem("dagger", "Dagger", 2, "A trusty backup blade."),
            ShopItem("light_crossbow", "Light Crossbow", 25, "Simple to use and effective."),
            ShopItem("quarterstaff", "Quarterstaff", 2, "A sturdy quarterstaff."),
            ShopItem("mace", "Mace", 5, "Crush foes with solid weight."),
            ShopItem("component_pouch", "Component Pouch", 25, "Keep spell components organised."),
            ShopItem("spellbook", "Spellbook", 50, "A blank tome for spells."),
            ShopItem("explorers_pack", "Explorer's Pack", 10, "Essentials for dungeoneering."),
            ShopItem("dungeoneers_pack", "Dungeoneer's Pack", 12, "Gear for underground adventures."),
            ShopItem("burglars_pack", "Burglar's Pack", 16, "Tools of the sneaky trade."),
            ShopItem("scholars_pack", "Scholar's Pack", 40, "Resources for the academic adventurer."),
            ShopItem("thieves_tools", "Thieves' Tools", 25, "Perfect for deft hands."),
            ShopItem("holy_symbol", "Holy Symbol", 5, "A focus for divine magic."),
            ShopItem("two_shortswords", "Two Shortswords", 20, "A matched pair of blades."),
        )
        return cls(stock)


__all__ = [
    "TavernConfig",
    "TavernConfigStore",
    "ShopItem",
    "ShopError",
    "InsufficientFunds",
    "ItemNotCarried",
    "TavernShop",
]
