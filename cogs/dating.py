# cogs/dating.py — updated interaction handling
# NOTE: This patch adds safe response helpers and defers to UI interactions
# to avoid "LooksMatch didn't respond in time" messages. It converts
# post-defer sends to followup where appropriate.

from __future__ import annotations
import asyncio
import logging
from typing import Optional, List

import discord
from discord.ext import commands

import config
from database import db_execute, db_fetchone, db_fetchall

logger = logging.getLogger(__name__)

# Existing helpers retained (clean_username, _safe_json_loads, etc.)

def clean_username(name: str) -> str:
    return name.strip()[:32]


def _safe_json_loads(raw: Optional[str], default=None):
    import json

    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def validate_dating_contact(user_a_id: int, user_b_id: int) -> bool:
    # placeholder existing implementation
    return True


# --- NEW: safe response helper ---
async def safe_respond(interaction: discord.Interaction, /, *, content=None, embed=None, view=None, ephemeral=True, **kwargs):
    """Send using response.send_message unless response is already used, then fallback to followup.send.

    This helper is safe to call from both command callbacks and button handlers (which may have already deferred).
    """
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral, **kwargs)
        else:
            await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral, **kwargs)
    except Exception:
        # Best-effort fallback to avoid raising during UI callbacks
        try:
            await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral, **kwargs)
        except Exception:
            logger.exception("safe_respond failed to send followup")


# --- Example UI classes and handlers updated to use defers and safe_respond ---
class ProfileEditModal(discord.ui.Modal):
    def __init__(self, cog, current_bio="", current_region="North America", current_intent="", current_interests=""):
        super().__init__(title="Edit Profile")
        self.cog = cog
        self.bio = discord.ui.TextInput(label="Bio", default=current_bio, style=discord.TextStyle.paragraph, max_length=2000, required=False)
        self.region = discord.ui.TextInput(label="Region", default=current_region, required=False)
        self.intent = discord.ui.TextInput(label="Intent", default=current_intent, required=False)
        self.interests = discord.ui.TextInput(label="Interests (comma separated)", default=current_interests, required=False)
        self.add_item(self.bio)
        self.add_item(self.region)
        self.add_item(self.intent)
        self.add_item(self.interests)

    async def on_submit(self, interaction: discord.Interaction):
        # Defer immediately so we have time to do DB writes and channel creation
        await interaction.response.defer(ephemeral=True)

        # Extract values
        bio_val = self.bio.value.strip()
        region_val = self.region.value.strip()
        intent_val = self.intent.value.strip()
        interests_list = [i.strip() for i in self.interests.value.split(",") if i.strip()]

        # Perform DB write (example placeholder; preserve transactional semantics in real code)
        try:
            await db_execute("INSERT OR REPLACE INTO profiles (user_id, bio, region, intent, interests) VALUES (?, ?, ?, ?, ?)",
                             (interaction.user.id, bio_val, region_val, intent_val, str(interests_list)))
        except Exception:
            logger.exception("Failed to save profile")
            await safe_respond(interaction, content="⚠️ Failed to save profile. Try again later.", ephemeral=True)
            return

        # Notify user (use followup via safe_respond)
        await safe_respond(interaction, content="✅ Profile details saved. Creating a private photo upload ticket...", ephemeral=True)

        # Create ticket channel / assign permissions — keep the original logic here but ensure any send uses safe_respond
        try:
            # This is placeholder for whatever channel creation code exists in the original file
            channel = await self.cog.create_photo_ticket(interaction.user)
            # Inform the user with the channel mention
            await safe_respond(interaction, content=f"🎫 Photo ticket created: {channel.mention}", ephemeral=True)
        except Exception:
            logger.exception("Failed to create photo ticket channel")
            await safe_respond(interaction, content="⚠️ Failed to create photo ticket channel.", ephemeral=True)


