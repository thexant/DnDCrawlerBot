"""Dungeon generation utilities."""

from dnd.content import EncounterTable, Item, Monster, RoomTemplate, Theme, ThemeRegistry, Trap

from .generator import Corridor, Dungeon, DungeonGenerator, EncounterResult, Room, RoomExit

LootDefinition = Item
MonsterDefinition = Monster
TrapDefinition = Trap

__all__ = [
    "Corridor",
    "Dungeon",
    "DungeonGenerator",
    "EncounterResult",
    "EncounterTable",
    "Item",
    "LootDefinition",
    "Monster",
    "MonsterDefinition",
    "Room",
    "RoomExit",
    "RoomTemplate",
    "Theme",
    "ThemeRegistry",
    "Trap",
    "TrapDefinition",
]
