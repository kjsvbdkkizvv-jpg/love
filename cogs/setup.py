import logging

import aiosqlite
import discord
from discord.ext import commands

import config
from database import DB_PATH

logger = logging.getLogger("LooksMatch.Setup")


def get_dating_cog(bot):
    return bot.get_cog("DatingCog")


async def safe_respond(interaction: discord.Interaction, /, *, content=None, embed=None, view=None, ephemeral=True, **kwargs):
    """Send using response.send_message unless response is already used, then fallback to followup.send."""
    send_view = view if view is not None else discord.utils.MISSING
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, view=send_view, ephemeral=ephemeral, **kwargs)
        else:
            await interaction.followup.send(content=content, embed=embed, view=send_view, ephemeral=ephemeral, **kwargs)
    except Exception:
        try:
            await interaction.followup.send(content=content, embed=embed, view=send_view, ephemeral=ephemeral, **kwargs)
        except Exception:
            logger.exception("safe_respond failed to send followup")


def is_adult_member(member) -> bool:
    """True only if the member holds one of the configured adult age roles.
    config.AGE_ROLES must never contain an underage bracket — this is the
    single gate that keeps the whole dating system adults-only."""
    if not member:
        return False
    adult_role_ids = set(config.AGE_ROLES.values())
    return any(role.id in adult_role_ids for role in member.roles)


