import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import random
import json
import config
from database import DB_PATH

async def validate_dating_contact(user_a_id: int, user_b_id: int) -> bool:
    """Strict safety verification isolating MINOR and ADULT pools and blocks."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT dating_eligible, dating_enabled, dating_pool FROM users WHERE user_id = ?", (user_a_id,)) as c:
            a_row = await c.fetchone()
        async with db.execute("SELECT dating_eligible, dating_enabled, dating_pool FROM users WHERE user_id = ?", (user_b_id,)) as c:
            b_row = await c.fetchone()

        if not a_row or not b_row:
            return False

        if not (a_row[0] and a_row[1] and b_row[0] and b_row[1]):
            return False

        async with db.execute("SELECT 1 FROM blocks WHERE (user_id = ? AND blocked_user_id = ?) OR (user_id = ? AND blocked_user_id = ?)", (user_a_id, user_b_id, user_b_id, user_a_id)) as c:
            if await c.fetchone():
                return False

        if a_row[2] != b_row[2]:  # Enforces MINOR vs ADULT isolation
            return False

    return True

class PreferencesModal(discord.ui.Modal, title="Matching Preferences"):
    min_age = discord.ui.TextInput(label="Minimum Age", placeholder="18", default="18", min_length=2, max_length=2)
    max_age = discord.ui.TextInput(label="Maximum Age", placeholder="99", default="99", min_length=2, max_length=2)
    genders = discord.ui.TextInput(label="Preferred Genders (Comma-separated)", placeholder="Woman, Man", default="Woman, Man", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            min_a = int(self.min_age.value)
            max_a = int(self.max_age.value)
        except ValueError:
            await interaction.response.send_message("Invalid age inputs.", ephemeral=True)
            return

        g_list = [g.strip() for g in self.genders.value.split(",") if g.strip()]

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO preferences (user_id, min_age, max_age, preferred_genders)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    min_age = excluded.min_age,
                    max_age = excluded.max_age,
                    preferred_genders = excluded.preferred_genders,
                    updated_at = CURRENT_TIMESTAMP
            """, (interaction.user.id, min_a, max_a, json.dumps(g_list)))
            await db.commit()

        await interaction.response.send_message("✅ Matching preferences saved!", ephemeral=True)

