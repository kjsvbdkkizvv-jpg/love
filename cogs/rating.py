"""
cogs/rating.py — staff profile moderation.

Replaces the old numeric-voting rating system entirely. Every time a
profile is completed or edited (see dating.py's post_profile_for_staff_review,
called from recompute_dating_eligible), the rendered profile card gets
posted into a staff-only review channel with this cog's ProfileReviewView
attached, letting staff:
  - Assign a tier role directly (no averaging, no vote threshold)
  - Ban the profile (e.g. inappropriate media) — clears media/bio, disables
    dating, and locks them out of creating a new profile
  - Correct mis-selected info (age group, gender, region, interested in)
  - Dismiss with no action needed

ProfileReviewView is registered persistently (bot.add_view()) rather than
per-message, so each button looks up which profile it's about via the
profile_reviews table (message_id -> user_id) keyed off the message the
button lives on — same pattern as MatchControlView.
"""
import json
import logging

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import config
from database import DB_PATH

logger = logging.getLogger("LooksMatch.Rating")


def is_staff(member: discord.Member) -> bool:
    if not member:
        return False
    staff_role_id = getattr(config, "ROLE_STAFF", None)
    return bool(staff_role_id and any(r.id == staff_role_id for r in member.roles))


async def safe_respond(interaction: discord.Interaction, /, *, content=None, embed=None, view=None, ephemeral=True, **kwargs):
    send_view = view if view is not None else discord.utils.MISSING
    send_embed = embed if embed is not None else discord.utils.MISSING
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=send_embed, view=send_view, ephemeral=ephemeral, **kwargs)
        else:
            await interaction.followup.send(content=content, embed=send_embed, view=send_view, ephemeral=ephemeral, **kwargs)
    except Exception:
        try:
            await interaction.followup.send(content=content, embed=send_embed, view=send_view, ephemeral=ephemeral, **kwargs)
        except Exception:
            logger.exception("safe_respond failed to send followup")


async def get_review_target(message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM profile_reviews WHERE message_id = ?", (message_id,)) as c:
            row = await c.fetchone()
    return row[0] if row else None


async def staff_apply_role_change(guild: discord.Guild, target_user_id: int, role_dict: dict, chosen_label: str, db_column: str):
    """Same remove-old/add-new role + DB sync as dating.py's apply_role_change,
    but parameterized on an explicit target rather than interaction.user —
    staff are correcting SOMEONE ELSE's profile here."""
    from cogs.dating import mark_staff_edit
    mark_staff_edit(target_user_id)

    member = guild.get_member(target_user_id)
    if member:
        old_role_ids = set(role_dict.values())
        roles_to_remove = [r for r in member.roles if r.id in old_role_ids]
        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason=f"Staff corrected {db_column}")
            except Exception:
                logger.exception("Failed to remove old %s role(s) for %s", db_column, target_user_id)
        new_role_id = role_dict.get(chosen_label)
        new_role = guild.get_role(new_role_id) if new_role_id else None
        if new_role:
            try:
                await member.add_roles(new_role, reason=f"Staff corrected {db_column}")
            except Exception:
                logger.exception("Failed to add new %s role for %s", db_column, target_user_id)

    extra_pool_clause = ", dating_pool = 'ADULT'" if db_column == "age_group" else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {db_column} = ?{extra_pool_clause}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (chosen_label, target_user_id)
        )
        await db.commit()


class TierSelect(discord.ui.Select):
    def __init__(self, target_user_id: int, tier_order: list):
        options = [discord.SelectOption(label=t) for t in tier_order]
        super().__init__(placeholder="Select a tier to assign", min_values=1, max_values=1, options=options)
        self.target_user_id = target_user_id

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await safe_respond(interaction, content="Staff only.", ephemeral=True)
            return

        chosen_tier = self.values[0]
        member = interaction.guild.get_member(self.target_user_id)

        from cogs.dating import mark_staff_edit
        mark_staff_edit(self.target_user_id)

        all_tier_role_ids = set(config.FEMALE_TIER_ROLES.values()) | set(config.MALE_TIER_ROLES.values())
        if member:
            to_remove = [r for r in member.roles if r.id in all_tier_role_ids]
            if to_remove:
                try:
                    await member.remove_roles(*to_remove, reason=f"Tier reassigned by {interaction.user}")
                except Exception:
                    logger.exception("Failed to remove old tier role for %s", self.target_user_id)

            new_role_id = config.FEMALE_TIER_ROLES.get(chosen_tier) or config.MALE_TIER_ROLES.get(chosen_tier)
            new_role = interaction.guild.get_role(new_role_id) if new_role_id else None
            if new_role:
                try:
                    await member.add_roles(new_role, reason=f"Tier assigned by {interaction.user}")
                except Exception:
                    logger.exception("Failed to add new tier role for %s", self.target_user_id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO rating_results (user_id, rating_count, tier, updated_at) VALUES (?, 1, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id) DO UPDATE SET tier = excluded.tier, updated_at = CURRENT_TIMESTAMP",
                (self.target_user_id, chosen_tier)
            )
            await db.commit()

        await safe_respond(interaction, content=f"🏷️ Tier set to **{chosen_tier}** for <@{self.target_user_id}>.", ephemeral=True)