class DiscoveryEntryView(discord.ui.View):
    """Persistent view for the single button in #start-dating / #discover."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💕 START DATING", style=discord.ButtonStyle.green, custom_id=config.ID_START_DATING)
    async def start_dating(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        dating_cog = get_dating_cog(interaction.client)
        if not dating_cog:
            await safe_respond(interaction, content="⚠️ Dating system is currently unavailable.", ephemeral=True)
            return
        await dating_cog.serve_next_candidate(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in DiscoveryEntryView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again.", ephemeral=True)


class ProfileManagementView(discord.ui.View):
    """Persistent view for the control panel in #my-profile."""

    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    async def _has_profile(user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM profiles WHERE user_id = ? AND bio IS NOT NULL AND bio != ''",
                (user_id,)
            ) as c:
                return (await c.fetchone()) is not None

    @discord.ui.button(label="👤 VIEW PROFILE", style=discord.ButtonStyle.primary, custom_id=config.ID_VIEW_PROFILE)
    async def view_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer immediately — building the profile card involves several
        # Discord API calls plus image download/compositing, which can
        # exceed the 3s ack window (esp. on a cold first call).
        await interaction.response.defer(ephemeral=True)
        dating_cog = get_dating_cog(interaction.client)
        if not dating_cog:
            await safe_respond(interaction, content="⚠️ Dating system is currently unavailable.", ephemeral=True)
            return
        await dating_cog.show_user_profile(interaction, interaction.user.id)

    @discord.ui.button(label="🆕 CREATE PROFILE", style=discord.ButtonStyle.success, custom_id=config.ID_CREATE_PROFILE)
    async def create_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        # No pre-existing age-role check here: the wizard's own Age step is
        # where a real (adult-only) role gets assigned. Requiring one to
        # already exist would lock everyone out before the roles have ever
        # been assigned to anyone.
        if await self._has_profile(interaction.user.id):
            await safe_respond(
                interaction,
                content="You already have a profile! Use **✏️ Edit Profile** instead to make changes.",
                ephemeral=True
            )
            return

        from cogs.dating import ProfileEditModal

        dating_cog = get_dating_cog(interaction.client)
        modal = ProfileEditModal(dating_cog, is_new_profile=True)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="✏️ EDIT PROFILE", style=discord.ButtonStyle.secondary, custom_id=config.ID_EDIT_PROFILE)
    async def edit_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._has_profile(interaction.user.id):
            await safe_respond(
                interaction,
                content="You don't have a profile yet! Use **🆕 Create Profile** to set one up first.",
                ephemeral=True
            )
            return

        from cogs.dating import EditChoiceView

        dating_cog = get_dating_cog(interaction.client)
        embed = discord.Embed(title="What would you like to edit?", color=config.PRIMARY_COLOR)
        view = EditChoiceView(dating_cog, interaction.user.id)
        await safe_respond(interaction, embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="⏸️ PAUSE DATING", style=discord.ButtonStyle.danger, custom_id=config.ID_PAUSE_DATING)
    async def pause_dating(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT dating_enabled FROM users WHERE user_id = ?", (interaction.user.id,)) as c:
                row = await c.fetchone()
            currently_enabled = bool(row[0]) if row else True
            new_state = 0 if currently_enabled else 1
            await db.execute(
                "UPDATE users SET dating_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (new_state, interaction.user.id)
            )
            await db.commit()

        if new_state:
            await safe_respond(interaction, content="▶️ Dating resumed! You'll appear in discovery again.", ephemeral=True)
        else:
            await safe_respond(interaction, content="⏸️ Dating paused. You won't appear in discovery until you resume.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in ProfileManagementView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again.", ephemeral=True)


class OnboardingProfileView(discord.ui.View):
    """Persistent view for the DM/onboarding 'Create Dating Profile' button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✏️ CREATE DATING PROFILE", style=discord.ButtonStyle.success, custom_id=config.ID_ONBOARDING_SETUP_PROFILE)
    async def create_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        from cogs.dating import ProfileEditModal

        dating_cog = get_dating_cog(interaction.client)
        modal = ProfileEditModal(dating_cog, is_new_profile=True)
        await interaction.response.send_modal(modal)

        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            role = interaction.guild.get_role(config.ROLE_CREATE_DATING_PROFILE)
            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Completed onboarding dating profile setup")
                except Exception:
                    logger.exception("Failed to remove onboarding role")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in OnboardingProfileView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again.", ephemeral=True)


class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register persistent views on every startup so existing panel
        # messages keep working without needing to be reposted.
        self.bot.add_view(DiscoveryEntryView())
        self.bot.add_view(ProfileManagementView())
        self.bot.add_view(OnboardingProfileView())

        # Idempotent schema migration: tracks whether WE paused someone's
        # dating_enabled because they left, so we know it's safe to restore
        # on rejoin (vs. them having paused it themselves beforehand).
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute("ALTER TABLE users ADD COLUMN left_server BOOLEAN DEFAULT 0")
                await db.commit()
            except Exception:
                pass  # column already exists

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return

        guild = after.guild
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}

        age_role_found = None
        gender_role_found = None
        interested_role_found = None
        region_role_found = None

        for role in after.roles:
            for age_label, r_id in config.AGE_ROLES.items():
                if role.id == r_id:
                    age_role_found = age_label
            for gen_label, r_id in config.GENDER_ROLES.items():
                if role.id == r_id:
                    gender_role_found = gen_label
            for pref_label, r_id in config.INTERESTED_IN_ROLES.items():
                if role.id == r_id:
                    interested_role_found = pref_label
            for reg_label, r_id in config.REGION_ROLES.items():
                if role.id == r_id:
                    region_role_found = reg_label

        # config.AGE_ROLES only contains adult brackets — anyone with a
        # recognized age role here is, by definition, adult. dating_pool is
        # always ADULT since underage groups are not supported at all.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO users (user_id, guild_id, age_group, dating_pool, gender, interested_in, location, dating_eligible, dating_enabled)
                VALUES (?, ?, ?, 'ADULT', ?, ?, ?, 1, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    age_group = COALESCE(excluded.age_group, users.age_group),
                    dating_pool = 'ADULT',
                    gender = COALESCE(excluded.gender, users.gender),
                    interested_in = COALESCE(excluded.interested_in, users.interested_in),
                    location = COALESCE(excluded.location, users.location),
                    updated_at = CURRENT_TIMESTAMP
            """, (after.id, guild.id, age_role_found, gender_role_found, interested_role_found, region_role_found))
            await db.commit()

        from cogs.dating import recompute_dating_eligible
        await recompute_dating_eligible(after.id)

        if config.ROLE_CREATE_DATING_PROFILE in after_ids and config.ROLE_CREATE_DATING_PROFILE not in before_ids:
            await self.trigger_onboarding_profile_setup(after)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """When someone leaves (or is kicked/banned), pull them out of the
        discovery queue by reusing the existing dating_enabled pause flag —
        but only if WE'RE the ones pausing it, so we know it's safe to
        restore automatically if they rejoin later."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT dating_enabled FROM users WHERE user_id = ?", (member.id,)) as c:
                row = await c.fetchone()
            if row and row[0]:
                await db.execute(
                    "UPDATE users SET dating_enabled = 0, left_server = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (member.id,)
                )
                await db.commit()

        # Abandon any unconfirmed media ticket — the ticket channel is now
        # inaccessible to them anyway, so leaving it open just wastes space.
        dating_cog = self.bot.get_cog("DatingCog")
        if dating_cog:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT ticket_id, channel_id FROM photo_tickets WHERE user_id = ? AND confirmed = 0",
                    (member.id,)
                ) as c:
                    pending_tickets = await c.fetchall()
            for ticket_id, channel_id in pending_tickets:
                task = dating_cog._photo_ticket_monitors.pop(ticket_id, None)
                if task:
                    task.cancel()
                dating_cog._photo_ticket_confirm_msgs.pop(channel_id, None)
                ch = self.bot.get_channel(channel_id)
                if ch and not isinstance(ch, discord.DMChannel):
                    try:
                        await ch.delete(reason="User left the server — abandoning media ticket")
                    except Exception:
                        pass

        # End any active match so the remaining partner isn't left stranded
        # with someone who's gone. The ticket channel is left in place (with
        # a notice) rather than deleted, so chat history isn't destroyed;
        # the ephemeral voice room is safe to remove immediately.
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT match_id, user_a, user_b, ticket_channel_id, voice_channel_id FROM matches "
                "WHERE status = 'ACTIVE' AND (user_a = ? OR user_b = ?)",
                (member.id, member.id)
            ) as c:
                active_matches = await c.fetchall()

            for match_id, *_ in active_matches:
                await db.execute(
                    "UPDATE matches SET status = 'ENDED_MEMBER_LEFT', closed_at = CURRENT_TIMESTAMP WHERE match_id = ?",
                    (match_id,)
                )
            await db.commit()

        for match_id, user_a, user_b, ticket_channel_id, voice_channel_id in active_matches:
            other_id = user_b if user_a == member.id else user_a
            if voice_channel_id:
                vc = self.bot.get_channel(voice_channel_id)
                if vc:
                    try:
                        await vc.delete(reason="Match partner left the server")
                    except Exception:
                        pass
            if ticket_channel_id:
                ch = self.bot.get_channel(ticket_channel_id)
                if ch:
                    try:
                        await ch.send(f"💔 <@{other_id}>, your match partner has left the server. This ticket is now closed.")
                    except Exception:
                        pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """If we're the ones who paused their dating on the way out, restore
        it automatically on rejoin. If they had paused it themselves before
        leaving, respect that and leave it paused."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT left_server FROM users WHERE user_id = ?", (member.id,)) as c:
                row = await c.fetchone()
            if row and row[0]:
                await db.execute(
                    "UPDATE users SET dating_enabled = 1, left_server = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (member.id,)
                )
                await db.commit()

    async def trigger_onboarding_profile_setup(self, member: discord.Member):
        # No age-role check here either — see create_profile for why. The
        # wizard's Age step is what actually assigns a real adult role.
        embed = discord.Embed(
            title="💕 COMPLETE YOUR DATING PROFILE",
            description=(
                f"Hello {member.mention}!\n\n"
                "You selected **YES** to creating a dating profile during Discord Onboarding.\n"
                "Click the button below to set up your bio, dating intentions, interests, and more.\n\n"
                "*(Once submitted, your `@Create Dating Profile` role will be removed automatically!)*"
            ),
            color=config.PRIMARY_COLOR
        )
        view = OnboardingProfileView()

        try:
            await member.send(embed=embed, view=view)
        except discord.Forbidden:
            my_profile_ch = discord.utils.get(member.guild.text_channels, name="my-profile")
            if my_profile_ch:
                await my_profile_ch.send(content=member.mention, embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(SetupCog(bot))
