import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import datetime
import config
from database import DB_PATH

class LevelsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_level_for_xp(self, xp_amount: int) -> int:
        level = 1
        for lvl in sorted(config.LEVEL_ROLES.keys()):
            req_xp = int(100 * (lvl ** 1.5))
            if xp_amount >= req_xp:
                level = lvl
        return level

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        user_id = message.author.id
        now = datetime.datetime.utcnow()

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT total_xp, level, last_msg_at FROM xp WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()

            if row:
                last_msg = datetime.datetime.fromisoformat(row[2]) if row[2] else None
                if last_msg and (now - last_msg).total_seconds() < 60:
                    return

                new_xp = row[0] + 15
                new_level = self.get_level_for_xp(new_xp)

                await db.execute("UPDATE xp SET total_xp = ?, level = ?, last_msg_at = ? WHERE user_id = ?", (new_xp, new_level, now.isoformat(), user_id))
                await db.commit()

                if new_level > row[1]:
                    await self.sync_level_role(message.author, new_level)
            else:
                await db.execute("INSERT INTO xp (user_id, total_xp, level, last_msg_at) VALUES (?, 15, 1, ?)", (user_id, now.isoformat()))
                await db.commit()

    async def sync_level_role(self, member: discord.Member, new_level: int):
        guild = member.guild
        all_level_roles = set(config.LEVEL_ROLES.values())
        to_remove = [r for r in member.roles if r.id in all_level_roles]

        if to_remove:
            try:
                await member.remove_roles(*to_remove)
            except discord.HTTPException:
                pass

        target_role_id = config.LEVEL_ROLES.get(new_level)
        if target_role_id:
            role = guild.get_role(target_role_id)
            if role:
                try:
                    await member.add_roles(role)
                except discord.HTTPException:
                    pass

    @app_commands.command(name="level-admin", description="Admin command to modify user XP or level")
    @app_commands.checks.has_permissions(administrator=True)
    async def level_admin(self, interaction: discord.Interaction, target: discord.Member, amount: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO xp (user_id, total_xp) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET total_xp = total_xp + ?", (target.id, amount, amount))
            await db.commit()
        await interaction.response.send_message(f"✅ Added {amount} XP to {target.mention}.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(LevelsCog(bot))