class TierSelectView(discord.ui.View):
    def __init__(self, target_user_id: int, tier_order: list):
        super().__init__(timeout=120)
        self.add_item(TierSelect(target_user_id, tier_order))


class BanReasonModal(discord.ui.Modal, title="Ban this profile"):
    def __init__(self, target_user_id: int):
        super().__init__()
        self.target_user_id = target_user_id
        self.reason = discord.ui.TextInput(
            label="Reason (shown to the user)",
            style=discord.TextStyle.paragraph,
            max_length=300,
            required=True,
            placeholder="e.g. Profile media violated community guidelines"
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reason = self.reason.value.strip()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET profile_banned = 1, profile_banned_reason = ?, dating_enabled = 0, dating_eligible = 0, "
                "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (reason, self.target_user_id)
            )
            # Clear media and bio — this is specifically for cases like
            # inappropriate media, so the content itself shouldn't linger
            # viewable via /profile even though dating is now disabled.
            await db.execute(
                "UPDATE profiles SET photos = '[]', primary_photo = NULL, bio = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ?",
                (self.target_user_id,)
            )
            await db.commit()

        try:
            user = await interaction.client.fetch_user(self.target_user_id)
            dm = await user.create_dm()
            await dm.send(
                f"❌ Your dating profile has been removed by staff.\n**Reason:** {reason}\n\n"
                "If you believe this was a mistake, please contact a staff member."
            )
        except Exception:
            logger.exception("Failed to DM ban notice to %s", self.target_user_id)

        try:
            await interaction.message.edit(
                content=f"🚫 **BANNED** by {interaction.user.mention} — <@{self.target_user_id}>\n**Reason:** {reason}",
                view=None
            )
        except Exception:
            pass

        await safe_respond(interaction, content=f"🚫 Profile banned for <@{self.target_user_id}>.", ephemeral=True)


