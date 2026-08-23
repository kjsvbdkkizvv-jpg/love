import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
import config
from database import DB_PATH

class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.update_stats_task.start()

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
        for guild in self.bot.guilds:
            stats_ch = discord.utils.get(guild.text_channels, name="server-stats")
            if not stats_ch:
                continue

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

    @app_commands.command(name="dating-admin", description="Administrative control panel for LooksMatch")
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

async def setup(bot):
    await bot.add_cog(AdminCog(bot))
