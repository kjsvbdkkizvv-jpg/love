# cogs/dating.py
import asyncio
import re
import json
import random
import logging
import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite

import config
from database import DB_PATH

logger = logging.getLogger("LooksMatch.Dating")

PHOTO_TICKET_TIMEOUT = 600  # 10 minutes
URL_RE = re.compile(r"https?://\S+")


def clean_username(name: str) -> str:
    """Sanitize username for channel names."""
    return "".join(c for c in name.lower() if c.isalnum() or c in ("-", "_"))[:12] or "user"


def _safe_json_loads(raw: Optional[str], default=None):
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


async def validate_dating_contact(user_a_id: int, user_b_id: int) -> bool:
    """Strict safety verification isolating MINOR and ADULT pools and blocks."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT dating_eligible, dating_enabled, dating_pool FROM users WHERE user_id = ?",
            (user_a_id,),
        ) as c:
            a_row = await c.fetchone()
        async with db.execute(
            "SELECT dating_eligible, dating_enabled, dating_pool FROM users WHERE user_id = ?",
            (user_b_id,),
        ) as c:
            b_row = await c.fetchone()

        if not a_row or not b_row:
            return False

        if not (a_row[0] and a_row[1] and b_row[0] and b_row[1]):
            return False

        async with db.execute(
            "SELECT 1 FROM blocks WHERE (user_id = ? AND blocked_user_id = ?) OR (user_id = ? AND blocked_user_id = ?)",
            (user_a_id, user_b_id, user_b_id, user_a_id),
        ) as c:
            if await c.fetchone():
                return False

        if a_row[2] != b_row[2]:  # Enforces MINOR vs ADULT isolation
            return False

    return True


class ProfileEditModal(discord.ui.Modal, title="Create / Edit Dating Profile"):
    def __init__(
        self,
        cog,
        current_bio="",
        current_region="North America",
        current_intent="",
        current_interests="",
    ):
        super().__init__()
        self.cog = cog
        self.bio = discord.ui.TextInput(
            label="Bio / Description",
            style=discord.TextStyle.paragraph,
            default=current_bio,
            max_length=500,
            required=True,
        )
        self.region = discord.ui.TextInput(
            label="Region / Location",
            placeholder="North America, Europe, Asia, Oceania, South America, Africa",
            default=current_region or "North America",
            max_length=50,
            required=True,
        )
        self.dating_intent = discord.ui.TextInput(
            label="Dating Intention",
            placeholder="Long-term relationship, casual...",
            default=current_intent,
            max_length=100,
            required=True,
        )
        self.interests = discord.ui.TextInput(
            label="Interests (Comma-separated)",
            placeholder="Music, Travel, Fitness",
            default=current_interests,
            max_length=150,
            required=False,
        )

        self.add_item(self.bio)
        self.add_item(self.region)
        self.add_item(self.dating_intent)
        self.add_item(self.interests)

    async def on_submit(self, interaction: discord.Interaction):
        # immediately tell Discord we will handle this (gives us more time for DB and channel ops)
        await interaction.response.defer(ephemeral=True)

        interests_list = [i.strip() for i in self.interests.value.split(",") if i.strip()]
        user_region_input = self.region.value.strip()

        # (rest of existing code follows — and change any interaction.response.send_message(...) that occurs after the defer
        # to interaction.followup.send(...)
        

        matched_region = "Other"
        for reg_key in config.REGION_ROLES.keys():
            if reg_key.lower() in user_region_input.lower() or user_region_input.lower() in reg_key.lower():
                matched_region = reg_key
                break

        guild_id = interaction.guild_id or (interaction.guild.id if interaction.guild else None)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Ensure base user row exists and update location
                await db.execute(
                    """
                    INSERT INTO users (user_id, guild_id, location, dating_eligible, dating_enabled)
                    VALUES (?, ?, ?, 1, 1)
                    ON CONFLICT(user_id) DO UPDATE SET
                        location = excluded.location,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (interaction.user.id, guild_id, matched_region),
                )

                # Insert or update profile WITHOUT photos yet
                await db.execute(
                    """
                    INSERT INTO profiles (user_id, guild_id, bio, photos, primary_photo, dating_intent, interests)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        bio = excluded.bio,
                        dating_intent = excluded.dating_intent,
                        interests = excluded.interests,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        interaction.user.id,
                        guild_id,
                        self.bio.value.strip(),
                        json.dumps([]),
                        None,
                        self.dating_intent.value.strip(),
                        json.dumps(interests_list),
                    ),
                )
                await db.commit()

            # Acknowledge
            try:
                await interaction.response.send_message(
                    "✅ Profile details saved. Creating a private photo upload ticket...", ephemeral=True
                )
            except Exception:
                pass

            # Create ticket (in guild if available)
            guild = interaction.guild
            if guild:
                await self.cog.create_photo_ticket(guild, interaction.user)
            else:
                # If no guild context, DM user with instructions
                try:
                    dm = await interaction.user.create_dm()
                    await dm.send(
                        "Your profile was saved, but I couldn't create a ticket because this interaction wasn't in a guild. Please re-run the profile editor from a server channel."
                    )
                except Exception:
                    pass

        except Exception:
            logger.exception("Error saving profile from modal")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred while saving your profile.", ephemeral=True)


class PhotoConfirmView(discord.ui.View):
    def __init__(self, cog, ticket_id: int, ticket_owner_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.ticket_id = ticket_id
        self.ticket_owner_id = ticket_owner_id

    @discord.ui.button(label="✅ Confirm Photos", style=discord.ButtonStyle.green, custom_id="photo_ticket:confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ticket_owner_id:
            await interaction.response.send_message("Only the ticket owner can confirm photos.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        urls = []

        try:
            async for msg in channel.history(limit=200, oldest_first=True):
                for att in msg.attachments:
                    if att.content_type and att.content_type.startswith("image/"):
                        urls.append(att.url)
                for token in URL_RE.findall(msg.content or ""):
                    urls.append(token)
                if len(urls) >= 5:
                    break
            urls = urls[:5]

            if not urls:
                await interaction.followup.send(
                    "No image attachments or links found in this ticket. Please upload images and press Confirm again.", ephemeral=True
                )
                return

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE profiles SET photos = ?, primary_photo = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (json.dumps(urls), urls[0], interaction.user.id),
                )
                await db.execute("UPDATE photo_tickets SET confirmed = 1 WHERE ticket_id = ?", (self.ticket_id,))
                await db.commit()

            await interaction.followup.send("✅ Photos saved to your profile. Closing ticket...", ephemeral=True)

            # delete channel to keep guild tidy
            try:
                await channel.delete(reason="Photo upload confirmed by user")
            except Exception:
                pass

        except Exception:
            logger.exception("Error during confirm photos")
            try:
                await interaction.followup.send("❌ An error occurred while confirming photos. Try again.", ephemeral=True)
            except Exception:
                pass


class DiscoveryCardView(discord.ui.View):
    def __init__(self, candidate: dict, photo_index: int, cog):
        super().__init__(timeout=300)
        self.candidate = candidate
        self.photo_index = photo_index
        self.cog = cog

    async def update_message(self, interaction: discord.Interaction = None, message: discord.Message = None):
        embed = self.cog.build_discovery_embed(self.candidate, self.photo_index, guild=interaction.guild if interaction else None)
        try:
            if interaction and interaction.response.is_done():
                # fallback if response already used
                await interaction.followup.edit_message(message.id, embed=embed, view=self)
            elif message:
                await message.edit(embed=embed, view=self)
            elif interaction:
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            pass

    @discord.ui.button(label="◀ PREV", style=discord.ButtonStyle.primary, custom_id="discovery:prev")
    async def prev_photo(self, interaction: discord.Interaction, button: discord.ui.Button):
        photos = self.candidate.get("photos", [])
        if photos:
            self.photo_index = (self.photo_index - 1) % len(photos)
            await self.update_message(interaction, message=interaction.message)

    @discord.ui.button(label="NEXT ▶", style=discord.ButtonStyle.primary, custom_id="discovery:next")
    async def next_photo(self, interaction: discord.Interaction, button: discord.ui.Button):
        photos = self.candidate.get("photos", [])
        if photos:
            self.photo_index = (self.photo_index + 1) % len(photos)
            await self.update_message(interaction, message=interaction.message)

    @discord.ui.button(label="❤️ LIKE", style=discord.ButtonStyle.green, custom_id=config.ID_DISCOVERY_LIKE)
    async def handle_like(self, interaction: discord.Interaction, button: discord.ui.Button):
        liker_id = interaction.user.id
        target_id = self.candidate["user_id"]

        if not await validate_dating_contact(liker_id, target_id):
            await interaction.response.send_message("❌ Cannot process action: Safety boundary restriction.", ephemeral=True)
            return

        is_mutual = await self.cog.record_like(liker_id, target_id)
        if is_mutual:
            ticket_channel = await self.cog.create_match_ticket(interaction.guild, liker_id, target_id)
            channel_mention = ticket_channel.mention if ticket_channel else "private match room"
            await interaction.response.send_message(
                f"💕 **IT'S A MATCH!** You and <@{target_id}> liked each other!\nPrivate match room created: {channel_mention}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("❤️ Recorded like!", ephemeral=True)

        await self.cog.serve_next_candidate(interaction)

    @discord.ui.button(label="❌ PASS", style=discord.ButtonStyle.secondary, custom_id=config.ID_DISCOVERY_PASS)
    async def handle_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.record_pass(interaction.user.id, self.candidate["user_id"])
        await interaction.response.send_message("❌ Passed.", ephemeral=True)
        await self.cog.serve_next_candidate(interaction)

    @discord.ui.button(label="🚫 BLOCK", style=discord.ButtonStyle.danger, custom_id=config.ID_DISCOVERY_BLOCK)
    async def handle_block(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO blocks (user_id, blocked_user_id) VALUES (?, ?)",
                (interaction.user.id, self.candidate["user_id"]),
            )
            await db.commit()
        await interaction.response.send_message("🚫 Candidate blocked permanently.", ephemeral=True)
        await self.cog.serve_next_candidate(interaction)

    @discord.ui.button(label="ℹ️ INFO", style=discord.ButtonStyle.secondary, custom_id=config.ID_VIEW_PROFILE)
    async def info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_user_profile(interaction, self.candidate["user_id"])


class LikedYouCardView(discord.ui.View):
    def __init__(self, candidate: dict, photo_index: int, cog):
        super().__init__(timeout=300)
        self.candidate = candidate
        self.photo_index = photo_index
        self.cog = cog

    @discord.ui.button(label="❤️ LIKE BACK", style=discord.ButtonStyle.green, custom_id="liked_you:like_back")
    async def handle_like_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        liker_id = interaction.user.id
        target_id = self.candidate["user_id"]

        if not await validate_dating_contact(liker_id, target_id):
            await interaction.response.send_message("❌ Cannot process action: Safety boundary restriction.", ephemeral=True)
            return

        await self.cog.record_like(liker_id, target_id)
        ticket_channel = await self.cog.create_match_ticket(interaction.guild, liker_id, target_id)
        channel_mention = ticket_channel.mention if ticket_channel else "private match room"

        await interaction.response.send_message(
            f"🎉 **MATCH CREATED!** You liked <@{target_id}> back!\nPrivate match room created: {channel_mention}",
            ephemeral=True,
        )
        await self.cog.serve_next_liked_you_candidate(interaction)

    @discord.ui.button(label="❌ PASS", style=discord.ButtonStyle.secondary, custom_id="liked_you:pass")
    async def handle_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.record_pass(interaction.user.id, self.candidate["user_id"])
        await interaction.response.send_message("❌ Passed on profile.", ephemeral=True)
        await self.cog.serve_next_liked_you_candidate(interaction)


class MatchControlView(discord.ui.View):
    def __init__(self, match_id: int, cog):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.cog = cog

    @discord.ui.button(label="🎙️ Create Voice Channel", style=discord.ButtonStyle.primary, custom_id=config.ID_MATCH_VOICE)
    async def create_voice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_a, user_b, voice_channel_id FROM matches WHERE match_id = ?", (self.match_id,)) as cursor:
                row = await cursor.fetchone()

        if not row:
            await interaction.followup.send("❌ Match ticket not found in database.", ephemeral=True)
            return

        user_a_id, user_b_id, existing_vc_id = row

        if existing_vc_id:
            existing_vc = guild.get_channel(existing_vc_id)
            if existing_vc:
                await interaction.followup.send(f"🎙️ Voice channel already exists: {existing_vc.mention}", ephemeral=True)
                return

        user_a = guild.get_member(user_a_id)
        user_b = guild.get_member(user_b_id)

        name_a = clean_username(user_a.name if user_a else "user1")
        name_b = clean_username(user_b.name if user_b else "user2")
        vc_name = f"💕・{name_a}-{name_b}"

        vc_category = guild.get_channel(config.CATEGORY_MATCH_VOICE)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
            guild.get_member(self.cog.bot.user.id) if guild else guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
        }
        if user_a:
            overwrites[user_a] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
        if user_b:
            overwrites[user_b] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)

        voice_channel = await guild.create_voice_channel(name=vc_name, category=vc_category, overwrites=overwrites)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE matches SET voice_channel_id = ?, voice_empty_since = NULL WHERE match_id = ?", (voice_channel.id, self.match_id))
            await db.commit()

        await interaction.followup.send(f"✅ Match voice channel created: {voice_channel.mention}", ephemeral=True)

    @discord.ui.button(label="🔒 Close Match Ticket", style=discord.ButtonStyle.danger, custom_id=config.ID_MATCH_CLOSE)
    async def close_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Closing match ticket and cleaning up channels...", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ticket_channel_id, voice_channel_id FROM matches WHERE match_id = ?", (self.match_id,)) as cursor:
                row = await cursor.fetchone()

            await db.execute("UPDATE matches SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP WHERE match_id = ?", (self.match_id,))
            await db.commit()

        if row:
            t_id, v_id = row
            if v_id:
                vc = interaction.guild.get_channel(v_id)
                if vc:
                    try:
                        await vc.delete()
                    except discord.HTTPException:
                        pass

            if t_id:
                tc = interaction.guild.get_channel(t_id)
                if tc:
                    try:
                        await tc.delete()
                    except discord.HTTPException:
                        pass


class DatingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice_cleanup_task.start()
        self._photo_ticket_confirm_msgs = {}  # channel_id -> confirm_message_id
        self._photo_ticket_monitors = {}  # ticket_id -> task

    async def cog_load(self):
        # Safe async initialization hook: schedule recovery & register any lightweight startup tasks
        try:
            asyncio.create_task(self._recover_photo_tickets())
            logger.info("DatingCog: scheduled photo ticket recovery task.")
        except Exception:
            logger.exception("Failed to schedule photo ticket recovery on cog_load")

    def cog_unload(self):
        self.voice_cleanup_task.cancel()
        for t in self._photo_ticket_monitors.values():
            t.cancel()

    @tasks.loop(minutes=1)
    async def voice_cleanup_task(self):
        """Monitors active match voice channels and deletes them if empty for 1 hour."""
        now = datetime.datetime.utcnow()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT match_id, voice_channel_id, voice_empty_since, ticket_channel_id FROM matches WHERE status = 'ACTIVE' AND voice_channel_id IS NOT NULL") as cursor:
                active_vcs = await cursor.fetchall()

            for match_id, vc_id, empty_since_str, ticket_id in active_vcs:
                channel = self.bot.get_channel(vc_id)
                if not channel:
                    await db.execute("UPDATE matches SET voice_channel_id = NULL, voice_empty_since = NULL WHERE match_id = ?", (match_id,))
                    await db.commit()
                    continue

                if len(channel.members) == 0:
                    if not empty_since_str:
                        await db.execute("UPDATE matches SET voice_empty_since = ? WHERE match_id = ?", (now.isoformat(), match_id))
                        await db.commit()
                    else:
                        try:
                            empty_start = datetime.datetime.fromisoformat(empty_since_str)
                        except Exception:
                            empty_start = now
                        if (now - empty_start).total_seconds() >= 3600:
                            try:
                                await channel.delete()
                            except discord.HTTPException:
                                pass

                            await db.execute("UPDATE matches SET voice_channel_id = NULL, voice_empty_since = NULL WHERE match_id = ?", (match_id,))
                            await db.commit()

                            ticket_ch = self.bot.get_channel(ticket_id)
                            if ticket_ch:
                                try:
                                    await ticket_ch.send("🎙️ *Match voice channel was automatically deleted due to 1 hour of inactivity.*")
                                except discord.HTTPException:
                                    pass
                else:
                    if empty_since_str:
                        await db.execute("UPDATE matches SET voice_empty_since = NULL WHERE match_id = ?", (match_id,))
                        await db.commit()

    async def _recover_photo_tickets(self):
        # Called at startup to reschedule monitors for outstanding tickets and ensure confirm messages exist
        await asyncio.sleep(2)  # let bot be ready
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ticket_id, user_id, channel_id, created_at FROM photo_tickets WHERE confirmed = 0") as cursor:
                rows = await cursor.fetchall()

        now = datetime.datetime.utcnow()
        for ticket_id, user_id, channel_id, created_at in rows:
            try:
                created_dt = now
                if isinstance(created_at, str):
                    try:
                        created_dt = datetime.datetime.fromisoformat(created_at)
                    except Exception:
                        try:
                            created_dt = datetime.datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            created_dt = now

                elapsed = (now - created_dt).total_seconds()
                remaining = PHOTO_TICKET_TIMEOUT - int(elapsed)
                if remaining <= 0:
                    ch = self.bot.get_channel(channel_id)
                    if ch:
                        try:
                            await ch.delete(reason="Photo ticket expired during downtime")
                        except Exception:
                            pass
                    try:
                        user = await self.bot.fetch_user(user_id)
                        await user.send("❌ Your photo upload ticket expired while the bot was offline. Please re-open the profile editor to try again.")
                    except Exception:
                        pass
                    continue

                channel = self.bot.get_channel(channel_id)
                if channel:
                    view = PhotoConfirmView(self, ticket_id, user_id)
                    try:
                        msg = await channel.send("When you are ready, press Confirm Photos below.", view=view)
                        self._photo_ticket_confirm_msgs[channel.id] = msg.id
                    except Exception:
                        pass

                task = asyncio.create_task(self._photo_ticket_monitor(ticket_id, channel_id, user_id, remaining))
                self._photo_ticket_monitors[ticket_id] = task
            except Exception:
                logger.exception("Error recovering ticket %s", ticket_id)

    async def create_photo_ticket(self, guild: discord.Guild, user: discord.User):
        category = guild.get_channel(config.CATEGORY_PHOTO_TICKETS) if config.CATEGORY_PHOTO_TICKETS else guild.get_channel(
            config.CATEGORY_MATCHES
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        # ensure bot member overwrite
        bot_member = guild.get_member(self.bot.user.id)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        else:
            try:
                bot_member = await guild.fetch_member(self.bot.user.id)
                overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
            except Exception:
                # best-effort; if we can't fetch, continue with default overwrites
                pass

        member = guild.get_member(user.id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        name = f"photo-ticket-{clean_username(user.name)}-{user.discriminator}"
        try:
            channel = await guild.create_text_channel(name=name, category=category, overwrites=overwrites)
        except Exception:
            logger.exception("Failed to create ticket channel")
            try:
                await user.send("❌ I couldn't create a private ticket channel in the server. Please ensure the bot has Manage Channels permission.")
            except Exception:
                pass
            return None

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("INSERT INTO photo_tickets (user_id, channel_id) VALUES (?, ?)", (user.id, channel.id))
            ticket_id = cursor.lastrowid
            await db.commit()

        embed = discord.Embed(
            title="Upload your profile photos",
            description=(
                "Upload up to 5 images in this ticket as attachments (jpg/png/webp).\n"
                "When you are happy with the images, press **Confirm Photos** below.\n"
                "If you do not confirm within 10 minutes the ticket will be removed and you'll be notified."
            ),
            color=config.PRIMARY_COLOR,
        )
        view = PhotoConfirmView(self, ticket_id, user.id)
        try:
            confirm_msg = await channel.send(embed=embed, view=view)
            self._photo_ticket_confirm_msgs[channel.id] = confirm_msg.id
        except Exception:
            logger.exception("Failed to post confirm message in ticket")

        # DM the user with a link and a confirm button
        try:
            dm = await user.create_dm()
            await dm.send(f"I created your photo upload ticket: {channel.mention}. Upload images there and press Confirm when ready.", view=PhotoConfirmView(self, ticket_id, user.id))
        except Exception:
            pass

        # spawn monitor
        task = asyncio.create_task(self._photo_ticket_monitor(ticket_id, channel.id, user.id, PHOTO_TICKET_TIMEOUT))
        self._photo_ticket_monitors[ticket_id] = task
        return channel

    async def _photo_ticket_monitor(self, ticket_id: int, channel_id: int, user_id: int, timeout: int = PHOTO_TICKET_TIMEOUT):
        try:
            await asyncio.sleep(timeout)
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT confirmed FROM photo_tickets WHERE ticket_id = ?", (ticket_id,)) as c:
                    row = await c.fetchone()
            if row and row[0]:
                return

            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.delete(reason="Photo ticket expired (no confirmation)")
                except Exception:
                    pass

            try:
                user = await self.bot.fetch_user(user_id)
                dm = await user.create_dm()
                await dm.send("❌ Your photo upload ticket timed out (no confirmation within 10 minutes). You can re-open the profile editor to try again.")
            except Exception:
                pass

            self._photo_ticket_confirm_msgs.pop(channel_id, None)
            self._photo_ticket_monitors.pop(ticket_id, None)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in photo ticket monitor")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # keep confirm message at bottom for photo tickets
        if message.author.bot:
            return
        ch = message.channel
        if ch.id in self._photo_ticket_confirm_msgs:
            prev_id = self._photo_ticket_confirm_msgs.get(ch.id)
            try:
                if prev_id:
                    prev = await ch.fetch_message(prev_id)
                    try:
                        await prev.delete()
                    except Exception:
                        pass
            except Exception:
                pass

            # find ticket id and owner
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT ticket_id, user_id FROM photo_tickets WHERE channel_id = ?", (ch.id,)) as c:
                    row = await c.fetchone()
            if not row:
                return
            ticket_id, owner_id = row
            new_msg = await ch.send("When you are ready, press Confirm Photos below.", view=PhotoConfirmView(self, ticket_id, owner_id))
            self._photo_ticket_confirm_msgs[ch.id] = new_msg.id

    # ---------- open_profile_modal: used by Setup view to open modal reliably ----------
    async def open_profile_modal(self, interaction: discord.Interaction):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    """
                    SELECT p.bio, u.location, p.dating_intent, p.interests
                    FROM users u
                    LEFT JOIN profiles p ON u.user_id = p.user_id
                    WHERE u.user_id = ?
                    """,
                    (interaction.user.id,),
                ) as cursor:
                    row = await cursor.fetchone()

            if row and row[0]:
                bio, region, intent, interests_json = row
                interests_str = ", ".join(_safe_json_loads(interests_json, [])) if interests_json else ""
                modal = ProfileEditModal(self, current_bio=bio or "", current_region=region or "North America", current_intent=intent or "", current_interests=interests_str)
            else:
                modal = ProfileEditModal(self)
            await interaction.response.send_modal(modal)
        except Exception:
            logger.exception("Failed to open profile modal")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred while opening the profile editor.", ephemeral=True)

    # ---------- discovery & profile helpers ----------
    async def get_weighted_candidate(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """
                SELECT u.dating_pool, u.gender, u.age_group, u.location, r.tier 
                FROM users u 
                LEFT JOIN rating_results r ON u.user_id = r.user_id 
                WHERE u.user_id = ?
                """,
                (user_id,),
            ) as c:
                user_row = await c.fetchone()

            if not user_row:
                return None

            user_pool = user_row[0]
            user_tier = user_row[4]
            user_tier_idx = None
            if user_tier in config.FEMALE_TIER_ORDER:
                user_tier_idx = config.FEMALE_TIER_ORDER.index(user_tier)
            elif user_tier in config.MALE_TIER_ORDER:
                user_tier_idx = config.MALE_TIER_ORDER.index(user_tier)

            query = """
                SELECT u.user_id, u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests,
                       r.tier, r.overall_average,
                       EXISTS(SELECT 1 FROM likes WHERE liker_id = u.user_id AND target_id = ?) as has_liked_user
                FROM users u
                INNER JOIN profiles p ON u.user_id = p.user_id
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                WHERE u.user_id != ?
                  AND u.dating_pool = ?
                  AND u.dating_eligible = 1
                  AND u.dating_enabled = 1
                  AND u.user_id NOT IN (SELECT target_id FROM passes WHERE user_id = ?)
                  AND u.user_id NOT IN (SELECT blocked_user_id FROM blocks WHERE user_id = ?)
                  AND u.user_id NOT IN (
                      SELECT CASE WHEN user_a = ? THEN user_b ELSE user_a END 
                      FROM matches WHERE status = 'ACTIVE'
                  )
            """
            async with db.execute(query, (user_id, user_id, user_pool, user_id, user_id, user_id)) as cursor:
                candidates = await cursor.fetchall()

        if not candidates:
            return None

        valid_candidates = []
        for cand in candidates:
            cand_id = cand[0]
            if await validate_dating_contact(user_id, cand_id):
                weight = 10

                cand_tier = cand[8]
                cand_tier_idx = None
                if cand_tier in config.FEMALE_TIER_ORDER:
                    cand_tier_idx = config.FEMALE_TIER_ORDER.index(cand_tier)
                elif cand_tier in config.MALE_TIER_ORDER:
                    cand_tier_idx = config.MALE_TIER_ORDER.index(cand_tier)

                if user_tier_idx is not None and cand_tier_idx is not None:
                    distance = abs(user_tier_idx - cand_tier_idx)
                    tier_weight = max(1.0, 40.0 - (distance * 3.5))
                    weight += tier_weight

                if cand[10]:
                    weight += config.WEIGHT_ALREADY_LIKED

                valid_candidates.append((cand, weight))

        if not valid_candidates:
            return None

        chosen = random.choices([c[0] for c in valid_candidates], weights=[c[1] for c in valid_candidates], k=1)[0]

        return {
            "user_id": chosen[0],
            "age_group": chosen[1],
            "gender": chosen[2],
            "location": chosen[3],
            "bio": chosen[4],
            "photos": _safe_json_loads(chosen[5], []),
            "dating_intent": chosen[6],
            "interests": _safe_json_loads(chosen[7], []),
            "tier": chosen[8] or "Unrated",
            "average_score": chosen[9],
            "has_liked_user": bool(chosen[10]),
        }

    async def get_next_liked_you_candidate(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            query = """
                SELECT u.user_id, u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests,
                       r.tier, r.overall_average
                FROM likes l
                INNER JOIN users u ON l.liker_id = u.user_id
                INNER JOIN profiles p ON u.user_id = p.user_id
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                WHERE l.target_id = ?
                  AND u.user_id NOT IN (SELECT target_id FROM likes WHERE liker_id = ?)
                  AND u.user_id NOT IN (SELECT target_id FROM passes WHERE user_id = ?)
                  AND u.user_id NOT IN (SELECT blocked_user_id FROM blocks WHERE user_id = ?)
            """
            async with db.execute(query, (user_id, user_id, user_id, user_id)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return None

        for row in rows:
            if await validate_dating_contact(user_id, row[0]):
                return {
                    "user_id": row[0],
                    "age_group": row[1],
                    "gender": row[2],
                    "location": row[3],
                    "bio": row[4],
                    "photos": _safe_json_loads(row[5], []),
                    "dating_intent": row[6],
                    "interests": _safe_json_loads(row[7], []),
                    "tier": row[8] or "Unrated",
                    "average_score": row[9],
                }

        return None

    def build_discovery_embed(self, candidate: dict, photo_index: int = 0, guild: discord.Guild = None) -> discord.Embed:
        photos = candidate.get("photos", [])
        photo_url = photos[photo_index] if photos else None

        is_verified = False
        if guild:
            member = guild.get_member(candidate["user_id"])
            if member and any(r.id == config.ROLE_VERIFIED for r in member.roles):
                is_verified = True

        verified_badge = " ☑️ **Verified Member**" if is_verified else ""
        tier_str = f"{candidate['tier']} · {candidate.get('average_score')}/10" if candidate.get("average_score") else candidate["tier"]
        interests_str = " · ".join(candidate.get("interests") or []) if candidate.get("interests") else "None specified"

        embed = discord.Embed(title=f"👤 Member Profile — {candidate.get('age_group')}{verified_badge}", description=f"**Bio:** {candidate.get('bio')}", color=config.PRIMARY_COLOR)
        embed.add_field(name="📍 Location", value=candidate.get("location") or "Unknown", inline=True)
        embed.add_field(name="🎯 Intent", value=candidate.get("dating_intent") or "Not specified", inline=True)
        embed.add_field(name="📊 Rating Tier", value=f"**{tier_str}**", inline=True)
        embed.add_field(name="🎵 Interests", value=interests_str, inline=False)

        if photo_url:
            embed.set_image(url=photo_url)

        if photos:
            embed.set_footer(text=f"Photo {photo_index + 1} of {len(photos)}")

        return embed

    async def record_like(self, liker_id: int, target_id: int) -> bool:
        # idempotent insertion to avoid duplicates; then check for mutual
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO likes (liker_id, target_id) VALUES (?, ?)", (liker_id, target_id))
            async with db.execute("SELECT 1 FROM likes WHERE liker_id = ? AND target_id = ?", (target_id, liker_id)) as cursor:
                is_mutual = bool(await cursor.fetchone())
            await db.commit()
        return is_mutual

    async def record_pass(self, user_id: int, target_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO passes (user_id, target_id) VALUES (?, ?)", (user_id, target_id))
            await db.commit()

    async def create_match_ticket(self, guild: discord.Guild, user_a_id: int, user_b_id: int) -> Optional[discord.TextChannel]:
        """Create or return an existing match ticket between two users. Uses a transactional check to avoid duplicates."""
        category = guild.get_channel(config.CATEGORY_MATCHES) if config.CATEGORY_MATCHES else None

        match_id = None
        ticket_channel_id = None

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # prevent race by using an IMMEDIATE transaction
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT match_id, ticket_channel_id FROM matches WHERE (user_a = ? AND user_b = ?) OR (user_a = ? AND user_b = ?) LIMIT 1",
                    (user_a_id, user_b_id, user_b_id, user_a_id),
                ) as cur:
                    existing = await cur.fetchone()

                if existing:
                    match_id, ticket_channel_id = existing
                    await db.commit()
                else:
                    cur = await db.execute("INSERT INTO matches (user_a, user_b) VALUES (?, ?)", (user_a_id, user_b_id))
                    match_id = cur.lastrowid
                    await db.commit()
        except Exception:
            logger.exception("Error ensuring match row")

        user_a = None
        user_b = None
        try:
            user_a = guild.get_member(user_a_id)
            if not user_a:
                user_a = await guild.fetch_member(user_a_id)
        except Exception:
            user_a = None
        try:
            user_b = guild.get_member(user_b_id)
            if not user_b:
                user_b = await guild.fetch_member(user_b_id)
        except Exception:
            user_b = None

        # If a ticket channel already exists, return it
        if ticket_channel_id:
            ch = guild.get_channel(ticket_channel_id)
            if ch:
                return ch

        name_a = clean_username(user_a.name if user_a else "user1")
        name_b = clean_username(user_b.name if user_b else "user2")
        ticket_name = f"💌・{name_a}-{name_b}"

        overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False)}
        bot_member = guild.get_member(self.bot.user.id)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True, view_channel=True)
        else:
            try:
                fetched = await guild.fetch_member(self.bot.user.id)
                overwrites[fetched] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True, view_channel=True)
            except Exception:
                pass

        if user_a:
            overwrites[user_a] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)
        if user_b:
            overwrites[user_b] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)

        try:
            channel = await guild.create_text_channel(name=ticket_name, category=category, overwrites=overwrites)
        except Exception:
            logger.exception("Failed to create match ticket channel")
            return None

        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute("UPDATE matches SET ticket_channel_id = ? WHERE match_id = ?", (channel.id, match_id))
                await db.commit()
            except Exception:
                logger.exception("Failed to update match ticket_channel_id in DB")

        welcome_embed = discord.Embed(
            title="💕 YOU MATCHED!",
            description=f"Congratulations {user_a.mention if user_a else user_a_id} & {user_b.mention if user_b else user_b_id}!\nYou both liked each other. This is your private match ticket to chat and create a voice room.",
            color=config.PRIMARY_COLOR,
        )
        view = MatchControlView(match_id, self)
        try:
            await channel.send(embed=welcome_embed, view=view)
        except Exception:
            logger.exception("Failed to send welcome message in match ticket")

        return channel

    async def serve_next_candidate(self, interaction: discord.Interaction):
        candidate = await self.get_weighted_candidate(interaction.user.id)
        if not candidate:
            await interaction.followup.send("🎉 You have viewed all available candidate profiles in your pool for now!", ephemeral=True)
            return

        embed = self.build_discovery_embed(candidate, guild=interaction.guild)
        view = DiscoveryCardView(candidate, 0, self)

        photos = candidate.get("photos", [])
        max_photos = min(5, len(photos))
        # dynamically add numeric jump buttons
        for i in range(max_photos):
            idx = i

            async def jump_callback(interaction: discord.Interaction, button: discord.ui.Button, index=idx, viewref=view):
                viewref.photo_index = index
                await viewref.update_message(interaction, message=interaction.message)

            btn = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.secondary, custom_id=f"discovery:jump:{i+1}")
            btn.callback = jump_callback
            view.add_item(btn)

        def make_indicator(idx, total):
            dots = []
            for n in range(total):
                dots.append("●" if n == idx else "○")
            return " ".join(dots)

        indicator_label = make_indicator(0, max(1, len(photos)))
        indicator_btn = discord.ui.Button(label=indicator_label, style=discord.ButtonStyle.gray, disabled=True, custom_id=f"discovery:indicator:{random.randint(1,100000)}")
        view.add_item(indicator_btn)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def serve_next_liked_you_candidate(self, interaction: discord.Interaction):
        candidate = await self.get_next_liked_you_candidate(interaction.user.id)
        if not candidate:
            await interaction.followup.send("🤩 No new profiles currently waiting in your Liked You feed!", ephemeral=True)
            return

        embed = self.build_discovery_embed(candidate, guild=interaction.guild)
        embed.title = f"🤩 Liked Your Profile — {candidate['age_group']}"
        view = LikedYouCardView(candidate, 0, self)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def show_user_profile(self, interaction: discord.Interaction, target_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """
                SELECT u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests, r.tier, r.overall_average, x.level
                FROM users u
                LEFT JOIN profiles p ON u.user_id = p.user_id
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                LEFT JOIN xp x ON u.user_id = x.user_id
                WHERE u.user_id = ?
                """,
                (target_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row or not row[3]:
            ch_mention = f"<#{config.CHANNEL_MY_PROFILE}>" if config.CHANNEL_MY_PROFILE else "`#my-profile`"
            if target_id == interaction.user.id:
                await interaction.response.send_message(
                    f"❌ You have not created a profile yet! Please go to {ch_mention} and click **✏️ CREATE / EDIT PROFILE**.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("❌ This member has not created a dating profile yet.", ephemeral=True)
            return

        photos = _safe_json_loads(row[4], [])
        tier_str = f"{row[7]} · {row[8]}/10" if row[8] else row[7] or "Unrated"

        member = interaction.guild.get_member(target_id) if interaction.guild else None
        is_verified = member and any(r.id == config.ROLE_VERIFIED for r in member.roles)
        verified_badge = " ☑️ **Verified Member**" if is_verified else ""

        embed = discord.Embed(title=f"👤 Member Profile — <@{target_id}>{verified_badge}", color=config.PRIMARY_COLOR)
        embed.add_field(name="Age Group", value=row[0] or "N/A", inline=True)
        embed.add_field(name="Gender", value=row[1] or "N/A", inline=True)
        embed.add_field(name="Location", value=row[2] or "N/A", inline=True)
        embed.add_field(name="Dating Intent", value=row[5] or "N/A", inline=True)
        embed.add_field(name="Rating Tier", value=tier_str, inline=True)
        embed.add_field(name="Level", value=f"🏆 Level {row[9] or 1}", inline=True)
        embed.add_field(name="Bio", value=row[3] or "No bio provided.", inline=False)

        if photos:
            embed.set_image(url=photos[0])

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="profile", description="View a member's complete dating and rating profile card")
    async def view_profile_cmd(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        await self.show_user_profile(interaction, target.id)

    @app_commands.command(name="profile-check", description="Post your dating profile publicly in #profile-check for community review")
    @app_commands.checks.cooldown(1, 600.0, key=lambda i: i.user.id)
    async def profile_check_cmd(self, interaction: discord.Interaction):
        if config.CHANNEL_PROFILE_CHECK and interaction.channel_id != config.CHANNEL_PROFILE_CHECK:
            await interaction.response.send_message(
                f"❌ This command can only be executed inside <#{config.CHANNEL_PROFILE_CHECK}>!", ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """
                SELECT u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests, r.tier, r.overall_average
                FROM users u
                LEFT JOIN profiles p ON u.user_id = p.user_id
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                WHERE u.user_id = ?
                """,
                (interaction.user.id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row or not row[3]:
            await interaction.response.send_message("❌ You have not created a dating profile yet! Set it up in `#my-profile` first.", ephemeral=True)
            return

        photos = _safe_json_loads(row[4], [])
        tier_str = f"{row[7]} · {row[8]}/10" if row[8] else row[7] or "Unrated"

        is_verified = any(r.id == config.ROLE_VERIFIED for r in interaction.user.roles)
        verified_badge = " ☑️ **Verified Member**" if is_verified else ""

        embed = discord.Embed(title=f"📝 PUBLIC PROFILE REVIEW — {interaction.user.display_name}{verified_badge}", description=f"**Bio:** {row[3]}", color=config.PRIMARY_COLOR)
        embed.add_field(name="Age Group", value=row[0] or "N/A", inline=True)
        embed.add_field(name="Location", value=row[2] or "N/A", inline=True)
        embed.add_field(name="Rating Tier", value=tier_str, inline=True)
        embed.add_field(name="Dating Intent", value=row[5] or "N/A", inline=False)

        if photos:
            embed.set_image(url=photos[0])

        embed.set_footer(text="Community members can leave constructive feedback in thread/chat below!")
        await interaction.response.send_message(embed=embed)

    @profile_check_cmd.error
    async def profile_check_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            minutes = int(error.retry_after // 60)
            seconds = int(error.retry_after % 60)
            await interaction.response.send_message(
                f"⏳ Cooldown active: You can post another profile review in **{minutes}m {seconds}s**.", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(DatingCog(bot))
