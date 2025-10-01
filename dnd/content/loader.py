"""Structured content loading helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping, Sequence

import yaml

from .models import EncounterTable, Item, Monster, RoomTemplate, SchemaError, Theme, Trap
from .registry import ItemRegistry, MonsterRegistry, ThemeRegistry, TrapRegistry

__all__ = ["ContentLibrary", "ContentLoadError"]

SUPPORTED_EXTENSIONS = (".json", ".yaml", ".yml")


class ContentLoadError(RuntimeError):
    """Raised when content could not be loaded from disk."""

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        if path is not None:
            message = f"{message} (source: {path})"
        super().__init__(message)
        self.path = path


@dataclass(frozen=True)
class ContentLibrary:
    """Container bundling all loaded content registries."""

    base_path: Path
    monsters: MonsterRegistry
    items: ItemRegistry
    traps: TrapRegistry
    themes: ThemeRegistry

    @classmethod
    def load_from_path(cls, base_path: Path) -> "ContentLibrary":
        loader = _ContentLoader(base_path)
        return loader.load()


class _ContentLoader:
    def __init__(self, base_path: Path) -> None:
        self.base_path = base_path

    # -- public entrypoint -------------------------------------------------
    def load(self) -> ContentLibrary:
        monsters = self._load_monsters()
        traps = self._load_traps()
        items = self._load_items()
        themes = self._load_themes(monsters, items, traps)
        return ContentLibrary(
            base_path=self.base_path,
            monsters=monsters,
            items=items,
            traps=traps,
            themes=themes,
        )

    # -- concrete loaders --------------------------------------------------
    def _load_monsters(self) -> MonsterRegistry:
        registry = MonsterRegistry()
        for file_path, entry in self._iter_entries("monsters"):
            try:
                monster = Monster.from_mapping(entry[0], entry[1])
            except SchemaError as exc:
                raise ContentLoadError(str(exc), path=file_path) from exc
            try:
                registry.register(monster.key, monster)
            except ValueError as exc:
                raise ContentLoadError(str(exc), path=file_path) from exc
        return registry

    def _load_traps(self) -> TrapRegistry:
        registry = TrapRegistry()
        for file_path, entry in self._iter_entries("traps"):
            try:
                trap = Trap.from_mapping(entry[0], entry[1])
            except SchemaError as exc:
                raise ContentLoadError(str(exc), path=file_path) from exc
            try:
                registry.register(trap.key, trap)
            except ValueError as exc:
                raise ContentLoadError(str(exc), path=file_path) from exc
        return registry

    def _load_items(self) -> ItemRegistry:
        registry = ItemRegistry()
        for file_path, entry in self._iter_entries("items"):
            try:
                item = Item.from_mapping(entry[0], entry[1])
            except SchemaError as exc:
                raise ContentLoadError(str(exc), path=file_path) from exc
            try:
                registry.register(item.key, item)
            except ValueError as exc:
                raise ContentLoadError(str(exc), path=file_path) from exc
        return registry

    def _load_themes(
        self,
        monsters: MonsterRegistry,
        items: ItemRegistry,
        traps: TrapRegistry,
    ) -> ThemeRegistry:
        registry = ThemeRegistry()
        for file_path, entry in self._iter_entries("themes"):
            data = entry[1]
            key = entry[0]
            try:
                theme = self._build_theme(file_path, key, data, monsters, items, traps)
            except (SchemaError, ContentLoadError) as exc:
                if isinstance(exc, ContentLoadError):
                    raise
                raise ContentLoadError(str(exc), path=file_path) from exc
            try:
                registry.register(theme.key, theme)
            except ValueError as exc:
                raise ContentLoadError(str(exc), path=file_path) from exc
        return registry

    # -- helpers -----------------------------------------------------------
    def _iter_entries(self, category: str) -> Iterable[tuple[Path, tuple[str, MutableMapping[str, object]]]]:
        path = self.base_path / category
        if not path.exists():
            return []
        files = sorted(
            file_path
            for file_path in path.iterdir()
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        entries: list[tuple[Path, tuple[str, MutableMapping[str, object]]]] = []
        for file_path in files:
            raw = self._load_structured(file_path)
            if isinstance(raw, MutableMapping):
                mapping = dict(raw)
                key = self._extract_key(file_path, mapping)
                entries.append((file_path, (key, mapping)))
            elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                for index, element in enumerate(raw):
                    if not isinstance(element, MutableMapping):
                        raise ContentLoadError(
                            f"Expected mapping entries in {category} definition",
                            path=file_path,
                        )
                    mapping = dict(element)
                    key = self._extract_key(file_path, mapping, suffix=str(index))
                    entries.append((file_path, (key, mapping)))
            else:
                raise ContentLoadError(
                    f"Unsupported structure in {category} content: expected mapping or list of mappings",
                    path=file_path,
                )
        return entries

    def _load_structured(self, file_path: Path) -> object:
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContentLoadError("Unable to read content file", path=file_path) from exc
        suffix = file_path.suffix.lower()
        try:
            if suffix == ".json":
                return json.loads(text)
            if suffix in {".yaml", ".yml"}:
                return yaml.safe_load(text)
        except Exception as exc:
            raise ContentLoadError("Failed to parse structured content", path=file_path) from exc
        raise ContentLoadError(
            f"Unsupported file extension '{file_path.suffix}' for content file",
            path=file_path,
        )

    def _extract_key(
        self,
        file_path: Path,
        mapping: Mapping[str, object],
        *,
        suffix: str | None = None,
    ) -> str:
        for field in ("id", "key", "slug"):
            value = mapping.get(field)
            if isinstance(value, str) and value.strip():
                return value
        stem = file_path.stem
        if suffix is not None:
            stem = f"{stem}-{suffix}"
        return stem

    def _build_theme(
        self,
        file_path: Path,
        key: str,
        data: MutableMapping[str, object],
        monsters: MonsterRegistry,
        items: ItemRegistry,
        traps: TrapRegistry,
    ) -> Theme:
        name = str(data.get("name") or key)
        description = str(data.get("description", ""))
        raw_templates = data.get("room_templates", [])
        templates: list[RoomTemplate] = []
        if raw_templates:
            if not isinstance(raw_templates, Sequence) or isinstance(raw_templates, (str, bytes)):
                raise ContentLoadError("room_templates must be a sequence", path=file_path)
            for template_data in raw_templates:
                if not isinstance(template_data, Mapping):
                    raise ContentLoadError("room_templates entries must be mappings", path=file_path)
                try:
                    templates.append(RoomTemplate.from_mapping(template_data))
                except SchemaError as exc:
                    raise ContentLoadError(str(exc), path=file_path) from exc
        if not templates:
            raise ContentLoadError("Theme must define at least one room template", path=file_path)
        raw_encounters = data.get("encounters")
        try:
            if isinstance(raw_encounters, Mapping):
                encounter_table = EncounterTable({str(key): int(value) for key, value in raw_encounters.items()})
            else:
                encounter_table = EncounterTable({"combat": 3, "trap": 1, "treasure": 1, "empty": 1})
        except SchemaError as exc:
            raise ContentLoadError(str(exc), path=file_path) from exc
        monster_refs = self._resolve_references(file_path, data.get("monsters", []), monsters, "monster")
        trap_refs = self._resolve_references(file_path, data.get("traps", []), traps, "trap")
        loot_refs = self._resolve_references(file_path, data.get("loot", []), items, "item")
        return Theme(
            key=str(key).lower(),
            name=name,
            description=description,
            room_templates=tuple(templates),
            monsters=monster_refs,
            traps=trap_refs,
            loot=loot_refs,
            encounter_table=encounter_table,
        )

    def _resolve_references(
        self,
        file_path: Path,
        raw: object,
        registry,
        category: str,
    ) -> tuple:
        if raw is None:
            return ()
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            resolved = []
            for element in raw:
                weight = 1
                if isinstance(element, str):
                    identifier = element
                elif isinstance(element, Mapping):
                    identifier = element.get("id") or element.get("key") or element.get("name")
                    raw_weight = element.get("weight") if "weight" in element else element.get("count", 1)
                    try:
                        weight = int(raw_weight)
                    except (TypeError, ValueError):
                        raise ContentLoadError(
                            f"Invalid weight for {category} reference", path=file_path
                        ) from None
                else:
                    raise ContentLoadError(
                        f"Invalid {category} reference entry", path=file_path
                    )
                if not identifier:
                    raise ContentLoadError(
                        f"Missing identifier for {category} reference", path=file_path
                    )
                try:
                    target = registry.get(str(identifier))
                except KeyError as exc:
                    raise ContentLoadError(
                        f"Unknown {category} '{identifier}' referenced in theme",
                        path=file_path,
                    ) from exc
                for _ in range(max(1, weight)):
                    resolved.append(target)
            return tuple(resolved)
        raise ContentLoadError(
            f"Expected a sequence of {category} references",
            path=file_path,
        )
