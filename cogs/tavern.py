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
    """Result of recording a ballot within an adventuring party."""

    status: Literal["not_member", "started", "updated", "majority"]
    choice: Optional[str]
    votes_for: int
    required: int
    state_changed: bool = False
    party_name: Optional[str] = None
    party_members: Optional[tuple[int, ...]] = None


@dataclass
class PartyState:
    """Track membership and votes for a specific adventuring party."""

    name: str
    created_at: datetime
    members: list[int] = field(default_factory=list)
    active_vote: Optional[LobbyVote] = None

    def join(self, user_id: int, *, max_size: int) -> Literal["added", "exists", "full"]:
        if user_id in self.members:
            return "exists"
        if len(self.members) >= max_size:
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

    def prune(self, *, now: Optional[datetime] = None, vote_ttl: timedelta) -> bool:
        """Remove stale ballots and expire dormant votes."""

        now = now or datetime.now(timezone.utc)
        changed = False
        if self.active_vote is None:
            return changed

        for user_id in list(self.active_vote.ballots):
            if user_id not in self.members:
                del self.active_vote.ballots[user_id]
                changed = True

        if self.active_vote and self.active_vote.expired(now, vote_ttl):
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
        vote_ttl: timedelta,
    ) -> VoteProgress:
        current_time = now or datetime.now(timezone.utc)
        pruned = self.prune(now=current_time, vote_ttl=vote_ttl)
        if user_id not in self.members:
            return VoteProgress(
                status="not_member",
                choice=None,
                votes_for=0,
                required=self.required_votes(),
                state_changed=pruned,
                party_name=self.name,
                party_members=tuple(sorted(self.members)) or None,
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
                party_name=self.name,
                party_members=tuple(sorted(self.members)),
            )

        return VoteProgress(
            status=status,
            choice=choice,
            votes_for=self.active_vote.counts(self.members).get(choice, 0),
            required=required,
            state_changed=True,
            party_name=self.name,
            party_members=tuple(sorted(self.members)) or None,
        )


