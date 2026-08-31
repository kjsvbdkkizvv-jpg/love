import logging

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import DB_PATH
from cogs.setup import DiscoveryEntryView, ProfileManagementView, safe_respond, get_dating_cog
from cogs.dating import button_cooldown

logger = logging.getLogger("LooksMatch.Admin")


class LikedYouView(discord.ui.View):
    """Persistent view for the single button in #liked-you."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🤩 VIEW WHO LIKED YOU", style=discord.ButtonStyle.success, custom_id=config.ID_VIEW_LIKED_YOU)
    @button_cooldown(2.0)
    async def view_liked_you(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        dating_cog = get_dating_cog(interaction.client)
        if not dating_cog:
            await safe_respond(interaction, content="⚠️ Dating system is currently unavailable.", ephemeral=True)
            return
        await dating_cog.serve_next_liked_you_candidate(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in LikedYouView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again.", ephemeral=True)


class MyRatingView(discord.ui.View):
    """Persistent view for the single button in #my-rating."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⭐ VIEW MY RATING", style=discord.ButtonStyle.primary, custom_id=config.ID_MY_RATING_VIEW)
    @button_cooldown(1.5)
    async def view_my_rating(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT rating_count, overall_average, face_average, physique_average, style_average, tier "
                "FROM rating_results WHERE user_id = ?",
                (interaction.user.id,)
            ) as c:
                row = await c.fetchone()

        if not row or not row[0]:
            await safe_respond(
                interaction,
                content="⭐ You don't have any ratings yet. Head to `#get-rated` to start a rating session!",
                ephemeral=True
            )
            return

        rating_count, overall_avg, face_avg, physique_avg, style_avg, tier = row
        embed = discord.Embed(title="⭐ YOUR RATING BREAKDOWN", color=config.PRIMARY_COLOR)
        embed.add_field(name="Overall Average", value=f"{overall_avg}/10" if overall_avg is not None else "N/A", inline=True)
        embed.add_field(name="Tier", value=tier or "Unrated", inline=True)
        embed.add_field(name="Votes Counted", value=str(rating_count), inline=True)
        embed.add_field(name="Face", value=f"{face_avg}/10" if face_avg is not None else "N/A", inline=True)
        embed.add_field(name="Physique", value=f"{physique_avg}/10" if physique_avg is not None else "N/A", inline=True)
        embed.add_field(name="Style", value=f"{style_avg}/10" if style_avg is not None else "N/A", inline=True)

        await safe_respond(interaction, embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in MyRatingView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again.", ephemeral=True)


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.update_stats_task.start()
        # Only register the views THIS cog owns. DiscoveryEntryView and
        # ProfileManagementView are already registered by cogs.setup's
        # cog_load — registering them again here would raise a duplicate
        # custom_id error and break this cog's load.
        self.bot.add_view(LikedYouView())
        self.bot.add_view(MyRatingView())

    def cog_unload(self):
        self.update_stats_task.cancel()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return

        guild = after.guild
        staff_role = guild.get_role(config.ROLE_STAFF)
        if not staff_role:
            return

        staff_tier_ids = set(config.STAFF_ROLES.values())
        has_staff_tier = any(role.id in staff_tier_ids for role in after.roles)
        has_staff_role = staff_role in after.roles

        if has_staff_tier and not has_staff_role:
            try:
                await after.add_roles(staff_role, reason="Auto-assigned base Staff role for holding a staff tier.")
            except discord.HTTPException:
                pass
        elif not has_staff_tier and has_staff_role:
            try:
                await after.remove_roles(staff_role, reason="Auto-removed base Staff role as member holds no staff tier.")
            except discord.HTTPException:
                pass

    @tasks.loop(minutes=15)
    async def update_stats_task(self):
        if not config.CHANNEL_SERVER_STATS:
            return

        stats_ch = self.bot.get_channel(config.CHANNEL_SERVER_STATS)
        if not stats_ch:
            return

        guild = stats_ch.guild

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM users WHERE dating_eligible = 1") as c:
                total_eligible = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM profiles") as c:
                total_profiles = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM matches WHERE status = 'ACTIVE'") as c:
                active_matches = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM rating_results") as c:
                total_ratings = (await c.fetchone())[0]

        embed = discord.Embed(title="📊 SERVER DASHBOARD METRICS", color=config.PRIMARY_COLOR)
        embed.add_field(name="Total Guild Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="Dating Eligible Users", value=str(total_eligible), inline=True)
        embed.add_field(name="Profiles Created", value=str(total_profiles), inline=True)
        embed.add_field(name="Active Matches", value=str(active_matches), inline=True)
        embed.add_field(name="Official Ratings Calculated", value=str(total_ratings), inline=True)

        await stats_ch.purge(limit=5)
        await stats_ch.send(embed=embed)

    @app_commands.command(name="post-panels", description="Post persistent interactive UI panels into configured channels. Administrator only.")
    @app_commands.checks.has_permissions(administrator=True)
    async def post_panels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reports = []

        # 1. Discover Panel (#discover)
        if config.CHANNEL_DISCOVER:
            discover_ch = interaction.guild.get_channel(config.CHANNEL_DISCOVER)
            if discover_ch:
                embed = discord.Embed(
                    title="💐 FIND YOUR MATCH — DISCOVER",
                    description="Discover community members personalized to your age group and preferences.\nClick below to start swiping through profiles!",
                    color=config.PRIMARY_COLOR
                )
                view = DiscoveryEntryView()
                await discover_ch.purge(limit=5)
                await discover_ch.send(embed=embed, view=view)
                reports.append("✅ Discovery Panel posted in `#discover` (`CHANNEL_DISCOVER`).")
            else:
                reports.append("⚠️ `CHANNEL_DISCOVER` channel not found.")

        # 2. My Profile Panel (#my-profile)
        if config.CHANNEL_MY_PROFILE:
            my_profile_ch = interaction.guild.get_channel(config.CHANNEL_MY_PROFILE)
            if my_profile_ch:
                embed = discord.Embed(
                    title="👤 PROFILE CONTROL PANEL",
                    description="Manage your dating profile card, update your photos, or pause dating activity.",
                    color=config.PRIMARY_COLOR
                )
                view = ProfileManagementView()
                await my_profile_ch.purge(limit=5)
                await my_profile_ch.send(embed=embed, view=view)
                reports.append("✅ Profile Panel posted in `#my-profile` (`CHANNEL_MY_PROFILE`).")
            else:
                reports.append("⚠️ `CHANNEL_MY_PROFILE` channel not found.")

        # 3. Liked You Panel (#liked-you)
        if config.CHANNEL_LIKED_YOU:
            liked_ch = interaction.guild.get_channel(config.CHANNEL_LIKED_YOU)
            if liked_ch:
                embed = discord.Embed(
                    title="🤩 WHO LIKED YOUR PROFILE",
                    description="View members who have already liked your profile!\nClick the button below to browse through them and like them back for an instant match.",
                    color=config.PRIMARY_COLOR
                )
                view = LikedYouView()
                await liked_ch.purge(limit=5)
                await liked_ch.send(embed=embed, view=view)
                reports.append("✅ Liked-You Panel posted in `#liked-you` (`CHANNEL_LIKED_YOU`).")

        # 4. My Rating Panel (#my-rating)
        if config.CHANNEL_MY_RATING:
            rating_ch = interaction.guild.get_channel(config.CHANNEL_MY_RATING)
            if rating_ch:
                embed = discord.Embed(
                    title="⭐ YOUR RATING STATUS & OVERVIEW",
                    description="View your official calculated consensus rating score, breakdown metrics, and tier assignment.",
                    color=config.PRIMARY_COLOR
                )
                view = MyRatingView()
                await rating_ch.purge(limit=5)
                await rating_ch.send(embed=embed, view=view)
                reports.append("✅ Rating Overview Panel posted in `#my-rating` (`CHANNEL_MY_RATING`).")

        await interaction.followup.send(embed=discord.Embed(
            title="📌 UI CONTROL PANELS DEPLOYED",
            description="\n".join(reports),
            color=config.PRIMARY_COLOR
        ), ephemeral=True)

    @app_commands.command(name="dating-admin", description="Run administrative actions (e.g. audit matches) for LooksMatch. Administrator only.")
    @app_commands.checks.has_permissions(administrator=True)
    async def dating_admin(self, interaction: discord.Interaction, action: str, target: discord.Member = None):
        if action == "audit":
            invalidated = 0
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT match_id, user_a, user_b FROM matches WHERE status = 'ACTIVE'") as cursor:
                    matches = await cursor.fetchall()

                for m in matches:
                    async with db.execute("SELECT dating_pool FROM users WHERE user_id = ?", (m[1],)) as c:
                        pa = await c.fetchone()
                    async with db.execute("SELECT dating_pool FROM users WHERE user_id = ?", (m[2],)) as c:
                        pb = await c.fetchone()

                    if pa and pb and pa[0] != pb[0]:
                        await db.execute("UPDATE matches SET status = 'INVALID_AGE_PAIR' WHERE match_id = ?", (m[0],))
                        invalidated += 1

                await db.commit()

            await interaction.response.send_message(f"🔒 Audit completed: {invalidated} invalid age pair matches invalidated.", ephemeral=True)

        elif action == "resync-eligibility":
            # One-time repair for a bug where completing a profile
            # incrementally (via Edit Profile rather than the original
            # unbroken creation wizard) could leave dating_eligible stuck
            # at 0 forever even once every required field was filled in.
            await interaction.response.defer(ephemeral=True)
            from cogs.dating import recompute_dating_eligible

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT user_id, dating_eligible FROM users") as cursor:
                    rows = await cursor.fetchall()

            changed = 0
            for user_id, was_eligible in rows:
                await recompute_dating_eligible(user_id)

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT COUNT(*) FROM users WHERE dating_eligible = 1") as c:
                    now_eligible = (await c.fetchone())[0]

            await interaction.followup.send(
                f"🔄 Resynced eligibility for {len(rows)} users. {now_eligible} are now marked dating-eligible.",
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
