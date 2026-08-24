import json
import logging

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import config
from database import DB_PATH

logger = logging.getLogger("LooksMatch.Setup")


def get_dating_cog(bot):
    return bot.get_cog("DatingCog")


async def safe_respond(interaction: discord.Interaction, /, *, content=None, embed=None, view=None, ephemeral=True, **kwargs):
    """Send using response.send_message unless response is already used, then fallback to followup.send."""
    # discord.py's webhook/response senders require the MISSING sentinel (not
    # literal None) when no view is supplied — passing None raises a TypeError.
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


class DiscoveryEntryView(discord.ui.View):
    """Persistent view for the single button in #start-dating."""

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

    @discord.ui.button(label="👤 VIEW PROFILE", style=discord.ButtonStyle.primary, custom_id=config.ID_VIEW_PROFILE)
    async def view_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        dating_cog = get_dating_cog(interaction.client)
        if not dating_cog:
            await safe_respond(interaction, content="⚠️ Dating system is currently unavailable.", ephemeral=True)
            return
        await dating_cog.show_user_profile(interaction, interaction.user.id)

    @staticmethod
    async def _has_profile(user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM profiles WHERE user_id = ? AND bio IS NOT NULL AND bio != ''",
                (user_id,)
            ) as c:
                return (await c.fetchone()) is not None

    @discord.ui.button(label="🆕 CREATE PROFILE", style=discord.ButtonStyle.success, custom_id=config.ID_CREATE_PROFILE)
    async def create_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._has_profile(interaction.user.id):
            await safe_respond(
                interaction,
                content="You already have a profile! Use **✏️ Edit Profile** instead to make changes.",
                ephemeral=True
            )
            return

        from cogs.dating import ProfileEditModal

        dating_cog = get_dating_cog(interaction.client)
        modal = ProfileEditModal(dating_cog)
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

        # Local import avoids a circular import at module load time
        from cogs.dating import ProfileEditModal

        dating_cog = get_dating_cog(interaction.client)

        current_bio, current_region, current_intent, current_interests = "", "North America", "", ""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT bio, dating_intent, interests FROM profiles WHERE user_id = ?",
                (interaction.user.id,)
            ) as c:
                row = await c.fetchone()
            async with db.execute(
                "SELECT location FROM users WHERE user_id = ?",
                (interaction.user.id,)
            ) as c:
                loc_row = await c.fetchone()

        if row:
            current_bio = row[0] or ""
            current_intent = row[1] or ""
            try:
                current_interests = ", ".join(json.loads(row[2])) if row[2] else ""
            except Exception:
                current_interests = ""
        if loc_row and loc_row[0]:
            current_region = loc_row[0]

        modal = ProfileEditModal(
            dating_cog,
            current_bio=current_bio,
            current_region=current_region,
            current_intent=current_intent,
            current_interests=current_interests,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⚙️ PREFERENCES", style=discord.ButtonStyle.secondary, custom_id=config.ID_PREFERENCES)
    async def preferences(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Placeholder until a dedicated preferences flow exists — at least responds now.
        await safe_respond(interaction, content="⚙️ Preferences editing is coming soon!", ephemeral=True)

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
        modal = ProfileEditModal(dating_cog)
        await interaction.response.send_modal(modal)

        # Remove the "create dating profile" onboarding role now that they've engaged the flow
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
        # Re-register persistent views on every startup. Since these match the
        # custom_ids already embedded in existing panel messages, buttons on
        # PREVIOUSLY POSTED panels start working again immediately — no need
        # to repost or re-run /setup.
        self.bot.add_view(DiscoveryEntryView())
        self.bot.add_view(ProfileManagementView())
        self.bot.add_view(OnboardingProfileView())

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return

        guild = after.guild
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}

        # 1. Onboarding Sync: Age, Gender, Preferences
        age_role_found = None
        gender_role_found = None
        interested_role_found = None

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

        dating_pool = "MINOR" if age_role_found in config.UNDERAGE_GROUPS else "ADULT"

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO users (user_id, guild_id, age_group, dating_pool, gender, interested_in, dating_eligible, dating_enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    age_group = COALESCE(excluded.age_group, users.age_group),
                    dating_pool = excluded.dating_pool,
                    gender = COALESCE(excluded.gender, users.gender),
                    interested_in = COALESCE(excluded.interested_in, users.interested_in),
                    updated_at = CURRENT_TIMESTAMP
            """, (after.id, guild.id, age_role_found, dating_pool, gender_role_found, interested_role_found))
            await db.commit()

        # 2. Discord Onboarding Detection: @Create Dating Profile Role
        if config.ROLE_CREATE_DATING_PROFILE in after_ids and config.ROLE_CREATE_DATING_PROFILE not in before_ids:
            await self.trigger_onboarding_profile_setup(after)

    async def trigger_onboarding_profile_setup(self, member: discord.Member):
        embed = discord.Embed(
            title="💕 COMPLETE YOUR DATING PROFILE",
            description=(
                f"Hello {member.mention}!\n\n"
                "You selected **YES** to creating a dating profile during Discord Onboarding.\n"
                "Click the button below to set up your bio, region, dating intentions, interests, and photos.\n\n"
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

    @app_commands.command(name="setup", description="Initialize permanent server channels, roles, database, and persistent UIs")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_server(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        report = []

        bot_member = guild.get_member(self.bot.user.id)
        if not bot_member:
            await interaction.followup.send("❌ Bot member not found in guild.", ephemeral=True)
            return

        report.append(f"🤖 **Bot Role Hierarchy Top:** {bot_member.top_role.mention}")

        channels_created = 0
        categories_created = 0

        for cat_name, channel_list in config.SERVER_STRUCTURE.items():
            category = discord.utils.get(guild.categories, name=cat_name)
            if not category:
                category = await guild.create_category(cat_name)
                categories_created += 1

            for ch_name in channel_list:
                ch = discord.utils.get(category.text_channels + category.voice_channels, name=ch_name)
                if not ch:
                    if cat_name == "🔊 VOICE":
                        await guild.create_voice_channel(name=ch_name, category=category)
                    else:
                        await guild.create_text_channel(name=ch_name, category=category)
                    channels_created += 1

        report.append(f"📁 Categories Verified/Created ({categories_created} new).")
        report.append(f"💬 Channels Verified/Created ({channels_created} new).")

        # Persistent Discovery Interface
        start_dating_ch = discord.utils.get(guild.text_channels, name="start-dating")
        if start_dating_ch:
            embed = discord.Embed(
                title="💕 FIND YOUR MATCH",
                description="Discover people from the community based on your preferences.\nYour discovery feed is personalized to you.",
                color=config.PRIMARY_COLOR
            )
            view = DiscoveryEntryView()
            await start_dating_ch.purge(limit=5)
            await start_dating_ch.send(embed=embed, view=view)
            report.append("✅ Persistent Discovery UI posted in `#start-dating`.")

        # Persistent Profile Interface
        my_profile_ch = discord.utils.get(guild.text_channels, name="my-profile")
        if my_profile_ch:
            embed = discord.Embed(
                title="👤 YOUR PROFILE & PREFERENCES",
                description="Manage your profile, update photos, adjust matching filters, or pause dating.",
                color=config.PRIMARY_COLOR
            )
            view = ProfileManagementView()
            await my_profile_ch.purge(limit=5)
            await my_profile_ch.send(embed=embed, view=view)
            report.append("✅ Persistent Profile Management UI posted in `#my-profile`.")

        report_embed = discord.Embed(
            title="🛠️ LOOKSMATCH COMPLETE SYSTEM SETUP REPORT",
            description="\n".join(report),
            color=config.PRIMARY_COLOR
        )
        await interaction.followup.send(embed=report_embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SetupCog(bot))