class PartyManager:
    """Manage a collection of parties for a guild's tavern."""

    def __init__(self, *, max_size: int = MAX_PARTY_SIZE, vote_ttl: timedelta = VOTE_TTL) -> None:
        self.max_size = max_size
        self.vote_ttl = vote_ttl
        self._parties: dict[str, PartyState] = {}
        self._order: list[str] = []

    def parties(self) -> list[PartyState]:
        return [self._parties[name] for name in self._order if name in self._parties]

    def party_for_member(self, user_id: int) -> Optional[PartyState]:
        for party in self.parties():
            if user_id in party.members:
                return party
        return None

    def _unique_name(self, base_name: str) -> str:
        existing = {name.casefold() for name in self._parties}
        candidate = base_name
        suffix = 2
        while candidate.casefold() in existing:
            candidate = f"{base_name} #{suffix}"
            suffix += 1
        return candidate

    def prune(self, *, now: Optional[datetime] = None) -> bool:
        current_time = now or datetime.now(timezone.utc)
        changed = False
        for party in list(self.parties()):
            if party.prune(now=current_time, vote_ttl=self.vote_ttl):
                changed = True
            if not party.members and party.active_vote is None:
                self._remove_party(party.name)
                changed = True
        return changed

    def _remove_party(self, name: str) -> None:
        if name in self._parties:
            del self._parties[name]
        if name in self._order:
            self._order.remove(name)

    def create_party(
        self,
        owner_id: int,
        display_name: str,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[PartyState, Literal["created", "moved"], bool]:
        current_time = now or datetime.now(timezone.utc)
        state_changed = self.prune(now=current_time)

        previous = self.party_for_member(owner_id)
        if previous is not None:
            result = previous.leave(owner_id)
            if result == "removed":
                state_changed = True
            if previous.prune(now=current_time, vote_ttl=self.vote_ttl):
                state_changed = True
            if not previous.members and previous.active_vote is None:
                self._remove_party(previous.name)
                state_changed = True

        base_name = display_name.strip() or "Adventurer"
        party_name = f"{base_name}’s Party"
        unique_name = self._unique_name(party_name)

        party = PartyState(name=unique_name, created_at=current_time)
        self._parties[unique_name] = party
        self._order.append(unique_name)

        join_result = party.join(owner_id, max_size=self.max_size)
        if join_result != "added":  # pragma: no cover - defensive
            raise RuntimeError("Failed to enrol creator into their party")

        state_changed = True
        status: Literal["created", "moved"] = "moved" if previous else "created"
        return party, status, state_changed

    def join_any(
        self,
        user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[Literal["added", "exists", "full", "no_parties"], Optional[PartyState], bool]:
        current_time = now or datetime.now(timezone.utc)
        state_changed = self.prune(now=current_time)

        current_party = self.party_for_member(user_id)
        if current_party is not None:
            return "exists", current_party, state_changed

        parties = self.parties()
        if not parties:
            return "no_parties", None, state_changed

        for party in parties:
            result = party.join(user_id, max_size=self.max_size)
            if result == "added":
                return "added", party, True
        return "full", None, state_changed

    def leave_member(
        self,
        user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[Literal["removed", "missing"], Optional[PartyState], bool]:
        current_time = now or datetime.now(timezone.utc)
        state_changed = self.prune(now=current_time)

        party = self.party_for_member(user_id)
        if party is None:
            return "missing", None, state_changed

        result = party.leave(user_id)
        if result == "removed":
            state_changed = True
        if party.prune(now=current_time, vote_ttl=self.vote_ttl):
            state_changed = True
        if not party.members and party.active_vote is None:
            self._remove_party(party.name)
            state_changed = True
        return result, party, state_changed

    def record_vote(
        self,
        user_id: int,
        choice: str,
        *,
        now: Optional[datetime] = None,
    ) -> VoteProgress:
        current_time = now or datetime.now(timezone.utc)
        state_changed = self.prune(now=current_time)
        party = self.party_for_member(user_id)
        if party is None:
            return VoteProgress(
                status="not_member",
                choice=None,
                votes_for=0,
                required=1,
                state_changed=state_changed,
                party_name=None,
            )

        progress = party.record_vote(
            user_id,
            choice,
            now=current_time,
            vote_ttl=self.vote_ttl,
        )
        progress.state_changed = progress.state_changed or state_changed
        return progress

    def reset_party(self, party_name: str) -> bool:
        party = self._parties.get(party_name)
        if party is None:
            return False
        party.reset()
        self._remove_party(party_name)
        return True


class PartyJoinSelect(discord.ui.Select):
    """Selection component for choosing a party to join."""

    def __init__(
        self,
        view: "PartyJoinView",
        parties: Sequence[PartyState],
        *,
        max_size: int,
    ) -> None:
        options: list[discord.SelectOption] = []
        for party in parties[:25]:
            label = party.name[:100]
            description = f"{len(party.members)}/{max_size} adventurers"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=party.name,
                    description=description,
                )
            )
        super().__init__(
            placeholder="Choose a party to join",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.join_view = view

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
        await self.join_view.handle_selection(interaction, self.values[0])


class PartyJoinView(discord.ui.View):
    """Ephemeral view that allows a user to select a party to join."""

    def __init__(
        self,
        cog: "Tavern",
        manager: PartyManager,
        *,
        guild_id: int,
        user_id: int,
        parties: Sequence[PartyState],
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.manager = manager
        self.guild_id = guild_id
        self.user_id = user_id
        self.add_item(PartyJoinSelect(self, parties, max_size=manager.max_size))

    def disable_all_items(self) -> None:
        for child in self.children:
            child.disabled = True

    async def handle_selection(self, interaction: discord.Interaction, party_name: str) -> None:
        if interaction.user is None or interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This selection isn't for you.",
                ephemeral=True,
            )
            return

        current_party = self.manager.party_for_member(self.user_id)
        if current_party is not None:
            if current_party.name == party_name:
                message = f"You are already part of **{current_party.name}**."
            else:
                message = (
                    f"You are already part of **{current_party.name}**. Leave that party before joining another."
                )
            self.disable_all_items()
            await interaction.response.edit_message(content=message, view=self)
            self.stop()
            return

        party = next((p for p in self.manager.parties() if p.name == party_name), None)
        if party is None:
            self.disable_all_items()
            await interaction.response.edit_message(
                "That party is no longer gathering. Try joining again.",
                view=self,
            )
            await self.cog.update_tavern_embed(self.guild_id)
            self.stop()
            return

        result = party.join(self.user_id, max_size=self.manager.max_size)
        if result == "full":
            message = f"**{party.name}** filled up before you could join."
        elif result == "exists":  # defensive – should be caught earlier
            message = f"You are already part of **{party.name}**."
        else:
            message = f"You join **{party.name}** and wait for the next adventure."

        self.disable_all_items()
        await interaction.response.edit_message(content=message, view=self)

        if result == "added":
            await self.cog.update_tavern_embed(self.guild_id)

        self.stop()


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

        manager = self.tavern.get_party_manager(guild.id)
        progress = manager.record_vote(interaction.user.id, stored.name)
        if progress.state_changed:
            await self.tavern.update_tavern_embed(guild.id)

        if progress.status == "not_member":
            await interaction.response.send_message(
                "Join the party lobby with **Join Party** before casting a vote.",
                ephemeral=True,
            )
            return

        if progress.status == "majority":
            party_members: Optional[tuple[int, ...]] = progress.party_members
            if party_members is None and progress.party_name is not None:
                party_state = manager._parties.get(progress.party_name)
                if party_state is not None:
                    party_members = tuple(sorted(party_state.members)) or None
            started = await dungeon_cog._start_prepared_dungeon(
                interaction,
                stored,
                party_members=party_members,
            )
            if started:
                if progress.party_name and manager.reset_party(progress.party_name):
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

    def _resolve_manager(self, interaction: discord.Interaction) -> Optional[PartyManager]:
        guild = interaction.guild
        if guild is None:
            return None
        if guild.id != self.guild_id:
            return None
        return self.cog.get_party_manager(guild.id)

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
        manager = self._resolve_manager(interaction)
        if manager is None or interaction.guild is None:
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

        state_changed = manager.prune()

        current_party = manager.party_for_member(interaction.user.id)
        if current_party is not None:
            await interaction.response.send_message(
                f"You are already part of **{current_party.name}**.",
                ephemeral=True,
            )
            if state_changed:
                await self.cog.update_tavern_embed(interaction.guild.id)
            return

        parties = manager.parties()
        available = [party for party in parties if len(party.members) < manager.max_size]

        if not parties:
            await interaction.response.send_message(
                "No parties are gathering yet. Use **Create Party** to start one.",
                ephemeral=True,
            )
            if state_changed:
                await self.cog.update_tavern_embed(interaction.guild.id)
            return

        if not available:
            await interaction.response.send_message(
                "All parties are full. Create a new party to start a fresh expedition.",
                ephemeral=True,
            )
            if state_changed:
                await self.cog.update_tavern_embed(interaction.guild.id)
            return

        view = PartyJoinView(
            self.cog,
            manager,
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            parties=available,
        )
        await interaction.response.send_message(
            "Select a party to join from the list below.",
            view=view,
            ephemeral=True,
        )

        if state_changed:
            await self.cog.update_tavern_embed(interaction.guild.id)

    @discord.ui.button(
        label="Leave Party",
        style=discord.ButtonStyle.danger,
        custom_id="tavern:party_leave",
    )
    async def leave_party(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        manager = self._resolve_manager(interaction)
        if manager is None or interaction.guild is None:
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

        party = manager.party_for_member(interaction.user.id)
        if party is None:
            await interaction.response.send_message(
                "You are not currently part of any party in the tavern.",
                ephemeral=True,
            )
            return

        status, _, changed = manager.leave_member(interaction.user.id)
        if status == "removed":
            message = f"You leave **{party.name}** to rest by the fire."
        else:  # defensive
            message = "You step away from the party."

        await interaction.response.send_message(message, ephemeral=True)

        if changed:
            await self.cog.update_tavern_embed(interaction.guild.id)

    @discord.ui.button(
        label="Create Party",
        style=discord.ButtonStyle.primary,
        custom_id="tavern:party_create",
    )
    async def create_party(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # noqa: D401
        manager = self._resolve_manager(interaction)
        if manager is None or interaction.guild is None:
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
                "Create a character before forming a new party.",
                ephemeral=True,
            )
            return

        party, status, changed = manager.create_party(interaction.user.id, interaction.user.display_name)
        if status == "moved":
            message = f"You form **{party.name}** and lead the way."  # moved from another party
        else:
            message = f"You create **{party.name}** and take the first seat."

        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)

        if changed:
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
        self.party_managers: dict[int, PartyManager] = {}
        self.refresh_views.start()

    def cog_unload(self) -> None:  # noqa: D401 - discord.py hook
        self.refresh_views.cancel()

    def get_party_manager(self, guild_id: int) -> PartyManager:
        manager = self.party_managers.get(guild_id)
        if manager is None:
            manager = PartyManager()
            self.party_managers[guild_id] = manager
        return manager

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
        manager = self.get_party_manager(guild.id)
        manager.prune()

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

        parties = manager.parties()
        if not parties:
            embed.add_field(
                name="Adventuring Parties",
                value=(
                    "No adventurers have rallied yet. Use **Create Party** to start a new group."
                ),
                inline=False,
            )
            embed.set_footer(text="The tavern board refreshes every five minutes.")
            return embed

        embed.add_field(
            name="Adventuring Parties",
            value="Parties currently preparing for upcoming delves:",
            inline=False,
        )

        for party in parties:
            member_header = (
                f"**Members ({len(party.members)}/{manager.max_size})**"
            )
            if party.members:
                member_list = ", ".join(f"<@{user_id}>" for user_id in party.members)
            else:
                member_list = "No members yet."

            status_lines: list[str] = ["**Status**"]
            if not party.members:
                status_lines.append("🚧 Recruiting adventurers.")
            elif party.active_vote is None:
                status_lines.append("🕯️ Awaiting a dungeon vote.")
            else:
                counts = party.active_vote.counts(party.members)
                required = party.required_votes()
                winner, votes_for = party._evaluate_majority()
                expiry = party.active_vote.last_activity + manager.vote_ttl

                if winner:
                    status_lines.append(
                        f"✅ Ready to delve — **{winner}** reached {votes_for}/{required} votes."
                    )
                else:
                    status_lines.append(
                        f"🗳️ Vote in progress — {len(party.active_vote.ballots)}/{len(party.members)} ballots cast."
                    )

                if counts:
                    tally_lines = [
                        f"• {dungeon_name} — {count} vote(s)"
                        for dungeon_name, count in sorted(
                            counts.items(), key=lambda item: (-item[1], item[0].lower())
                        )
                    ]
                    status_lines.extend(tally_lines)
                else:
                    status_lines.append("• Waiting for the first ballot.")

                status_lines.append(f"🕒 Vote expires {format_dt(expiry, style='R')}")

            party_details = "\n".join([member_header, member_list, "", *status_lines])
            embed.add_field(name=party.name, value=party_details, inline=False)

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
