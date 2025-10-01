# Dungeon Content Authoring

The bot loads encounter content from structured files under the top level `data/` directory. Files may be written in **JSON** or **YAML** and are validated when the bot starts or when an admin runs `/dungeon reload`.

## Directory layout

| Folder | Description |
| ------ | ----------- |
| `data/monsters/` | Creature stat blocks grouped by theme (for example `constructs.json`). |
| `data/traps/` | Trap descriptions grouped by style (for example `arcane.json`). |
| `data/items/` | Lootable items and treasures. |
| `data/themes/` | Dungeon theme definitions that reference the other registries. |
| `data/sessions/metadata.json` | Automatically managed guild session metadata (default theme, stored dungeons, recent run). |

## Schemas

Files may define a single entry or a list of entries. When working with grouped files it is recommended to provide an explicit `id`, `key`, or `slug` field for each entry to keep identifiers predictable.

### Monsters (`data/monsters/`)

```json
[
  {
    "key": "ghast",
    "name": "Ghast",
    "challenge": 3,
    "armor_class": 12,
    "hit_points": 36,
    "attack_bonus": 5,
    "damage": "2d6+3",
    "ability_scores": {"STR": 16, "DEX": 17, "CON": 13},
    "tags": ["undead", "brute"]
  }
]
```

### Traps (`data/traps/`)

```json
[
  {
    "key": "necrotic_miasma",
    "name": "Necrotic Miasma",
    "description": "A swirling cloud of necrotic energy drains warmth and hope.",
    "saving_throw": {"ability": "CON", "dc": 14},
    "damage": "3d6 necrotic",
    "tags": ["necrotic", "hazard"]
  }
]
```

### Items (`data/items/`)

```json
{
  "name": "Potion of Vitality",
  "rarity": "Rare",
  "description": "Thick crimson tonic revitalises weary adventurers.",
  "tags": ["potion", "healing"]
}
```

### Themes (`data/themes/`)

Themes stitch the registries together.

```json
{
  "name": "Forgotten Catacombs",
  "description": "Ancient burial halls haunted by restless dead and cloying darkness.",
  "room_templates": [
    {
      "name": "Ossuary Gallery",
      "description": "Stacks of skulls line the walls while candles gutter in stagnant air.",
      "encounter_weights": {"combat": 3, "trap": 1, "empty": 1},
      "weight": 2,
      "tags": ["undead", "relics"]
    }
  ],
  "monsters": ["skeleton", "ghast", "wight"],
  "traps": ["falling_sarcophagus_lid", "necrotic_miasma"],
  "loot": ["ruby_eye_gem", "blessed_reliquary", "potion_of_vitality"],
  "encounters": {"combat": 5, "trap": 3, "treasure": 1, "empty": 1}
}
```

For weighted picks, use an object with a `weight` (or `count`) field: `{"id": "animated_armor", "weight": 3}`.

## Reloading content

Content files are read at startup. To pick up changes without restarting, an administrator can run `/dungeon reload`. The command reloads every registry and clears the per-guild theme cache.

## Guild configuration

The bot stores each guild's preferred theme, catalogue of stored dungeons, and last session details in `data/sessions/metadata.json`. The file is managed automatically when `/dungeon start`, `/dungeon delete`, or `/dungeon configure theme:<name>` are used.