class EditFieldChooserView(discord.ui.View):
    """Staff-facing 'what would you like to correct?' menu for a profile
    under review — same idea as the user-facing EditChoiceView in
    dating.py, but operating on an arbitrary target rather than the
    clicking user."""

    def __init__(self, target_user_id: int):
        super().__init__(timeout=120)
        self.target_user_id = target_user_id

    @discord.ui.button(label="🌎 Region", style=discord.ButtonStyle.secondary)
    async def edit_region(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_select(interaction, config.REGION_ROLES, "location", "Select the correct region")

    @discord.ui.button(label="⚧ Gender", style=discord.ButtonStyle.secondary)
    async def edit_gender(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_select(interaction, config.GENDER_ROLES, "gender", "Select the correct gender")

    @discord.ui.button(label="🎂 Age", style=discord.ButtonStyle.secondary)
    async def edit_age(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_select(interaction, config.AGE_ROLES, "age_group", "Select the correct age group")

    @discord.ui.button(label="❤️ Interested In", style=discord.ButtonStyle.secondary)
    async def edit_interested_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_select(interaction, config.INTERESTED_IN_ROLES, "interested_in", "Select the correct preference")

    async def _show_select(self, interaction: discord.Interaction, role_dict: dict, db_column: str, placeholder: str):
        if not is_staff(interaction.user):
            await safe_respond(interaction, content="Staff only.", ephemeral=True)
            return

        target_user_id = self.target_user_id

        class _FieldSelect(discord.ui.Select):
            def __init__(self):
                options = [discord.SelectOption(label=k) for k in role_dict.keys()]
                super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

            async def callback(self_select, select_interaction: discord.Interaction):
                if not is_staff(select_interaction.user):
                    await safe_respond(select_interaction, content="Staff only.", ephemeral=True)
                    return
                chosen = self_select.values[0]
                await staff_apply_role_change(select_interaction.guild, target_user_id, role_dict, chosen, db_column)
                await safe_respond(
                    select_interaction,
                    content=f"✅ {db_column.replace('_', ' ').title()} corrected to **{chosen}** for <@{target_user_id}>.",
                    ephemeral=True
                )

        view = discord.ui.View(timeout=120)
        view.add_item(_FieldSelect())
        await safe_respond(interaction, content=f"Editing for <@{target_user_id}>:", view=view, ephemeral=True)


class ProfileReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="review:prev_media")
    async def prev_media(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._navigate_media(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="review:next_media")
    async def next_media(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._navigate_media(interaction, 1)

    async def _navigate_media(self, interaction: discord.Interaction, delta: int):
        if not is_staff(interaction.user):
            await safe_respond(interaction, content="Staff only.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT user_id, media_index FROM profile_reviews WHERE message_id = ?",
                (interaction.message.id,)
            ) as c:
                row = await c.fetchone()
        if not row:
            await safe_respond(interaction, content="⚠️ Couldn't find this review's profile.", ephemeral=True)
            return
        target_user_id, current_index = row

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests,
                       r.tier, r.overall_average, u.interested_in
                FROM users u
                LEFT JOIN profiles p ON u.user_id = p.user_id
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                WHERE u.user_id = ?
            """, (target_user_id,)) as c:
                prow = await c.fetchone()
        if not prow:
            await safe_respond(interaction, content="⚠️ Profile no longer found.", ephemeral=True)
            return

        media_raw = json.loads(prow[4]) if prow[4] else []
        if not media_raw:
            await safe_respond(interaction, content="This profile has no media to flip through.", ephemeral=True)
            return

        new_index = (current_index + delta) % len(media_raw)

        await interaction.response.defer()

        # Local imports: rating.py doesn't own card rendering, dating.py does.
        from cogs.dating import build_card_files, resolve_profile_media, wrap_card_embeds

        media = await resolve_profile_media(interaction.client, media_raw)
        dating_cog = interaction.client.get_cog("DatingCog")
        member = interaction.guild.get_member(target_user_id) if interaction.guild else None
        display_name = member.display_name if member else f"Member {str(target_user_id)[-4:]}"
        is_verified = bool(member and any(r.id == config.ROLE_VERIFIED for r in member.roles))
        tier_text = f"{prow[7]} · {prow[8]}/10" if prow[8] else prow[7]

        files = await build_card_files(
            dating_cog._http_session, media, new_index,
            display_name=display_name, age_group=prow[0], location=prow[2],
            is_verified=is_verified, tier_text=tier_text, gender=prow[1],
            interested_in=prow[9], interests=json.loads(prow[6]) if prow[6] else [],
            dating_intent=prow[5], bio=prow[3],
            executor=dating_cog._render_executor,
        )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE profile_reviews SET media_index = ? WHERE message_id = ?", (new_index, interaction.message.id))
            await db.commit()

        await interaction.edit_original_response(attachments=files, embeds=wrap_card_embeds(files), view=self)

    @discord.ui.button(label="🏷️ Assign Tier", style=discord.ButtonStyle.primary, custom_id="review:assign_tier")
    async def assign_tier(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await safe_respond(interaction, content="Staff only.", ephemeral=True)
            return
        target_user_id = await get_review_target(interaction.message.id)
        if not target_user_id:
            await safe_respond(interaction, content="⚠️ Couldn't find which profile this review is for.", ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT gender FROM users WHERE user_id = ?", (target_user_id,)) as c:
                row = await c.fetchone()
        gender = row[0] if row else None
        tier_order = config.FEMALE_TIER_ORDER if gender == "Woman" else config.MALE_TIER_ORDER

        await safe_respond(
            interaction,
            content=f"Assigning tier for <@{target_user_id}> ({gender or 'gender unknown, using male tier list'}):",
            view=TierSelectView(target_user_id, tier_order),
            ephemeral=True
        )

    @discord.ui.button(label="🚫 Ban Profile", style=discord.ButtonStyle.danger, custom_id="review:ban")
    async def ban_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await safe_respond(interaction, content="Staff only.", ephemeral=True)
            return
        target_user_id = await get_review_target(interaction.message.id)
        if not target_user_id:
            await safe_respond(interaction, content="⚠️ Couldn't find which profile this review is for.", ephemeral=True)
            return
        await interaction.response.send_modal(BanReasonModal(target_user_id))

    @discord.ui.button(label="✏️ Edit Info", style=discord.ButtonStyle.secondary, custom_id="review:edit_info")
    async def edit_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await safe_respond(interaction, content="Staff only.", ephemeral=True)
            return
        target_user_id = await get_review_target(interaction.message.id)
        if not target_user_id:
            await safe_respond(interaction, content="⚠️ Couldn't find which profile this review is for.", ephemeral=True)
            return
        await safe_respond(
            interaction,
            content=f"What would you like to correct for <@{target_user_id}>?",
            view=EditFieldChooserView(target_user_id),
            ephemeral=True
        )

    @discord.ui.button(label="✅ Dismiss", style=discord.ButtonStyle.success, custom_id="review:dismiss")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await safe_respond(interaction, content="Staff only.", ephemeral=True)
            return
        target_user_id = await get_review_target(interaction.message.id)
        try:
            await interaction.message.edit(
                content=f"✅ Reviewed by {interaction.user.mention} — no action needed"
                        + (f" (<@{target_user_id}>)" if target_user_id else ""),
                view=None
            )
        except Exception:
            pass
        await safe_respond(interaction, content="Dismissed.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in ProfileReviewView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong.", ephemeral=True)


class RatingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(ProfileReviewView())


async def setup(bot):
    await bot.add_cog(RatingsCog(bot))