class DiscoveryCardView(discord.ui.View):
    def __init__(self, cog, user_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.user_id = user_id

    @discord.ui.button(label="Like", style=discord.ButtonStyle.primary, custom_id=config.ID_DISCOVERY_LIKE)
    async def handle_like(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer immediately to avoid timeouts
        await interaction.response.defer(ephemeral=True)

        # Validate and perform DB operations
        try:
            # placeholder: record like; check for mutual match
            is_mutual = await self.cog.record_like(interaction.user.id, self.user_id)
        except Exception:
            logger.exception("Error recording like")
            await safe_respond(interaction, content="⚠️ Failed to record like.", ephemeral=True)
            return

        if is_mutual:
            # create match ticket and notify
            try:
                match_channel = await self.cog.create_match_ticket(interaction.user.id, self.user_id)
                await safe_respond(interaction, content=f"💕 **IT'S A MATCH!** Ticket: {match_channel.mention}", ephemeral=True)
            except Exception:
                logger.exception("Failed to create match ticket")
                await safe_respond(interaction, content="⚠️ Match detected but failed to create ticket.", ephemeral=True)
        else:
            await safe_respond(interaction, content="❤️ Recorded like!", ephemeral=True)

        # Serve next candidate
        await self.cog.serve_next_candidate(interaction)

    @discord.ui.button(label="Pass", style=discord.ButtonStyle.secondary, custom_id=config.ID_DISCOVERY_PASS)
    async def handle_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.record_pass(interaction.user.id, self.user_id)
            await safe_respond(interaction, content="❌ Passed.", ephemeral=True)
        except Exception:
            logger.exception("Failed to record pass")
            await safe_respond(interaction, content="⚠️ Failed to process pass.", ephemeral=True)
        await self.cog.serve_next_candidate(interaction)

    @discord.ui.button(label="Info", style=discord.ButtonStyle.secondary, custom_id=config.ID_DISCOVERY_INFO)
    async def info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer then call profile display which uses safe_respond
        await interaction.response.defer(ephemeral=True)
        await self.cog.show_user_profile(interaction, self.user_id)

    @discord.ui.button(label="Block", style=discord.ButtonStyle.danger, custom_id=config.ID_DISCOVERY_BLOCK)
    async def handle_block(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.record_block(interaction.user.id, self.user_id)
            await safe_respond(interaction, content="🚫 Candidate blocked permanently.", ephemeral=True)
        except Exception:
            logger.exception("Failed to block candidate")
            await safe_respond(interaction, content="⚠️ Failed to block candidate.", ephemeral=True)
        await self.cog.serve_next_candidate(interaction)


class MatchControlView(discord.ui.View):
    def __init__(self, cog, match_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.match_id = match_id

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.secondary, custom_id=config.ID_MATCH_CLOSE)
    async def close_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.close_match_ticket(self.match_id)
            await safe_respond(interaction, content="🔒 Match ticket closed and cleaned up.", ephemeral=True)
        except Exception:
            logger.exception("Failed to close match ticket")
            await safe_respond(interaction, content="⚠️ Failed to close ticket.", ephemeral=True)


# Placeholder Cog methods referenced above should be implemented in the DatingCog class.
class DatingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def create_photo_ticket(self, user: discord.User) -> discord.TextChannel:
        # placeholder: create a private channel and return it
        guild = self.bot.get_guild(config.GUILD_ID)
        channel = await guild.create_text_channel(name=f"photo-ticket-{user.id}")
        return channel

    async def record_like(self, user_a: int, user_b: int) -> bool:
        # placeholder DB operation; return True if mutual
        return False

    async def create_match_ticket(self, user_a: int, user_b: int) -> discord.TextChannel:
        guild = self.bot.get_guild(config.GUILD_ID)
        channel = await guild.create_text_channel(name=f"match-{user_a}-{user_b}")
        return channel

    async def serve_next_candidate(self, interaction: discord.Interaction):
        # placeholder: fetch next candidate and send an embed + view using safe_respond
        embed = discord.Embed(title="Next candidate")
        view = DiscoveryCardView(self, user_id=123456)
        await safe_respond(interaction, embed=embed, view=view, ephemeral=True)

    async def record_pass(self, user_a: int, user_b: int):
        return True

    async def record_block(self, user_a: int, user_b: int):
        return True

    async def show_user_profile(self, interaction: discord.Interaction, user_id: int):
        # prepare embed and show using safe_respond
        embed = discord.Embed(title="User profile")
        await safe_respond(interaction, embed=embed, ephemeral=True)

    async def close_match_ticket(self, match_id: int):
        # placeholder close logic
        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(DatingCog(bot))
