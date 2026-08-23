import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import statistics
import config
from database import DB_PATH

class VerifiedRaterVoteModal(discord.ui.Modal, title="Verified Rater Evaluation"):
    overall = discord.ui.TextInput(label="Overall Score (1.0 - 10.0)", placeholder="8.5", required=True, min_length=1, max_length=4)
    face = discord.ui.TextInput(label="Face Score (Optional)", placeholder="8.0", required=False)
    physique = discord.ui.TextInput(label="Physique Score (Optional)", placeholder="8.5", required=False)
    style = discord.ui.TextInput(label="Style Score (Optional)", placeholder="9.0", required=False)

    def __init__(self, session_id: int, target_user_id: int, cog):
        super().__init__()
        self.session_id = session_id
        self.target_user_id = target_user_id
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            o_score = float(self.overall.value)
            if not (1.0 <= o_score <= config.SCORE_SCALE_MAX):
                raise ValueError()
            f_score = float(self.face.value) if self.face.value else None
            p_score = float(self.physique.value) if self.physique.value else None
            s_score = float(self.style.value) if self.style.value else None
        except ValueError:
            await interaction.response.send_message("❌ Invalid numerical score provided.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO ratings (session_id, rater_id, target_id, overall_score, face_score, physique_score, style_score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (self.session_id, interaction.user.id, self.target_user_id, o_score, f_score, p_score, s_score))
            await db.commit()

        await interaction.response.send_message("✅ Confidentially recorded official rating vote!", ephemeral=True)
        await self.cog.recalculate_results(interaction.guild, self.target_user_id, self.session_id)

class RatingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if not interaction.data or "custom_id" not in interaction.data:
            return

        cid = interaction.data["custom_id"]

        if cid == config.ID_RATING_START:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT gender FROM users WHERE user_id = ?", (interaction.user.id,)) as c:
                    row = await c.fetchone()
                gender = row[0] if row else "Woman"

                cursor = await db.execute("INSERT INTO rating_sessions (target_user_id, gender) VALUES (?, ?)", (interaction.user.id, gender))
                session_id = cursor.lastrowid
                await db.commit()

            await interaction.response.send_message(f"✅ Rating session #{session_id} created! Verified raters can now evaluate your photos.", ephemeral=True)

        elif cid == config.ID_RATING_RESULTS:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT rating_count, overall_average, face_average, physique_average, style_average, tier FROM rating_results WHERE user_id = ?", (interaction.user.id,)) as cursor:
                    res = await cursor.fetchone()

            if not res:
                await interaction.response.send_message("❌ No official rating results calculated yet.", ephemeral=True)
                return

            embed = discord.Embed(title="📊 Your Official Rating Results", color=config.PRIMARY_COLOR)
            embed.add_field(name="Overall Score", value=f"**{res[1]}/10**" if res[1] else "N/A", inline=True)
            embed.add_field(name="Assigned Tier", value=f"**{res[5]}**" if res[5] else "N/A", inline=True)
            embed.add_field(name="Verified Votes", value=str(res[0]), inline=True)
            embed.add_field(name="Face Avg", value=str(res[2]) if res[2] else "N/A", inline=True)
            embed.add_field(name="Physique Avg", value=str(res[3]) if res[3] else "N/A", inline=True)
            embed.add_field(name="Style Avg", value=str(res[4]) if res[4] else "N/A", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="rate-user", description="Verified Raters command to vote on an active rating session")
    async def rate_user(self, interaction: discord.Interaction, target: discord.Member):
        if not any(r.id in (config.ROLE_VERIFIED_RATER, config.ROLE_LEAD_RATER) for r in interaction.user.roles):
            await interaction.response.send_message("❌ Only Verified Raters can execute ratings.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT session_id FROM rating_sessions WHERE target_user_id = ? AND status = 'ACTIVE' ORDER BY session_id DESC LIMIT 1", (target.id,)) as cursor:
                row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(f"❌ No active rating session found for {target.mention}.", ephemeral=True)
            return

        modal = VerifiedRaterVoteModal(row[0], target.id, self)
        await interaction.response.send_modal(modal)

    async def recalculate_results(self, guild: discord.Guild, target_user_id: int, session_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT overall_score, face_score, physique_score, style_score FROM ratings WHERE session_id = ? AND valid = 1", (session_id,)) as cursor:
                votes = await cursor.fetchall()

            if len(votes) < config.MINIMUM_VERIFIED_VOTES:
                return

            o_scores = [v[0] for v in votes if v[0] is not None]
            f_scores = [v[1] for v in votes if v[1] is not None]
            p_scores = [v[2] for v in votes if v[2] is not None]
            s_scores = [v[3] for v in votes if v[3] is not None]

            o_avg = round(statistics.mean(o_scores), 2) if o_scores else None
            f_avg = round(statistics.mean(f_scores), 2) if f_scores else None
            p_avg = round(statistics.mean(p_scores), 2) if p_scores else None
            s_avg = round(statistics.mean(s_scores), 2) if s_scores else None

            async with db.execute("SELECT gender FROM users WHERE user_id = ?", (target_user_id,)) as cursor:
                grow = await cursor.fetchone()
                gender = grow[0] if grow else "Woman"

            tier_ladder = config.FEMALE_TIER_ORDER if gender == "Woman" else config.MALE_TIER_ORDER
            tier_idx = min(int((o_avg / 10.0) * len(tier_ladder)), len(tier_ladder) - 1)
            final_tier = tier_ladder[tier_idx]

            await db.execute("""
                INSERT OR REPLACE INTO rating_results (user_id, rating_count, overall_average, face_average, physique_average, style_average, tier)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (target_user_id, len(votes), o_avg, f_avg, p_avg, s_avg, final_tier))
            await db.commit()

        member = guild.get_member(target_user_id) if guild else None
        if member:
            await self.sync_tier_role(member, gender, final_tier)

    async def sync_tier_role(self, member: discord.Member, gender: str, new_tier: str):
        role_map = config.FEMALE_TIER_ROLES if gender == "Woman" else config.MALE_TIER_ROLES
        all_tier_role_ids = set(config.FEMALE_TIER_ROLES.values()).union(set(config.MALE_TIER_ROLES.values()))

        to_remove = [r for r in member.roles if r.id in all_tier_role_ids]
        if to_remove:
            try:
                await member.remove_roles(*to_remove)
            except discord.HTTPException:
                pass

        target_role_id = role_map.get(new_tier)
        if target_role_id:
            role = member.guild.get_role(target_role_id)
            if role:
                try:
                    await member.add_roles(role)
                except discord.HTTPException:
                    pass

    @app_commands.command(name="set-tier", description="Lead Raters command to override a member's tier role")
    @app_commands.checks.has_role(config.ROLE_LEAD_RATER)
    async def set_tier(self, interaction: discord.Interaction, target: discord.Member, tier_name: str):
        gender = "Woman" if any(r.id == config.BASE_FEMALE_ROLE_ID for r in target.roles) else "Man"
        await self.sync_tier_role(target, gender, tier_name)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO rating_results (user_id, rating_count, tier) VALUES (?, 1, ?)", (target.id, tier_name))
            await db.commit()

        await interaction.response.send_message(f"👑 Lead Rater direct tier override applied: **{tier_name}** to {target.mention}.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(RatingsCog(bot))
