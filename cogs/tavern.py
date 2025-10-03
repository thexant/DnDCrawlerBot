"""Tavern hub management for coordinating adventurers between dungeons."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional, Sequence, Set, TYPE_CHECKING

import discord
from discord import app_commands
from discord.utils import format_dt
from discord.ext import commands, tasks

from dnd import (
    Character,
    CharacterRepository,
    InsufficientFunds,
    ItemNotCarried,
    TavernConfig,
    TavernConfigStore,
    TavernShop,
)
from dnd.dungeon.state import StoredDungeon

if TYPE_CHECKING:  # pragma: no cover - typing helper
    from cogs.dungeon import DungeonCog
    from discord import Message


log = logging.getLogger(__name__)

TAVERN_ROLE_NAME = "Tavern Adventurer"
MAX_PARTY_SIZE = 4
VOTE_TTL = timedelta(minutes=10)


@dataclass
class LobbyVote:
    """Track ballots for a prepared dungeon selection vote."""

    started_at: datetime
    last_activity: datetime
    ballots: dict[int, str] = field(default_factory=dict)

    def touch(self, now: datetime) -> None:
        self.last_activity = now

    def expired(self, now: datetime, ttl: timedelta) -> bool:
        return now - self.last_activity >= ttl

    def counts(self, members: Sequence[int]) -> dict[str, int]:
        tally: dict[str, int] = {}
        for user_id in members:
            choice = self.ballots.get(user_id)
            if choice is None:
                continue
            tally[choice] = tally.get(choice, 0) + 1
        return tally


@dataclass
class VoteProgress:
    """Result of recording a ballot in the party lobby."""

    status: Literal["not_member", "started", "updated", "majority"]
    choice: Optional[str]
    votes_for: int
    required: int
    state_changed: bool = False


class PartyLobby:
    """Manage party membership and voting for a guild lobby."""

    def __init__(self, *, max_size: int = MAX_PARTY_SIZE, vote_ttl: timedelta = VOTE_TTL) -> None:
        self.max_size = max_size
        self.vote_ttl = vote_ttl
        self.members: list[int] = []
        self.active_vote: Optional[LobbyVote] = None

    def join(self, user_id: int) -> Literal["added", "exists", "full"]:
        if user_id in self.members:
            return "exists"
        if len(self.members) >= self.max_size:
            return "full"
        self.members.append(user_id)
        return "added"

    def leave(self, user_id: int) -> Literal["removed", "missing"]:
        if user_id not in self.members:
            return "missing"
        self.members.remove(user_id)
        return "removed"

    def reset(self) -> None:
        self.members.clear()
        self.active_vote = None

    def clear_vote(self) -> None:
        self.active_vote = None

    def prune(self, now: Optional[datetime] = None) -> bool:
        """Remove stale ballots and clear empty votes."""

        now = now or datetime.now(timezone.utc)
        changed = False
        if self.active_vote is None:
            return changed

        for user_id in list(self.active_vote.ballots):
            if user_id not in self.members:
                del self.active_vote.ballots[user_id]
                changed = True

        if self.active_vote and self.active_vote.expired(now, self.vote_ttl):
            self.active_vote = None
            return True

        if self.active_vote and not self.active_vote.ballots:
            self.active_vote = None
            return True

        return changed

    def required_votes(self) -> int:
        return max(1, (len(self.members) // 2) + 1)

    def _evaluate_majority(self) -> tuple[Optional[str], int]:
        if not self.active_vote:
            return None, 0
        counts = self.active_vote.counts(self.members)
        required = self.required_votes()
        for name, count in counts.items():
            if count >= required:
                return name, count
        return None, 0

    def record_vote(
        self,
        user_id: int,
        choice: str,
        *,
        now: Optional[datetime] = None,
    ) -> VoteProgress:
        current_time = now or datetime.now(timezone.utc)
        pruned = self.prune(current_time)
        if user_id not in self.members:
            return VoteProgress(
                status="not_member",
                choice=None,
                votes_for=0,
                required=self.required_votes(),
                state_changed=pruned,
            )

        if self.active_vote is None:
            self.active_vote = LobbyVote(started_at=current_time, last_activity=current_time)
            status: Literal["started", "updated", "majority"] = "started"
        else:
            status = "updated"
        self.active_vote.ballots[user_id] = choice
        self.active_vote.touch(current_time)

        winner, votes_for = self._evaluate_majority()
        required = self.required_votes()
        if winner is not None:
            return VoteProgress(
                status="majority",
                choice=winner,
                votes_for=votes_for,
                required=required,
                state_changed=True,
            )

        return VoteProgress(
            status=status,
            choice=choice,
            votes_for=self.active_vote.counts(self.members).get(choice, 0),
            required=required,
            state_changed=True,
        )


class DungeonMapSelect(discord.ui.Select):
    """Select menu for choosing a prepared dungeon to explore."""

    def __init__(
        self,
        tavern: "Tavern",
        *,
        guild_id: int,
        dungeons: Sequence[StoredDungeon],
    ) -> None:
        options: list[discord.SelectOption] = []
        for dungeon in dungeons[:25]:
            details: list[str] = []
            if dungeon.difficulty:
                details.append(dungeon.difficulty.title())
            if dungeon.room_count:
                details.append(f"{dungeon.room_count} rooms")
            if dungeon.seed is not None:
                details.append(f"Seed {dungeon.seed}")
            description = ", ".join(details) or "Ready for adventure"
            options.append(
                discord.SelectOption(
                    label=dungeon.name[:100],
                    value=dungeon.name,
                    description=description[:100],
                )
            )
        placeholder = "Select a prepared dungeon to begin"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="tavern:dungeon_select",
        )
        self.tavern = tavern
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
        dungeon_cog = self.tavern._get_dungeon_cog()
        if dungeon_cog is None:
            await interaction.response.send_message(
                "Dungeon operations are currently unavailable.",
                ephemeral=True,
            )
            return

        choice = self.values[0]
        stored = await dungeon_cog.metadata_store.get_dungeon(self.guild_id, choice)
        if stored is None:
            await interaction.response.send_message(
                "That expedition is no longer on the map. Try refreshing the tavern board.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Expeditions can only be planned from within a guild channel.",
                ephemeral=True,
            )
            return

        lobby = self.tavern.get_lobby(guild.id)
        progress = lobby.record_vote(interaction.user.id, stored.name)
        if progress.state_changed:
            await self.tavern.update_tavern_embed(guild.id)

        if progress.status == "not_member":
            await interaction.response.send_message(
                "Join the party lobby with **Join Party** before casting a vote.",
                ephemeral=True,
            )
            return

        if progress.status == "majority":
            started = await dungeon_cog._start_prepared_dungeon(interaction, stored)
            if started:
                lobby.reset()
                await self.tavern.update_tavern_embed(guild.id)
            elif not interaction.response.is_done():
                await interaction.response.send_message(
                    "Unable to start the expedition. Resolve any issues and vote again.",
                    ephemeral=True,
                )
            return

        if interaction.response.is_done():
            return

        if progress.status == "started":
            message = (
                f"A new expedition vote has begun for **{progress.choice}**. "
                f"{progress.votes_for}/{progress.required} votes recorded."
            )
        else:
            message = (
                f"Your vote for **{progress.choice}** is logged. "
                f"{progress.votes_for}/{progress.required} votes recorded."
            )
        await interaction.response.send_message(message, ephemeral=True)


class DungeonMapView(discord.ui.View):
    """View wrapper for the dungeon selection dropdown."""

    def __init__(
        self,
        tavern: "Tavern",
        *,
        guild_id: int,
        dungeons: Sequence[StoredDungeon],
    ) -> None:
        super().__init__(timeout=120)
        self.add_item(DungeonMapSelect(tavern, guild_id=guild_id, dungeons=dungeons))


class ShopBuySelect(discord.ui.Select):
    """Dropdown for selecting items to purchase."""

    def __init__(self, view: "TavernShopView") -> None:
        super().__init__(
            placeholder="Buy an item",
            min_values=1,
            max_values=1,
            options=[],
            custom_id="tavern:shop_buy",
        )
        self.shop_view = view

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
        choice = self.values[0]
        await self.shop_view.handle_purchase(interaction, choice)


class ShopSellSelect(discord.ui.Select):
    """Dropdown for selecting items to sell back to the trader."""

    def __init__(self, view: "TavernShopView") -> None:
        super().__init__(
            placeholder="Sell an item",
            min_values=1,
            max_values=1,
            options=[],
            custom_id="tavern:shop_sell",
        )
        self.shop_view = view

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
        choice = self.values[0]
        await self.shop_view.handle_sale(interaction, choice)


class LeaveShopButton(discord.ui.Button):
    """Button allowing the player to close the shop view."""

    def __init__(self) -> None:
        super().__init__(
            label="Leave Shop",
            style=discord.ButtonStyle.danger,
            custom_id="tavern:shop_leave",
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
        view = self.view
        if not isinstance(view, TavernShopView):
            await interaction.response.defer()
            return
        view.stop()
        for child in view.children:
            child.disabled = True
        embed = view.build_embed()
        await interaction.response.edit_message(
            content="You leave the shop behind.",
            embed=embed,
            view=view,
        )


class TavernShopView(discord.ui.View):
    """Interactive trading interface for the tavern shop."""

    def __init__(
        self,
        cog: "Tavern",
        *,
        guild_id: int,
        user_id: int,
        character: Character,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.character = character
        self.message: Optional["Message"] = None
        self.status: Optional[str] = None
        self.buy_select = ShopBuySelect(self)
        self.sell_select = ShopSellSelect(self)
        self.add_item(self.buy_select)
        self.add_item(self.sell_select)
        self.add_item(LeaveShopButton())
        self._refresh_controls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # noqa: D401
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the hero who opened the shop may trade right now.",
                ephemeral=True,
            )
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Tavern Shop",
            description=(
                "The shopkeeper eyes you keenly, ready to barter goods for Gold Coins."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Your Purse",
            value=f"{self.character.gold_coins} Gold Coins",
            inline=False,
        )
        inventory_lines = [f"• {entry}" for entry in self.character.inventory]
        embed.add_field(
            name="Inventory",
            value="\n".join(inventory_lines) if inventory_lines else "Empty",
            inline=False,
        )
        stock_lines: list[str] = []
        for item in self.cog.shop.list_items():
            stock_lines.append(f"{item.name} — {item.price} Gold Coins")
        display_stock = stock_lines[:25]
        embed.add_field(
            name="Available Goods",
            value="\n".join(display_stock),
            inline=False,
        )
        if self.status:
            embed.add_field(name="Latest Transaction", value=self.status, inline=False)
        embed.set_footer(text="Use the menus below to buy or sell items.")
        return embed

    def _refresh_controls(self) -> None:
        buy_options: list[discord.SelectOption] = []
        for item in self.cog.shop.list_items()[:25]:
            buy_options.append(
                discord.SelectOption(
                    label=item.name[:100],
                    value=item.key,
                    description=f"{item.price} Gold Coins"[:100],
                )
            )
        self.buy_select.options = buy_options
        self.buy_select.disabled = not buy_options

        sell_entries = self.cog.shop.items_from_inventory(self.character.inventory)
        sell_options: list[discord.SelectOption] = []
        for item, count in sell_entries[:25]:
            label = item.name if count == 1 else f"{item.name} ({count})"
            description = f"Sell for {item.resale_value} Gold Coins"
            sell_options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=item.key,
                    description=description[:100],
                )
            )
        self.sell_select.options = sell_options
        self.sell_select.disabled = not sell_options

    async def handle_purchase(self, interaction: discord.Interaction, item_key: str) -> None:
        item = self.cog.shop.get(item_key)
        if item is None:
            self.status = "That item is no longer available."
            await self._edit(interaction)
            return
        try:
            updated = self.cog.shop.purchase(self.character, item_key)
        except InsufficientFunds:
            self.status = f"You cannot afford {item.name}."
            await self._edit(interaction)
            return
        except ItemNotCarried:
            self.status = "The shopkeeper cannot locate that item."  # defensive
            await self._edit(interaction)
            return
        self.character = updated
        await self.cog.characters.save(updated)
        self.status = f"Purchased {item.name} for {item.price} Gold Coins."
        self._refresh_controls()
        await self._edit(interaction)

    async def handle_sale(self, interaction: discord.Interaction, item_key: str) -> None:
        item = self.cog.shop.get(item_key)
        if item is None:
            self.status = "The shopkeeper isn't buying that right now."
            await self._edit(interaction)
            return
        try:
            updated = self.cog.shop.sell(self.character, item_key)
        except ItemNotCarried:
            self.status = f"You aren't carrying any {item.name}."
            await self._edit(interaction)
            return
        self.character = updated
        await self.cog.characters.save(updated)
        self.status = f"Sold {item.name} for {item.resale_value} Gold Coins."
        self._refresh_controls()
        await self._edit(interaction)

    async def _edit(self, interaction: discord.Interaction) -> None:
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:  # noqa: D401
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        embed = self.build_embed()
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

class TavernControlView(discord.ui.View):
    """Interactive controls for the tavern hub embed."""

    def __init__(self, cog: "Tavern", *, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    def _resolve_lobby(self, interaction: discord.Interaction) -> Optional[PartyLobby]:
        guild = interaction.guild
        if guild is None:
            return None
        if guild.id != self.guild_id:
            return None
        return self.cog.get_lobby(guild.id)

    @discord.ui.button(
        label="Visit the Shop",
        style=discord.ButtonStyle.secondary,
        custom_id="tavern:shop",
    )
    async def visit_shop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        if not interaction.guild:
            await interaction.response.send_message(
                "The tavern shop is only available within a server.",
                ephemeral=True,
            )
            return
        character = await self.cog.characters.get(interaction.guild.id, interaction.user.id)
        if character is None:
            await interaction.response.send_message(
                "You need to finish creating a character before trading in the tavern.",
                ephemeral=True,
            )
            return
        view = TavernShopView(self.cog, guild_id=interaction.guild.id, user_id=interaction.user.id, character=character)
        embed = view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            view.message = None

    @discord.ui.button(
        label="Join Party",
        style=discord.ButtonStyle.success,
        custom_id="tavern:party_join",
    )
    async def join_party(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        lobby = self._resolve_lobby(interaction)
        if lobby is None or interaction.guild is None:
            await interaction.response.send_message(
                "The party lobby is only available inside the tavern's guild.",
                ephemeral=True,
            )
            return

        if interaction.user is None:
            await interaction.response.send_message(
                "Unable to resolve who clicked the button. Try again.",
                ephemeral=True,
            )
            return

        has_character = await self.cog.characters.exists(interaction.guild.id, interaction.user.id)
        if not has_character:
            await interaction.response.send_message(
                "Create a character before joining the party lobby.",
                ephemeral=True,
            )
            return

        pruned = lobby.prune()
        result = lobby.join(interaction.user.id)
        if result == "full":
            message = "The party is already full."
        elif result == "exists":
            message = "You are already waiting in the party lobby."
        else:
            message = "You take a seat with the adventuring party."

        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)

        if pruned or result == "added":
            await self.cog.update_tavern_embed(interaction.guild.id)

    @discord.ui.button(
        label="Leave Party",
        style=discord.ButtonStyle.danger,
        custom_id="tavern:party_leave",
    )
    async def leave_party(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        lobby = self._resolve_lobby(interaction)
        if lobby is None or interaction.guild is None:
            await interaction.response.send_message(
                "The party lobby is only available inside the tavern's guild.",
                ephemeral=True,
            )
            return

        pruned = lobby.prune()
        result = lobby.leave(interaction.user.id)
        if result == "missing":
            message = "You are not currently part of the party lobby."
        else:
            message = "You leave the party to rest by the fire."

        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)

        if pruned or result == "removed":
            await self.cog.update_tavern_embed(interaction.guild.id)

    @discord.ui.button(
        label="View Dungeon Map",
        style=discord.ButtonStyle.primary,
        custom_id="tavern:map",
    )
    async def view_map(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        if not interaction.guild:
            await interaction.response.send_message(
                "Dungeon information is only available within a server.",
                ephemeral=True,
            )
            return
        embed, view, fallback = await self.cog.build_dungeon_map_components(interaction.guild.id)
        if embed is None or view is None:
            await interaction.response.send_message(fallback, ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class Tavern(commands.GroupCog, name="tavern", description="Configure the guild's tavern hub"):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        data_path = Path("data")
        self.config_store = TavernConfigStore(data_path / "taverns.json")
        self.characters = CharacterRepository(data_path / "characters.json")
        self.shop = TavernShop.default_shop()
        self.lobbies: dict[int, PartyLobby] = {}
        self.refresh_views.start()

    def cog_unload(self) -> None:  # noqa: D401 - discord.py hook
        self.refresh_views.cancel()

    def get_lobby(self, guild_id: int) -> PartyLobby:
        lobby = self.lobbies.get(guild_id)
        if lobby is None:
            lobby = PartyLobby()
            self.lobbies[guild_id] = lobby
        return lobby

    async def update_tavern_embed(self, guild_id: int) -> None:
        config = await self.config_store.get_config(guild_id)
        if (
            config is None
            or config.channel_id is None
            or config.message_id is None
        ):
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            message = await channel.fetch_message(config.message_id)
        except (discord.NotFound, discord.HTTPException):
            return

        embed = self._build_tavern_embed(guild)
        view = TavernControlView(self, guild_id=guild_id)
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            log.debug("Failed to update tavern embed in %s", channel.name)

    def _build_tavern_embed(self, guild: discord.Guild) -> discord.Embed:
        lobby = self.get_lobby(guild.id)
        lobby.prune()

        embed = discord.Embed(
            title="The Adventurers' Tavern",
            description=(
                "A warm hearth welcomes heroes between expeditions. Share tales, "
                "recruit allies, and prepare for the next delve."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Planning",
            value="Use the buttons below to browse services before embarking on your next dungeon run.",
            inline=False,
        )

        if lobby.members:
            lines: list[str] = []
            for user_id in lobby.members:
                mention = f"<@{user_id}>"
                status = "⏳ Pending"
                if lobby.active_vote:
                    choice = lobby.active_vote.ballots.get(user_id)
                    if choice:
                        status = f"🗳️ {choice}"
                lines.append(f"• {mention} — {status}")
            party_value = "\n".join(lines)
        else:
            party_value = "No adventurers have rallied yet. Use **Join Party** to form a group."
        embed.add_field(name="Party Lobby", value=party_value, inline=False)

        if lobby.active_vote and lobby.active_vote.ballots:
            counts = lobby.active_vote.counts(lobby.members)
            if counts:
                vote_lines: list[str] = []
                for dungeon_name, count in sorted(
                    counts.items(), key=lambda item: (-item[1], item[0].lower())
                ):
                    vote_lines.append(f"• **{dungeon_name}** — {count} vote(s)")
                expiry = lobby.active_vote.last_activity + lobby.vote_ttl
                vote_lines.append(f"Votes expire {format_dt(expiry, style='R')}")
                vote_lines.append(f"{lobby.required_votes()} vote(s) needed to delve.")
                vote_value = "\n".join(vote_lines)
            else:
                vote_value = "Waiting for the first ballot."
        else:
            vote_value = "No vote in progress."
        embed.add_field(name="Active Expedition Vote", value=vote_value, inline=False)

        embed.set_footer(text="The tavern board refreshes every five minutes.")
        return embed

    async def build_dungeon_map_components(
        self, guild_id: int
    ) -> tuple[Optional[discord.Embed], Optional[discord.ui.View], str]:
        dungeon_cog = self._get_dungeon_cog()
        if dungeon_cog is None:
            return (
                None,
                None,
                "Dungeon operations are currently unavailable.",
            )

        dungeons = await dungeon_cog.metadata_store.list_dungeons(guild_id)
        if not dungeons:
            return (
                None,
                None,
                (
                    "No expeditions are prepared yet. Ask an administrator to use "
                    "/dungeon prepare so the map can be charted."
                ),
            )

        display_dungeons = list(dungeons[:25])
        lines: list[str] = []
        for stored in display_dungeons:
            try:
                theme = dungeon_cog.theme_registry.get(stored.theme)
                theme_name = theme.name
            except KeyError:
                theme_name = stored.theme
            details: list[str] = [theme_name]
            if stored.difficulty:
                details.append(stored.difficulty.title())
            if stored.room_count:
                details.append(f"{stored.room_count} rooms")
            if stored.seed is not None:
                details.append(f"Seed {stored.seed}")
            summary = ", ".join(details)
            lines.append(f"• **{stored.name}** — {summary}")

        embed = discord.Embed(
            title="Dungeon Map",
            description="Select a prepared expedition from the map below.",
            color=discord.Color.dark_purple(),
        )
        embed.add_field(
            name="Prepared Expeditions",
            value="\n".join(lines),
            inline=False,
        )
        if len(dungeons) > len(display_dungeons):
            embed.set_footer(text="Only the first 25 expeditions are shown. Use /dungeon start for others.")
        else:
            embed.set_footer(text="Choose an expedition to rally the party.")

        view = DungeonMapView(self, guild_id=guild_id, dungeons=display_dungeons)
        return embed, view, ""

    @app_commands.command(name="set", description="Designate a channel as the guild's tavern hub")
    @app_commands.describe(channel="Text channel to host the tavern. Defaults to the current channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_tavern(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Tavern configuration is only available inside a server.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel) or target.guild != interaction.guild:
            await interaction.response.send_message(
                "Please choose a text channel from this server to host the tavern.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        previous = await self.config_store.get_config(interaction.guild.id)
        new_config = await self.config_store.set_channel(interaction.guild.id, target.id)

        if previous and previous.message_id and previous.channel_id != target.id:
            await self._delete_previous_message(interaction.guild, previous)

        await self._refresh_tavern_for_config(new_config)

        channel_mention = target.mention
        await interaction.followup.send(f"The tavern is now located in {channel_mention}.", ephemeral=True)

    @tasks.loop(minutes=5)
    async def refresh_views(self) -> None:
        configs = await self.config_store.all_configs()
        for config in configs:
            await self._refresh_tavern_for_config(config)

    @refresh_views.before_loop
    async def before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    def _get_dungeon_cog(self) -> Optional["DungeonCog"]:
        cog = self.bot.get_cog("DungeonCog")
        if cog is None:
            return None
        try:
            from cogs.dungeon import DungeonCog
        except ImportError:  # pragma: no cover - defensive
            return None
        return cog if isinstance(cog, DungeonCog) else None

    async def refresh_tavern_access(self, guild_id: int) -> None:
        """Synchronise tavern role membership for ``guild_id``."""

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        try:
            role = await self._ensure_tavern_role(guild)
        except discord.HTTPException as exc:
            log.debug("Unable to ensure tavern role in %s: %s", guild.name, exc)
            return
        allowed = await self._eligible_member_ids(guild_id)
        await self._sync_role_membership(role, allowed)

    async def _refresh_tavern_for_config(self, config: TavernConfig) -> None:
        guild = self.bot.get_guild(config.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        await self._sync_channel_access(channel)
        await self._refresh_tavern_embed(channel, config)

    async def _sync_channel_access(self, channel: discord.TextChannel) -> None:
        guild = channel.guild
        try:
            role = await self._ensure_tavern_role(guild)
        except discord.HTTPException:
            log.warning("Missing permissions to manage tavern role in %s", guild.name)
            return
        allowed = await self._eligible_member_ids(guild.id)
        await self._sync_role_membership(role, allowed)

        overwrites = channel.overwrites_for(guild.default_role)
        if overwrites.view_channel is not False:
            try:
                await channel.set_permissions(guild.default_role, view_channel=False)
            except discord.HTTPException:
                log.warning("Failed to update default role permissions for tavern in %s", guild.name)

        role_overwrites = channel.overwrites_for(role)
        changed = False
        if role_overwrites.view_channel is not True or role_overwrites.send_messages is not True:
            role_overwrites.view_channel = True
            role_overwrites.send_messages = True
            role_overwrites.read_message_history = True
            changed = True
        if changed:
            try:
                await channel.set_permissions(role, overwrite=role_overwrites)
            except discord.HTTPException:
                log.warning("Failed to apply tavern role permissions in %s", guild.name)

    async def _refresh_tavern_embed(self, channel: discord.TextChannel, config: TavernConfig) -> None:
        if config.message_id:
            try:
                message = await channel.fetch_message(config.message_id)
            except (discord.NotFound, discord.HTTPException):
                message = None
            else:
                try:
                    await message.delete()
                except discord.HTTPException:
                    log.debug("Could not delete previous tavern embed in %s", channel.name)

        embed = self._build_tavern_embed(channel.guild)
        view = TavernControlView(self, guild_id=channel.guild.id)
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            log.warning("Failed to send tavern embed in %s", channel.name)
            await self.config_store.update_message(channel.guild.id, None)
            return
        await self.config_store.update_message(channel.guild.id, message.id)

    async def _delete_previous_message(self, guild: discord.Guild, config: TavernConfig) -> None:
        channel = guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(config.message_id)  # type: ignore[arg-type]
        except (discord.NotFound, discord.HTTPException, TypeError):
            return
        try:
            await message.delete()
        except discord.HTTPException:
            log.debug("Unable to delete previous tavern embed in %s", channel.name)

    async def _ensure_tavern_role(self, guild: discord.Guild) -> discord.Role:
        role = discord.utils.get(guild.roles, name=TAVERN_ROLE_NAME)
        if role is None:
            try:
                role = await guild.create_role(name=TAVERN_ROLE_NAME, reason="Tavern access role")
            except discord.HTTPException as exc:
                log.warning("Failed to create tavern role in %s: %s", guild.name, exc)
                raise
        return role

    async def _eligible_member_ids(self, guild_id: int) -> Set[int]:
        characters = await self.characters.list_guild_characters(guild_id)
        if not characters:
            return set()
        dungeon_cog = self._get_dungeon_cog()
        active: Set[int] = set()
        if dungeon_cog is not None:
            sessions = await dungeon_cog.sessions.values()
            for session in sessions:
                if session.guild_id == guild_id:
                    active.update(session.party_ids)
        return {user_id for user_id in characters if user_id not in active}

    async def _sync_role_membership(self, role: discord.Role, allowed: Set[int]) -> None:
        guild = role.guild
        current_ids = {member.id for member in role.members}
        to_add = allowed - current_ids
        to_remove = current_ids - allowed

        for member_id in to_add:
            member = guild.get_member(member_id)
            if member is None:
                continue
            try:
                await member.add_roles(role, reason="Granted tavern access")
            except discord.HTTPException:
                log.debug("Failed to add tavern role to %s in %s", member, guild.name)

        for member_id in to_remove:
            member = guild.get_member(member_id)
            if member is None:
                continue
            try:
                await member.remove_roles(role, reason="Tavern access revoked")
            except discord.HTTPException:
                log.debug("Failed to remove tavern role from %s in %s", member, guild.name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tavern(bot))
