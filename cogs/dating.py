import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
import random
import json
import datetime
import config
from database import DB_PATH

def clean_username(name: str) -> str:
    """Sanitize username for channel names."""
    return "".join(c for c in name.lower() if c.isalnum() or c in ("-", "_"))[:12] or "user"

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

        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        role_removed = False

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
            channel_mention = ticket_channel.mention if ticket_channel else "private match room"
            await interaction.response.send_message(
                f"💕 **IT'S A MATCH!** You and <@{target_id}> liked each other!\nPrivate match room created: {channel_mention}",
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
            embed = self.cog.build_discovery_embed(self.candidate, self.photo_index, guild=interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="NEXT ▶", style=discord.ButtonStyle.primary, custom_id="discovery:next")
    async def next_photo(self, interaction: discord.Interaction, button: discord.ui.Button):
        photos = self.candidate.get("photos", [])
        if photos:
            self.photo_index = (self.photo_index + 1) % len(photos)
            embed = self.cog.build_discovery_embed(self.candidate, self.photo_index, guild=interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🚫 BLOCK", style=discord.ButtonStyle.danger, custom_id=config.ID_DISCOVERY_BLOCK)
    async def handle_block(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO blocks (user_id, blocked_user_id) VALUES (?, ?)", (interaction.user.id, self.candidate["user_id"]))
            await db.commit()
        await interaction.response.send_message("🚫 Candidate blocked permanently.", ephemeral=True)
        await self.cog.serve_next_candidate(interaction)

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
            ephemeral=True
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

        # Permissions: Everyone can VIEW, but ONLY user1 & user2 can CONNECT/SPEAK
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        }
        if user_a: overwrites[user_a] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
        if user_b: overwrites[user_b] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)

        voice_channel = await guild.create_voice_channel(name=vc_name, category=vc_category, overwrites=overwrites)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE matches SET voice_channel_id = ?, voice_empty_since = NULL WHERE match_id = ?", (voice_channel.id, self.match_id))
            await db.commit()

        await interaction.followup.send(f"✅ Match voice channel created: {voice_channel.mention}\n*(Note: Everyone can view this channel, but ONLY you two can join! Automatically deleted after 1 hour of inactivity)*", ephemeral=True)

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
                    try: await vc.delete()
                    except discord.HTTPException: pass

            if t_id:
                tc = interaction.guild.get_channel(t_id)
                if tc:
                    try: await tc.delete()
                    except discord.HTTPException: pass

class DatingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice_cleanup_task.start()

    def cog_unload(self):
        self.voice_cleanup_task.cancel()

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
                    # Voice channel was manually deleted
                    await db.execute("UPDATE matches SET voice_channel_id = NULL, voice_empty_since = NULL WHERE match_id = ?", (match_id,))
                    await db.commit()
                    continue

                if len(channel.members) == 0:
                    if not empty_since_str:
                        # Mark empty start time
                        await db.execute("UPDATE matches SET voice_empty_since = ? WHERE match_id = ?", (now.isoformat(), match_id))
                        await db.commit()
                    else:
                        empty_start = datetime.datetime.fromisoformat(empty_since_str)
                        if (now - empty_start).total_seconds() >= 3600:  # 1 hour empty
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
                    # Voice channel currently has active members
                    if empty_since_str:
                        await db.execute("UPDATE matches SET voice_empty_since = NULL WHERE match_id = ?", (match_id,))
                        await db.commit()

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
        elif cid == config.ID_VIEW_LIKED_YOU:
            await interaction.response.defer(ephemeral=True)
            await self.serve_next_liked_you_candidate(interaction)
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
            async with db.execute("""
                SELECT u.dating_pool, u.gender, u.age_group, u.location, r.tier 
                FROM users u 
                LEFT JOIN rating_results r ON u.user_id = r.user_id 
                WHERE u.user_id = ?
            """, (user_id,)) as c:
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

                # Tier proximity weighting: matches with identical/close tier indices receive significantly higher weights
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
            "photos": json.loads(chosen[5]) if chosen[5] else [],
            "dating_intent": chosen[6],
            "interests": json.loads(chosen[7]) if chosen[7] else [],
            "tier": chosen[8] or "Unrated",
            "average_score": chosen[9],
            "has_liked_user": bool(chosen[10])
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
                    "photos": json.loads(row[5]) if row[5] else [],
                    "dating_intent": row[6],
                    "interests": json.loads(row[7]) if row[7] else [],
                    "tier": row[8] or "Unrated",
                    "average_score": row[9]
                }

        return None

    def build_discovery_embed(self, candidate: dict, photo_index: int = 0, guild: discord.Guild = None) -> discord.Embed:
        photos = candidate.get("photos", [])
        photo_url = photos[photo_index] if photos else None

        # Check for Verified Role
        is_verified = False
        if guild:
            member = guild.get_member(candidate["user_id"])
            if member and any(r.id == config.ROLE_VERIFIED for r in member.roles):
                is_verified = True

        verified_badge = " ☑️ **Verified Member**" if is_verified else ""
        tier_str = f"{candidate['tier']} · {candidate['average_score']}/10" if candidate['average_score'] else candidate['tier']
        interests_str = " · ".join(candidate["interests"]) if candidate["interests"] else "None specified"

        embed = discord.Embed(
            title=f"👤 Member Profile — {candidate['age_group']}{verified_badge}",
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
            description=f"Congratulations {user_a.mention if user_a else user_a_id} & {user_b.mention if user_b else user_b_id}!\nYou both liked each other. This is your private match ticket to chat.\n\nUse the control buttons below to create a private match voice room or close this ticket.",
            color=config.PRIMARY_COLOR
        )
        view = MatchControlView(match_id, self)
        await channel.send(embed=welcome_embed, view=view)
        return channel

    async def serve_next_candidate(self, interaction: discord.Interaction):
        candidate = await self.get_weighted_candidate(interaction.user.id)
        if not candidate:
            await interaction.followup.send("🎉 You have viewed all available candidate profiles in your pool for now!", ephemeral=True)
            return

        embed = self.build_discovery_embed(candidate, guild=interaction.guild)
        view = DiscoveryCardView(candidate, 0, self)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

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
                f"❌ This command can only be executed inside <#{config.CHANNEL_PROFILE_CHECK}>!",
                ephemeral=True
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests, r.tier, r.overall_average
                FROM users u
                LEFT JOIN profiles p ON u.user_id = p.user_id
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                WHERE u.user_id = ?
            """, (interaction.user.id,)) as cursor:
                row = await cursor.fetchone()

        if not row or not row[3]:
            await interaction.response.send_message("❌ You have not created a dating profile yet! Set it up in `#my-profile` first.", ephemeral=True)
            return

        photos = json.loads(row[4]) if row[4] else []
        tier_str = f"{row[7]} · {row[8]}/10" if row[8] else row[7] or "Unrated"

        is_verified = any(r.id == config.ROLE_VERIFIED for r in interaction.user.roles)
        verified_badge = " ☑️ **Verified Member**" if is_verified else ""

        embed = discord.Embed(
            title=f"📝 PUBLIC PROFILE REVIEW — {interaction.user.display_name}{verified_badge}",
            description=f"**Bio:** {row[3]}",
            color=config.PRIMARY_COLOR
        )
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
                f"⏳ Cooldown active: You can post another profile review in **{minutes}m {seconds}s**.",
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(DatingCog(bot))
