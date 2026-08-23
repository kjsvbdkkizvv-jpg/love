import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import config
from database import DB_PATH

class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="✏️ CREATE DATING PROFILE",
            style=discord.ButtonStyle.success,
            custom_id=config.ID_ONBOARDING_SETUP_PROFILE
        ))

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
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="💕 START DATING", style=discord.ButtonStyle.green, custom_id=config.ID_START_DATING))
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
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="👤 VIEW PROFILE", style=discord.ButtonStyle.primary, custom_id=config.ID_VIEW_PROFILE))
            view.add_item(discord.ui.Button(label="✏️ EDIT PROFILE", style=discord.ButtonStyle.secondary, custom_id=config.ID_EDIT_PROFILE))
            view.add_item(discord.ui.Button(label="⚙️ PREFERENCES", style=discord.ButtonStyle.secondary, custom_id=config.ID_PREFERENCES))
            view.add_item(discord.ui.Button(label="⏸️ PAUSE DATING", style=discord.ButtonStyle.danger, custom_id=config.ID_PAUSE_DATING))
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
