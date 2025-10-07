"""Utilities for rendering dungeon maps as raster images."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence, TYPE_CHECKING

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError:  # pragma: no cover - environment without Pillow
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

if TYPE_CHECKING:  # pragma: no cover - typing helpers
    from PIL import Image as PILImage
else:  # pragma: no cover - executed at runtime without affecting behaviour
    PILImage = None  # type: ignore[assignment]

from dnd.dungeon.generator import Corridor, Room

__all__ = ["RenderConfig", "render_dungeon_map"]


@dataclass(frozen=True)
class RenderConfig:
    """Configuration controlling how dungeon maps are rendered."""

    tile_size: int = 192
    margin: int = 48
    corridor_width: int = 14
    room_width_ratio: float = 0.68
    room_height_ratio: float = 0.52
    background: tuple[int, int, int, int] = (16, 17, 23, 255)
    room_fill: tuple[int, int, int, int] = (54, 59, 82, 255)
    room_outline: tuple[int, int, int, int] = (206, 214, 242, 255)
    highlight_fill: tuple[int, int, int, int] = (88, 129, 189, 255)
    highlight_outline: tuple[int, int, int, int] = (233, 242, 255, 255)
    corridor_colour: tuple[int, int, int, int] = (134, 142, 170, 255)
    label_colour: tuple[int, int, int, int] = (240, 245, 255, 255)


def render_dungeon_map(
    *,
    rooms: Sequence[Room],
    corridors: Sequence[Corridor],
    positions: Mapping[int, tuple[int, int]],
    current_room: int,
    config: RenderConfig | None = None,
) -> "PILImage.Image":
    """Render the provided dungeon layout as a :class:`PIL.Image`.

    Parameters
    ----------
    rooms:
        The rooms that will be drawn on the map. Only the room ``id`` is used
        for rendering, so this can be a lightweight subset of the dungeon data.
    corridors:
        The corridors connecting the rooms.
    positions:
        A mapping from room id to logical ``(x, y)`` coordinates.
    current_room:
        The room id that should be highlighted.
    config:
        Optional rendering configuration. If omitted, :class:`RenderConfig`
        defaults will be used.
    """

    if not positions:
        raise ValueError("A dungeon must contain at least one positioned room")

    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("Pillow is required to render dungeon maps")

    config = config or RenderConfig()
    xs = [coord[0] for coord in positions.values()]
    ys = [coord[1] for coord in positions.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    tile_size = max(32, config.tile_size)
    corridor_width = max(2, config.corridor_width)
    width_tiles = max_x - min_x + 1
    height_tiles = max_y - min_y + 1

    image_width = width_tiles * tile_size + config.margin * 2
    image_height = height_tiles * tile_size + config.margin * 2

    image = Image.new("RGBA", (image_width, image_height), config.background)
    draw = ImageDraw.Draw(image)

    def room_center(room_id: int) -> tuple[int, int]:
        grid_x, grid_y = positions[room_id]
        x_index = grid_x - min_x
        y_index = max_y - grid_y
        center_x = config.margin + int((x_index + 0.5) * tile_size)
        center_y = config.margin + int((y_index + 0.5) * tile_size)
        return center_x, center_y

    # Draw corridors first so rooms are layered on top.
    for corridor in corridors:
        if (
            corridor.from_room not in positions
            or corridor.to_room not in positions
        ):
            continue
        start = room_center(corridor.from_room)
        end = room_center(corridor.to_room)
        draw.line([start, end], fill=config.corridor_colour, width=corridor_width)

    room_half_width = int(tile_size * config.room_width_ratio / 2)
    room_half_height = int(tile_size * config.room_height_ratio / 2)
    corner_radius = max(8, min(room_half_width, room_half_height) // 3)

    try:
        font = ImageFont.load_default()
    except OSError:
        font = None

    for room in rooms:
        if room.id not in positions:
            continue
        center_x, center_y = room_center(room.id)
        left = center_x - room_half_width
        right = center_x + room_half_width
        top = center_y - room_half_height
        bottom = center_y + room_half_height

        if room.id == current_room:
            fill_colour = config.highlight_fill
            outline_colour = config.highlight_outline
        else:
            fill_colour = config.room_fill
            outline_colour = config.room_outline

        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=corner_radius,
            fill=fill_colour,
            outline=outline_colour,
            width=4,
        )

        if font is not None:
            label = f"{room.id + 1:02d}"
            text_width, text_height = draw.textsize(label, font=font)
            text_position = (
                center_x - text_width // 2,
                center_y - text_height // 2,
            )
            draw.text(text_position, label, fill=config.label_colour, font=font)

    return image

