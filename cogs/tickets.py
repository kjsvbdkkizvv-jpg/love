"""
cogs/tickets.py — media (photo/video) tickets and match tickets, split out
of dating.py to keep that file from growing unbounded. This cog owns:
  - PhotoConfirmView and the whole media-upload-ticket lifecycle (create,
    monitor/expire, recover after restart, confirm with the triple-layer
    duplicate-submission guard).
  - Match ticket creation and the voice-channel-cleanup background task.

Other cogs reach into this one via get_tickets_cog(bot) (same pattern as
get_dating_cog in setup.py/admin.py) rather than importing it directly,
since persistent state here belongs to whichever TicketsCog instance the
bot actually loaded.
"""
import asyncio
import datetime
import json
import logging

import aiosqlite
import discord
from discord.ext import commands, tasks

import config
from database import DB_PATH
from cogs.dating import (
    safe_respond,
    button_cooldown,
    clean_username,
    recompute_dating_eligible,
    get_missing_dating_requirements,
)

logger = logging.getLogger("LooksMatch.Tickets")

PHOTO_TICKET_TIMEOUT = 600  # 10 minutes in seconds
MAX_MEDIA_ITEMS = 5


def get_tickets_cog(bot):
    return bot.get_cog("TicketsCog")


class PhotoConfirmView(discord.ui.View):
    def __init__(self, cog, ticket_id: int, ticket_owner_id: int, mode: str = "replace", max_items: int = MAX_MEDIA_ITEMS):
        super().__init__(timeout=None)
        self.cog = cog
        self.ticket_id = ticket_id
        self.ticket_owner_id = ticket_owner_id
        self.mode = mode
        self.max_items = max_items

    @discord.ui.button(label="✅ Confirm Media", style=discord.ButtonStyle.green, custom_id="photo_ticket:confirm")
    @button_cooldown(1.5)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ticket_owner_id:
            await safe_respond(interaction, content="Only the ticket owner can confirm media.", ephemeral=True)
            return

        if self.ticket_id in self.cog._confirming_tickets:
            await safe_respond(interaction, content="⏳ Your media is already being processed — please wait.", ephemeral=True)
            return
        self.cog._confirming_tickets.add(self.ticket_id)

        try:
            button.disabled = True
            button.label = "Processing..."
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)

            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "UPDATE photo_tickets SET confirmed = 1 WHERE ticket_id = ? AND confirmed = 0",
                    (self.ticket_id,)
                )
                await db.commit()
                won_guard = cursor.rowcount > 0

            if not won_guard:
                await safe_respond(interaction, content="✅ This ticket has already been confirmed.", ephemeral=True)
                return

            source_channel = interaction.channel
            found = []
            try:
                # Newest-first (the default when no `after`/`oldest_first` is
                # passed) — critical fix: oldest_first=True was fetching the
                # OLDEST 200 messages in the channel. That's fine for a brand
                # new guild ticket channel (nothing existed before it), but a
                # DM with the bot accumulates messages across the whole
                # onboarding conversation — once it passed 200 messages, the
                # oldest-200 window could miss the photos someone had JUST
                # uploaded entirely, since those are among the newest
                # messages, not the oldest. Scanning newest-first and
                # reversing afterward guarantees we always see the most
                # recent uploads, while still preserving upload order.
                async for msg in source_channel.history(limit=200):
                    for att in msg.attachments:
                        if att.content_type and (att.content_type.startswith("image/") or att.content_type.startswith("video/")):
                            found.append(att)
                    if len(found) >= self.max_items:
                        break
                found = list(reversed(found[:self.max_items]))

                if not found:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE photo_tickets SET confirmed = 0 WHERE ticket_id = ?", (self.ticket_id,))
                        await db.commit()
                    button.disabled = False
                    button.label = "✅ Confirm Media"
                    try:
                        await interaction.message.edit(view=self)
                    except Exception:
                        pass
                    await safe_respond(interaction, content="No uploaded photos or videos found. Please upload media directly (not links) and press Confirm again.", ephemeral=True)
                    return

                vault_channel = self.cog.bot.get_channel(config.CHANNEL_PHOTO_VAULT)
                if not vault_channel:
                    logger.error("CHANNEL_PHOTO_VAULT is not configured or accessible")
                    await safe_respond(interaction, content="⚠️ Media storage isn't configured correctly. Please contact an admin.", ephemeral=True)
                    return

                async def _upload_one(att):
                    try:
                        file = await att.to_file()
                        is_video = bool(att.content_type and att.content_type.startswith("video/"))
                        vault_msg = await vault_channel.send(
                            content=f"{'🎥' if is_video else '📸'} Profile media — <@{interaction.user.id}> (ticket #{self.ticket_id})",
                            file=file
                        )
                        return {"id": vault_msg.id, "type": "video" if is_video else "photo"}
                    except Exception:
                        logger.exception("Failed to archive a media item to the vault channel")
                        return None

                upload_results = await asyncio.gather(*[_upload_one(att) for att in found])
                new_media = [r for r in upload_results if r is not None]

                if not new_media:
                    await safe_respond(interaction, content="❌ Failed to save media permanently. Please try again.", ephemeral=True)
                    return

                async with aiosqlite.connect(DB_PATH) as db:
                    existing_media = []
                    if self.mode == "append":
                        async with db.execute("SELECT photos FROM profiles WHERE user_id = ?", (interaction.user.id,)) as c:
                            prow = await c.fetchone()
                        if prow and prow[0]:
                            try:
                                existing_media = json.loads(prow[0])
                            except Exception:
                                existing_media = []

                    final_media = (existing_media + new_media)[:MAX_MEDIA_ITEMS]
                    primary = str(final_media[0]["id"]) if final_media else None

                    await db.execute(
                        "UPDATE profiles SET photos = ?, primary_photo = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (json.dumps(final_media), primary, interaction.user.id)
                    )
                    async with db.execute("SELECT channel_id FROM photo_tickets WHERE ticket_id = ?", (self.ticket_id,)) as c:
                        ticket_row = await c.fetchone()
                    await db.commit()

                await recompute_dating_eligible(interaction.user.id)
                missing = await get_missing_dating_requirements(interaction.user.id)
                note = "✅ Media saved to your profile! 🎉 Profile complete." if not missing else \
                    f"✅ Media saved to your profile. Still missing: {', '.join(missing)} — revisit Edit Profile to finish."
                await safe_respond(interaction, content=f"{note} Closing ticket...", ephemeral=True)

                monitor_task = self.cog._photo_ticket_monitors.pop(self.ticket_id, None)
                if monitor_task:
                    monitor_task.cancel()

                ticket_channel_id = ticket_row[0] if ticket_row else None
                if ticket_channel_id:
                    self.cog._photo_ticket_confirm_msgs.pop(ticket_channel_id, None)
                    ticket_channel = self.cog.bot.get_channel(ticket_channel_id)
                    if ticket_channel:
                        try:
                            await ticket_channel.delete(reason="Media upload confirmed by user")
                        except Exception:
                            pass

            except Exception:
                logger.exception("Error during confirm media")
                await safe_respond(interaction, content="❌ An error occurred while confirming media. Try again.", ephemeral=True)
        finally:
            self.cog._confirming_tickets.discard(self.ticket_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Unhandled error in PhotoConfirmView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again.", ephemeral=True)


class TicketsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._photo_ticket_confirm_msgs = {}
        self._photo_ticket_monitors = {}
        self._confirming_tickets = set()

    async def cog_load(self):
        self.voice_cleanup_task.start()
        async with aiosqlite.connect(DB_PATH) as db:
            for stmt in (
                "ALTER TABLE photo_tickets ADD COLUMN mode TEXT DEFAULT 'replace'",
                "ALTER TABLE photo_tickets ADD COLUMN max_items INTEGER DEFAULT 5",
            ):
                try:
                    await db.execute(stmt)
                    await db.commit()
                except Exception:
                    pass
        asyncio.create_task(self._recover_photo_tickets())

    async def cog_unload(self):
        self.voice_cleanup_task.cancel()
        for t in self._photo_ticket_monitors.values():
            t.cancel()

    @tasks.loop(minutes=1)
    async def voice_cleanup_task(self):
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
                        empty_start = datetime.datetime.fromisoformat(empty_since_str)
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
        await asyncio.sleep(2)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ticket_id, user_id, channel_id, created_at, mode, max_items FROM photo_tickets WHERE confirmed = 0") as cursor:
                rows = await cursor.fetchall()

        now = datetime.datetime.utcnow()
        for ticket_id, user_id, channel_id, created_at, mode, max_items in rows:
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
                        try: await ch.delete(reason="Media ticket expired during downtime")
                        except Exception: pass
                    try:
                        user = await self.bot.fetch_user(user_id)
                        await user.send("❌ Your media upload ticket expired while the bot was offline. Please re-open profile editing to try again.")
                    except Exception:
                        pass
                    continue

                channel = self.bot.get_channel(channel_id)
                if not channel:
                    try:
                        user = await self.bot.fetch_user(user_id)
                        dm = await user.create_dm()
                        if dm.id == channel_id:
                            channel = dm
                    except Exception:
                        channel = None
                if channel:
                    view = PhotoConfirmView(self, ticket_id, user_id, mode=mode or "replace", max_items=max_items or MAX_MEDIA_ITEMS)
                    try:
                        msg = await channel.send("When you are ready, press Confirm Media below.", view=view)
                        self._photo_ticket_confirm_msgs[channel.id] = msg.id
                    except Exception:
                        pass

                task = asyncio.create_task(self._photo_ticket_monitor(ticket_id, channel_id, user_id, remaining))
                self._photo_ticket_monitors[ticket_id] = task
            except Exception:
                logger.exception("Error recovering ticket %s", ticket_id)

    async def create_photo_ticket(self, guild: discord.Guild, user: discord.User, mode: str = "replace", max_items: int = MAX_MEDIA_ITEMS):
        category = guild.get_channel(config.CATEGORY_PHOTO_TICKETS) if config.CATEGORY_PHOTO_TICKETS else guild.get_channel(config.CATEGORY_MATCHES)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }
        member = guild.get_member(user.id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        name = f"media-ticket-{clean_username(user.name)}"
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
            cursor = await db.execute(
                "INSERT INTO photo_tickets (user_id, channel_id, mode, max_items) VALUES (?, ?, ?, ?)",
                (user.id, channel.id, mode, max_items)
            )
            ticket_id = cursor.lastrowid
            await db.commit()

        embed = discord.Embed(
            title="Upload your profile media",
            description=(
                f"Upload up to {max_items} item(s) in this ticket **as direct attachments** (photos or videos).\n"
                "Pasted links are not supported — please upload the files themselves.\n"
                "When you are happy with your media, press **Confirm Media** below.\n"
                "If you do not confirm within 10 minutes the ticket will be removed and you'll be notified."
            ),
            color=config.PRIMARY_COLOR
        )
        view = PhotoConfirmView(self, ticket_id, user.id, mode=mode, max_items=max_items)
        try:
            confirm_msg = await channel.send(content=user.mention, embed=embed, view=view)
            self._photo_ticket_confirm_msgs[channel.id] = confirm_msg.id
        except Exception:
            logger.exception("Failed to post confirm message in ticket")

        try:
            dm = await user.create_dm()
            await dm.send(f"I created your media upload ticket: {channel.mention}. Upload media there and press Confirm when ready.", view=PhotoConfirmView(self, ticket_id, user.id, mode=mode, max_items=max_items))
        except Exception:
            pass

        task = asyncio.create_task(self._photo_ticket_monitor(ticket_id, channel.id, user.id, PHOTO_TICKET_TIMEOUT))
        self._photo_ticket_monitors[ticket_id] = task
        return channel

    async def create_dm_media_ticket(self, user: discord.User, mode: str = "replace", max_items: int = MAX_MEDIA_ITEMS):
        """DM equivalent of create_photo_ticket: no guild channel is created —
        the user's DM with the bot IS the ticket, since it's already private
        by nature."""
        try:
            dm = await user.create_dm()
        except Exception:
            logger.exception("Failed to open DM for media ticket")
            return None

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO photo_tickets (user_id, channel_id, mode, max_items) VALUES (?, ?, ?, ?)",
                (user.id, dm.id, mode, max_items)
            )
            ticket_id = cursor.lastrowid
            await db.commit()

        embed = discord.Embed(
            title="Upload your profile media",
            description=(
                f"Upload up to {max_items} item(s) right here in this DM **as direct attachments** (photos or videos).\n"
                "Pasted links are not supported — please upload the files themselves.\n"
                "When you are happy with your media, press **Confirm Media** below.\n"
                "If you do not confirm within 10 minutes this request will expire and you'll be notified."
            ),
            color=config.PRIMARY_COLOR
        )
        view = PhotoConfirmView(self, ticket_id, user.id, mode=mode, max_items=max_items)
        try:
            confirm_msg = await dm.send(embed=embed, view=view)
            self._photo_ticket_confirm_msgs[dm.id] = confirm_msg.id
        except Exception:
            logger.exception("Failed to post confirm message in DM")

        task = asyncio.create_task(self._photo_ticket_monitor(ticket_id, dm.id, user.id, PHOTO_TICKET_TIMEOUT))
        self._photo_ticket_monitors[ticket_id] = task
        return dm

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
                    await channel.delete(reason="Media ticket expired (no confirmation)")
                except Exception:
                    pass

            try:
                user = await self.bot.fetch_user(user_id)
                dm = await user.create_dm()
                await dm.send("❌ Your media upload ticket timed out (no confirmation within 10 minutes). You can re-open profile editing to try again.")
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

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT ticket_id, user_id, mode, max_items FROM photo_tickets WHERE channel_id = ?", (ch.id,)) as c:
                    row = await c.fetchone()
            if not row:
                return
            ticket_id, owner_id, mode, max_items = row
            new_msg = await ch.send("When you are ready, press Confirm Media below.", view=PhotoConfirmView(self, ticket_id, owner_id, mode=mode or "replace", max_items=max_items or MAX_MEDIA_ITEMS))
            self._photo_ticket_confirm_msgs[ch.id] = new_msg.id

    async def create_match_ticket(self, guild: discord.Guild, user_a_id: int, user_b_id: int) -> discord.TextChannel:
        category = guild.get_channel(config.CATEGORY_MATCHES) if config.CATEGORY_MATCHES else None

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("INSERT INTO matches (user_a, user_b) VALUES (?, ?)", (user_a_id, user_b_id))
            match_id = cursor.lastrowid
            await db.commit()

        user_a = guild.get_member(user_a_id)
        user_b = guild.get_member(user_b_id)
        name_a = clean_username(user_a.name if user_a else "user1")
        name_b = clean_username(user_b.name if user_b else "user2")
        ticket_name = f"💌・{name_a}-{name_b}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        if user_a: overwrites[user_a] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)
        if user_b: overwrites[user_b] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)

        channel = await guild.create_text_channel(name=ticket_name, category=category, overwrites=overwrites)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE matches SET ticket_channel_id = ? WHERE match_id = ?", (channel.id, match_id))
            await db.commit()

        welcome_embed = discord.Embed(
            title="💕 YOU MATCHED!",
            description=f"Congratulations {user_a.mention if user_a else user_a_id} & {user_b.mention if user_b else user_b_id}!\nYou both liked each other. This is your private match ticket to chat and create a voice room.",
            color=config.PRIMARY_COLOR
        )
        try:
            await channel.send(embed=welcome_embed)
        except Exception:
            pass
        return channel


async def setup(bot):
    await bot.add_cog(TicketsCog(bot))