class ProfileEditModal(discord.ui.Modal, title="Edit Dating Profile"):
    bio = discord.ui.TextInput(label="Bio / Description", style=discord.TextStyle.paragraph, max_length=500, required=True)
    region = discord.ui.TextInput(
        label="Region / Location",
        placeholder="North America, Europe, Asia / Oceania, South America, Other",
        default="North America",
        max_length=50,
        required=True
    )
    dating_intent = discord.ui.TextInput(label="Dating Intention", placeholder="Long-term relationship, casual...", max_length=100, required=True)
    interests = discord.ui.TextInput(label="Interests (Comma-separated)", placeholder="Music, Travel, Fitness", max_length=150, required=False)
    photos = discord.ui.TextInput(label="Direct Photo URLs (1-5 URLs separated by space)", style=discord.TextStyle.paragraph, max_length=1000, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        urls = [u.strip() for u in self.photos.value.replace("\n", " ").split(" ") if u.strip().startswith("http")]
        if not urls or len(urls) > 5:
            await interaction.response.send_message("Please provide 1 to 5 valid HTTP/HTTPS direct image URLs.", ephemeral=True)
            return

        interests_list = [i.strip() for i in self.interests.value.split(",") if i.strip()]
        user_region_input = self.region.value.strip()

        matched_region = "Other"
        for reg_key in config.REGION_ROLES.keys():
            if reg_key.lower() in user_region_input.lower() or user_region_input.lower() in reg_key.lower():
                matched_region = reg_key
                break

        guild_id = interaction.guild_id or (interaction.guild.id if interaction.guild else None)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO profiles (user_id, guild_id, bio, photos, primary_photo, dating_intent, interests)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    bio = excluded.bio,
                    photos = excluded.photos,
                    primary_photo = excluded.primary_photo,
                    dating_intent = excluded.dating_intent,
                    interests = excluded.interests,
                    updated_at = CURRENT_TIMESTAMP
            """, (interaction.user.id, guild_id, self.bio.value.strip(), json.dumps(urls), urls[0], self.dating_intent.value.strip(), json.dumps(interests_list)))

            await db.execute("""
                UPDATE users SET location = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?
            """, (matched_region, interaction.user.id))

            await db.commit()

        # Handle Discord Region Role Assignment and Remove Onboarding Role
        role_removed = False
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None

        if member:
            all_region_role_ids = set(config.REGION_ROLES.values())
            existing_region_roles = [r for r in member.roles if r.id in all_region_role_ids]
            if existing_region_roles:
                try:
                    await member.remove_roles(*existing_region_roles)
                except discord.HTTPException:
                    pass

            target_region_role_id = config.REGION_ROLES.get(matched_region)
            if target_region_role_id:
                target_role = member.guild.get_role(target_region_role_id)
                if target_role:
                    try:
                        await member.add_roles(target_role)
                    except discord.HTTPException:
                        pass

            target_onboarding_role = member.guild.get_role(config.ROLE_CREATE_DATING_PROFILE)
            if target_onboarding_role and target_onboarding_role in member.roles:
                try:
                    await member.remove_roles(target_onboarding_role)
                    role_removed = True
                except discord.HTTPException:
                    pass

        status_msg = f"✅ Dating profile updated successfully!\n📍 **Region set to:** {matched_region}"
        if role_removed:
            status_msg += "\n🎉 `@Create Dating Profile` role removed! You are now fully active in the dating pool."

        await interaction.response.send_message(status_msg, ephemeral=True)

class DiscoveryCardView(discord.ui.View):
    def __init__(self, candidate: dict, photo_index: int, cog):
        super().__init__(timeout=300)
        self.candidate = candidate
        self.photo_index = photo_index
        self.cog = cog

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
            await interaction.response.send_message(
                f"💕 **IT'S A MATCH!** You and <@{target_id}> liked each other!\nPrivate match room created: {ticket_channel.mention}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("❤️ Recorded like!", ephemeral=True)

        await self.cog.serve_next_candidate(interaction)

    @discord.ui.button(label="❌ PASS", style=discord.ButtonStyle.secondary, custom_id=config.ID_DISCOVERY_PASS)
    async def handle_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.record_pass(interaction.user.id, self.candidate["user_id"])
        await interaction.response.send_message("❌ Passed.", ephemeral=True)
        await self.cog.serve_next_candidate(interaction)

    @discord.ui.button(label="◀ PREV", style=discord.ButtonStyle.primary, custom_id="discovery:prev")
    async def prev_photo(self, interaction: discord.Interaction, button: discord.ui.Button):
        photos = self.candidate.get("photos", [])
        if photos:
            self.photo_index = (self.photo_index - 1) % len(photos)
            embed = self.cog.build_discovery_embed(self.candidate, self.photo_index)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="NEXT ▶", style=discord.ButtonStyle.primary, custom_id="discovery:next")
    async def next_photo(self, interaction: discord.Interaction, button: discord.ui.Button):
        photos = self.candidate.get("photos", [])
        if photos:
            self.photo_index = (self.photo_index + 1) % len(photos)
            embed = self.cog.build_discovery_embed(self.candidate, self.photo_index)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🚫 BLOCK", style=discord.ButtonStyle.danger, custom_id=config.ID_DISCOVERY_BLOCK)
    async def handle_block(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO blocks (user_id, blocked_user_id) VALUES (?, ?)", (interaction.user.id, self.candidate["user_id"]))
            await db.commit()
        await interaction.response.send_message("🚫 Candidate blocked permanently.", ephemeral=True)
        await self.cog.serve_next_candidate(interaction)

class DatingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if not interaction.data or "custom_id" not in interaction.data:
            return

        cid = interaction.data["custom_id"]

        if cid in (config.ID_EDIT_PROFILE, config.ID_ONBOARDING_SETUP_PROFILE):
            await interaction.response.send_modal(ProfileEditModal())
        elif cid == config.ID_START_DATING:
            await interaction.response.defer(ephemeral=True)
            await self.serve_next_candidate(interaction)
        elif cid == config.ID_VIEW_PROFILE:
            await self.show_user_profile(interaction, interaction.user.id)
        elif cid == config.ID_PREFERENCES:
            await interaction.response.send_modal(PreferencesModal())
        elif cid == config.ID_PAUSE_DATING:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET dating_enabled = CASE WHEN dating_enabled = 1 THEN 0 ELSE 1 END WHERE user_id = ?", (interaction.user.id,))
                await db.commit()
            await interaction.response.send_message("⏯️ Dating status toggled.", ephemeral=True)

    async def get_weighted_candidate(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT dating_pool, gender, age_group, location FROM users WHERE user_id = ?", (user_id,)) as c:
                user_row = await c.fetchone()

            if not user_row:
                return None

            user_pool = user_row[0]

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
                if cand[10]: weight += config.WEIGHT_ALREADY_LIKED
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
            "photos": json.loads(chosen[5]) if chosen[5] else [],
            "dating_intent": chosen[6],
            "interests": json.loads(chosen[7]) if chosen[7] else [],
            "tier": chosen[8] or "Unrated",
            "average_score": chosen[9],
            "has_liked_user": bool(chosen[10])
        }

    def build_discovery_embed(self, candidate: dict, photo_index: int = 0) -> discord.Embed:
        photos = candidate.get("photos", [])
        photo_url = photos[photo_index] if photos else None
        
        tier_str = f"{candidate['tier']} · {candidate['average_score']}/10" if candidate['average_score'] else candidate['tier']
        interests_str = " · ".join(candidate["interests"]) if candidate["interests"] else "None specified"

        embed = discord.Embed(
            title=f"👤 Member Discovery — {candidate['age_group']}",
            description=f"**Bio:** {candidate['bio']}",
            color=config.PRIMARY_COLOR
        )
        embed.add_field(name="📍 Location", value=candidate["location"] or "Unknown", inline=True)
        embed.add_field(name="🎯 Intent", value=candidate["dating_intent"] or "Not specified", inline=True)
        embed.add_field(name="📊 Rating Tier", value=f"**{tier_str}**", inline=True)
        embed.add_field(name="🎵 Interests", value=interests_str, inline=False)

        if photo_url:
            embed.set_image(url=photo_url)

        if photos:
            embed.set_footer(text=f"Photo {photo_index + 1} of {len(photos)}")

        return embed

    async def record_like(self, liker_id: int, target_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO likes (liker_id, target_id) VALUES (?, ?)", (liker_id, target_id))
            async with db.execute("SELECT 1 FROM likes WHERE liker_id = ? AND target_id = ?", (target_id, liker_id)) as cursor:
                is_mutual = bool(await cursor.fetchone())
            await db.commit()
        return is_mutual

    async def record_pass(self, user_id: int, target_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO passes (user_id, target_id) VALUES (?, ?)", (user_id, target_id))
            await db.commit()

    async def create_match_ticket(self, guild: discord.Guild, user_a_id: int, user_b_id: int) -> discord.TextChannel:
        category = discord.utils.get(guild.categories, name="💞 MATCHES")
        if not category:
            category = await guild.create_category("💞 MATCHES")

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("INSERT INTO matches (user_a, user_b) VALUES (?, ?)", (user_a_id, user_b_id))
            match_id = cursor.lastrowid
            await db.commit()

        user_a = guild.get_member(user_a_id)
        user_b = guild.get_member(user_b_id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        if user_a: overwrites[user_a] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)
        if user_b: overwrites[user_b] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)

        channel = await guild.create_text_channel(name=f"match-{match_id}", category=category, overwrites=overwrites)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE matches SET ticket_channel_id = ? WHERE match_id = ?", (channel.id, match_id))
            await db.commit()

        welcome_embed = discord.Embed(
            title="💕 YOU MATCHED!",
            description=f"Congratulations {user_a.mention if user_a else user_a_id} & {user_b.mention if user_b else user_b_id}!\nYou both liked each other. This is your private space to talk.",
            color=config.PRIMARY_COLOR
        )
        await channel.send(embed=welcome_embed)
        return channel

    async def serve_next_candidate(self, interaction: discord.Interaction):
        candidate = await self.get_weighted_candidate(interaction.user.id)
        if not candidate:
            await interaction.followup.send("🎉 You have viewed all available candidate profiles in your pool for now!", ephemeral=True)
            return

        embed = self.build_discovery_embed(candidate)
        view = DiscoveryCardView(candidate, 0, self)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def show_user_profile(self, interaction: discord.Interaction, target_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests, r.tier, r.overall_average, x.level
                FROM users u
                LEFT JOIN profiles p ON u.user_id = p.user_id
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                LEFT JOIN xp x ON u.user_id = x.user_id
                WHERE u.user_id = ?
            """, (target_id,)) as cursor:
                row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message("❌ Profile not found.", ephemeral=True)
            return

        photos = json.loads(row[4]) if row[4] else []
        tier_str = f"{row[7]} · {row[8]}/10" if row[8] else row[7] or "Unrated"

        embed = discord.Embed(title=f"👤 Member Profile — <@{target_id}>", color=config.PRIMARY_COLOR)
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

async def setup(bot):
    await bot.add_cog(DatingCog(bot))
