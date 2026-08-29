import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
import asyncio
import random
import json
import datetime
import logging
import io
import functools
import concurrent.futures
from typing import List, Optional, Tuple

import aiohttp

import config
from database import DB_PATH
from card_renderer import render_profile_card

logger = logging.getLogger("LooksMatch.Dating")

PHOTO_TICKET_TIMEOUT = 600  # 10 minutes in seconds
MAX_MEDIA_ITEMS = 5
MAX_INTERESTS = 5

# Label translation between the "Interested In" role set and the "Gender" role
# set, since config.py names them differently ("Men"/"Women" vs "Man"/"Woman").
INTEREST_TO_GENDER = {"Men": "Man", "Women": "Woman"}


def clean_username(name: str) -> str:
    """Sanitize username for channel names."""
    return "".join(c for c in name.lower() if c.isalnum() or c in ("-", "_"))[:12] or "user"


def is_adult_member(member: Optional[discord.Member]) -> bool:
    """True only if the member holds one of the configured adult age roles.
    config.AGE_ROLES must never contain an underage bracket — this is the
    single gate that keeps the whole dating system adults-only."""
    if not member:
        return False
    adult_role_ids = set(config.AGE_ROLES.values())
    return any(role.id in adult_role_ids for role in member.roles)


async def resolve_profile_media(bot: commands.Bot, media_items) -> List[dict]:
    """Re-fetch each vault-channel message to obtain a fresh, non-expired
    signed attachment URL. Discord's CDN URLs are cryptographically signed
    and expire (~24h) regardless of whether anything was deleted, so we
    never store a raw URL long-term — only a message reference — and
    resolve it to a live URL each time a profile is actually displayed.
    Returns a list of {"url": str, "is_video": bool}.
    """
    if not media_items:
        return []
    vault_channel = bot.get_channel(config.CHANNEL_PHOTO_VAULT)
    if not vault_channel:
        return []
    resolved = []
    for item in media_items:
        try:
            mid = int(item["id"]) if isinstance(item, dict) else int(item)
            is_video = isinstance(item, dict) and item.get("type") == "video"
            msg = await vault_channel.fetch_message(mid)
            if msg.attachments:
                resolved.append({"url": msg.attachments[0].url, "is_video": is_video})
        except Exception:
            # Message id invalid/legacy entry, or message deleted from vault — skip it.
            continue
    return resolved


MAX_PHOTO_FETCH_BYTES = 20 * 1024 * 1024  # defensive cap; draft() decoding handles normal photos fine regardless


async def fetch_media_bytes(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    """Downloads raw image bytes for card rendering. Returns None on any
    failure — the renderer already draws a graceful placeholder box."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            if resp.content_length and resp.content_length > MAX_PHOTO_FETCH_BYTES:
                return None
            return await resp.read()
    except Exception:
        logger.exception("Failed to fetch media bytes for card rendering")
    return None


MAX_VIDEO_ATTACH_BYTES = 24 * 1024 * 1024  # stay safely under Discord's 25MB default upload limit


async def fetch_video_file(session: aiohttp.ClientSession, url: str) -> Optional[discord.File]:
    """Downloads the actual video so Discord can attach and natively play it
    (a PNG card can never play video — only a real video attachment can).
    Returns None if the file is missing or too large to attach; the card
    itself always shows a 'view original' style link as a fallback."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            content_length = resp.content_length
            if content_length and content_length > MAX_VIDEO_ATTACH_BYTES:
                return None
            data = await resp.read()
            if len(data) > MAX_VIDEO_ATTACH_BYTES:
                return None
            ext = "mp4"
            content_type = resp.headers.get("Content-Type", "")
            if "webm" in content_type:
                ext = "webm"
            elif "quicktime" in content_type or url.lower().endswith(".mov"):
                ext = "mov"
            return discord.File(io.BytesIO(data), filename=f"profile_video.{ext}")
    except Exception:
        logger.exception("Failed to fetch video for attachment")
    return None


async def build_card_files(
    session: aiohttp.ClientSession,
    media: List[dict],
    active_index: int,
    *,
    display_name: str,
    age_group: Optional[str],
    location: Optional[str],
    is_verified: bool,
    tier_text: Optional[str],
    gender: Optional[str],
    interested_in: Optional[str],
    interests: List[str],
    dating_intent: Optional[str],
    bio: Optional[str],
    cache: Optional[dict] = None,
    executor: Optional[concurrent.futures.Executor] = None,
) -> List[discord.File]:
    """Fetches whatever bytes are needed and renders the full image card,
    returning a ready-to-send list of discord.File — the card image, plus
    the actual video file when the active slide is a video (so Discord's
    native player can actually play it, which a static PNG never could).
    `cache` (keyed by media index) lets a single card session — e.g.
    flipping through media on the same discovery card — avoid re-downloading
    images it already has. `executor` should be a small, dedicated
    ThreadPoolExecutor (rather than the default shared one) so concurrent
    renders from multiple users can't all pile up in parallel and spike
    memory at once."""
    thumbnails: List[Tuple[Optional[bytes], bool]] = []
    for i, item in enumerate((media or [])[:5]):
        is_vid = item.get("is_video", False)
        if is_vid:
            # No frame extraction — videos render as a placeholder + play icon
            # in the card itself; the actual playable file is attached separately.
            thumbnails.append((None, True))
            continue
        if cache is not None and i in cache:
            thumbnails.append((cache[i], False))
            continue
        data = await fetch_media_bytes(session, item["url"])
        if cache is not None:
            cache[i] = data
        thumbnails.append((data, False))

    main_bytes = None
    is_video_active = False
    video_file = None
    if media and 0 <= active_index < len(media):
        active_item = media[active_index]
        is_video_active = active_item.get("is_video", False)
        if not is_video_active and active_index < len(thumbnails):
            main_bytes = thumbnails[active_index][0]
        elif is_video_active:
            video_file = await fetch_video_file(session, active_item["url"])

    loop = asyncio.get_event_loop()
    png_bytes = await loop.run_in_executor(
        executor,
        functools.partial(
            render_profile_card,
            main_image_bytes=main_bytes,
            is_video=is_video_active,
            thumbnails=thumbnails,
            active_index=active_index,
            display_name=display_name,
            age_group=age_group,
            location=location,
            is_verified=is_verified,
            tier_text=tier_text,
            gender=gender,
            interested_in=interested_in,
            interests=interests,
            dating_intent=dating_intent,
            bio=bio,
        )
    )
    files = [discord.File(io.BytesIO(png_bytes), filename="profile_card.png")]
    if video_file:
        files.append(video_file)
    return files


async def safe_respond(interaction: discord.Interaction, /, *, content=None, embed=None, view=None, ephemeral=True, **kwargs):
    """Send using response.send_message unless response is already used, then fallback to followup.send.

    Safe to call from command callbacks and button handlers, whether or not
    interaction.response has already been used (e.g. via defer()).
    """
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


async def apply_role_change(interaction: discord.Interaction, role_dict: dict, chosen_label: str, db_column: str):
    """Shared handler for Region/Gender/Age/Interested-In selections:
    removes any existing role from that category and assigns the new one,
    then persists the same value to the matching users.<db_column>.
    This is THE mechanism that keeps profile data, Discord roles, and
    matching criteria synchronized — reused by every structured field."""
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None

    if member:
        old_role_ids = set(role_dict.values())
        roles_to_remove = [r for r in member.roles if r.id in old_role_ids]
        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason=f"Updated {db_column} via profile wizard")
            except Exception:
                logger.exception("Failed to remove old %s role(s)", db_column)
        new_role_id = role_dict.get(chosen_label)
        new_role = interaction.guild.get_role(new_role_id) if new_role_id else None
        if new_role:
            try:
                await member.add_roles(new_role, reason=f"Updated {db_column} via profile wizard")
            except Exception:
                logger.exception("Failed to add new %s role", db_column)

    guild_id = interaction.guild_id or (interaction.guild.id if interaction.guild else None)
    extra_pool_clause = ", dating_pool = 'ADULT'" if db_column == "age_group" else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"""INSERT INTO users (user_id, guild_id, {db_column}, dating_eligible, dating_enabled)
                VALUES (?, ?, ?, 1, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    {db_column} = excluded.{db_column}{extra_pool_clause},
                    updated_at = CURRENT_TIMESTAMP""",
            (interaction.user.id, guild_id, chosen_label)
        )
        await db.commit()


async def get_missing_dating_requirements(user_id: int) -> List[str]:
    """Returns human-readable names of required dating fields the user has
    not yet completed: Age Group, Region, Gender, Interested In."""
    missing = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT age_group, location, gender, interested_in FROM users WHERE user_id = ?",
            (user_id,)
        ) as c:
            row = await c.fetchone()

    if not row:
        return ["Age Group", "Region", "Gender", "Interested In"]

    age_group, location, gender, interested_in = row
    if not age_group:
        missing.append("Age Group")
    if not location:
        missing.append("Region")
    if not gender:
        missing.append("Gender")
    if not interested_in:
        missing.append("Interested In")
    return missing


async def recompute_dating_eligible(user_id: int):
    """A profile only becomes eligible for discovery once Age Group, Region,
    Gender, Interested In, and a bio all exist. Called after every relevant
    edit so dating_eligible (already enforced in the matching SQL) never
    drifts out of sync with the actual profile state."""
    missing = await get_missing_dating_requirements(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM profiles WHERE user_id = ? AND bio IS NOT NULL AND bio != ''",
            (user_id,)
        ) as c:
            has_bio = (await c.fetchone()) is not None
        complete = (not missing) and has_bio
        await db.execute(
            "UPDATE users SET dating_eligible = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (1 if complete else 0, user_id)
        )
        await db.commit()


class ChoiceStepView(discord.ui.View):
    """Generic single-question button step: renders one button per option
    and calls back with whichever label was chosen. Used for every
    structured field (Region/Gender/Age/Interested In) so the wizard and
    the individual-field editors share one implementation."""

    def __init__(self, options: List[str], on_choice, owner_id: int, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.on_choice = on_choice
        self.owner_id = owner_id
        for label in options:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(label)
            self.add_item(btn)

    def _make_callback(self, label: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await safe_respond(interaction, content="This isn't your profile setup session.", ephemeral=True)
                return
            await self.on_choice(interaction, label)
        return callback

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in ChoiceStepView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again from Edit Profile.", ephemeral=True)


# ---------------------------------------------------------------------------
# Wizard step orchestration (chained via interaction.response.edit_message)
# ---------------------------------------------------------------------------

async def show_region_step(interaction: discord.Interaction, cog, next_step):
    embed = discord.Embed(title="🌎 Select your region", description="Where are you located?", color=config.PRIMARY_COLOR)

    async def on_choice(i2: discord.Interaction, label: str):
        await apply_role_change(i2, config.REGION_ROLES, label, "location")
        await next_step(i2, cog)

    view = ChoiceStepView(list(config.REGION_ROLES.keys()), on_choice, interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)


async def show_gender_step(interaction: discord.Interaction, cog, next_step):
    embed = discord.Embed(title="⚧ Select your gender", color=config.PRIMARY_COLOR)

    async def on_choice(i2: discord.Interaction, label: str):
        await apply_role_change(i2, config.GENDER_ROLES, label, "gender")
        await next_step(i2, cog)

    view = ChoiceStepView(list(config.GENDER_ROLES.keys()), on_choice, interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)


async def show_age_step(interaction: discord.Interaction, cog, next_step):
    embed = discord.Embed(
        title="🎂 Select your age group",
        description="Only adult age groups are available.",
        color=config.PRIMARY_COLOR
    )

    async def on_choice(i2: discord.Interaction, label: str):
        # AGE SELECTION TOUCHES DISCORD ROLES — this is the same apply_role_change
        # helper used for every structured field, assigning/removing the real
        # config.AGE_ROLES role. Underage brackets cannot appear here because
        # they are not present in config.AGE_ROLES at all.
        await apply_role_change(i2, config.AGE_ROLES, label, "age_group")
        await next_step(i2, cog)

    view = ChoiceStepView(list(config.AGE_ROLES.keys()), on_choice, interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)


async def show_interested_in_step(interaction: discord.Interaction, cog, next_step):
    embed = discord.Embed(title="❤️ Who are you interested in?", color=config.PRIMARY_COLOR)

    async def on_choice(i2: discord.Interaction, label: str):
        await apply_role_change(i2, config.INTERESTED_IN_ROLES, label, "interested_in")
        await next_step(i2, cog)

    view = ChoiceStepView(list(config.INTERESTED_IN_ROLES.keys()), on_choice, interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)


async def show_media_step(interaction: discord.Interaction, cog):
    """Terminal step for the CREATE wizard: always a fresh/empty profile at
    this point, so it's a simple Add Media / Finish choice."""
    embed = discord.Embed(
        title="📸 Add your profile media",
        description=f"You can add up to {MAX_MEDIA_ITEMS} media items (photos or videos).",
        color=config.PRIMARY_COLOR
    )
    view = discord.ui.View(timeout=300)

    add_btn = discord.ui.Button(label="Add Media", style=discord.ButtonStyle.success)
    finish_btn = discord.ui.Button(label="Finish", style=discord.ButtonStyle.secondary)

    async def on_add(i2: discord.Interaction):
        if i2.user.id != interaction.user.id:
            await safe_respond(i2, content="This isn't your profile setup session.", ephemeral=True)
            return
        await i2.response.edit_message(
            content=f"📸 Upload up to {MAX_MEDIA_ITEMS} photos/videos in your ticket, then press Confirm.",
            embed=None, view=None
        )
        await start_media_ticket(i2, cog, mode="replace", max_items=MAX_MEDIA_ITEMS)

    async def on_finish(i2: discord.Interaction):
        if i2.user.id != interaction.user.id:
            await safe_respond(i2, content="This isn't your profile setup session.", ephemeral=True)
            return
        await recompute_dating_eligible(i2.user.id)
        missing = await get_missing_dating_requirements(i2.user.id)
        note = "🎉 Profile complete!" if not missing else f"✅ Profile saved. Still missing: {', '.join(missing)}."
        await i2.response.edit_message(content=note, embed=None, view=None)

    add_btn.callback = on_add
    finish_btn.callback = on_finish
    view.add_item(add_btn)
    view.add_item(finish_btn)
    await interaction.response.edit_message(embed=embed, view=view)


async def start_media_ticket(interaction: discord.Interaction, cog, mode: str, max_items: int):
    if interaction.guild:
        await cog.create_photo_ticket(interaction.guild, interaction.user, mode=mode, max_items=max_items)
    else:
        # Running from a DM (e.g. the onboarding flow) — post the upload
        # request right here instead of requiring a guild channel.
        await cog.create_dm_media_ticket(interaction.user, mode=mode, max_items=max_items)


async def show_media_edit_choice(interaction: discord.Interaction, cog, current_count: int):
    embed = discord.Embed(title="What would you like to do?", color=config.PRIMARY_COLOR)
    view = discord.ui.View(timeout=300)
    owner_id = interaction.user.id

    if current_count < MAX_MEDIA_ITEMS:
        remaining = MAX_MEDIA_ITEMS - current_count
        add_btn = discord.ui.Button(label="Add Media", style=discord.ButtonStyle.success)

        async def on_add(i2: discord.Interaction):
            if i2.user.id != owner_id:
                await safe_respond(i2, content="This isn't your profile setup session.", ephemeral=True)
                return
            await i2.response.edit_message(
                content=f"You currently have {current_count}/{MAX_MEDIA_ITEMS} media items. Send up to {remaining} more in your ticket, then press Confirm.",
                embed=None, view=None
            )
            await start_media_ticket(i2, cog, mode="append", max_items=remaining)

        add_btn.callback = on_add
        view.add_item(add_btn)

    replace_btn = discord.ui.Button(label="Clear & Replace", style=discord.ButtonStyle.danger)

    async def on_replace(i2: discord.Interaction):
        if i2.user.id != owner_id:
            await safe_respond(i2, content="This isn't your profile setup session.", ephemeral=True)
            return
        await i2.response.edit_message(
            content=f"Your current media will be removed. Send your new media (up to {MAX_MEDIA_ITEMS}) in your ticket, then press Confirm.",
            embed=None, view=None
        )
        await start_media_ticket(i2, cog, mode="replace", max_items=MAX_MEDIA_ITEMS)

    replace_btn.callback = on_replace
    view.add_item(replace_btn)

    await interaction.response.edit_message(embed=embed, view=view)


async def show_media_edit_prompt(interaction: discord.Interaction, cog):
    """Entry point for editing media on an EXISTING profile: never forces
    re-upload, always asks first."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT photos FROM profiles WHERE user_id = ?", (interaction.user.id,)) as c:
            row = await c.fetchone()
    existing_media = json.loads(row[0]) if row and row[0] else []
    count = len(existing_media)

    embed = discord.Embed(title="📸 Would you like to edit your pictures?", color=config.PRIMARY_COLOR)
    view = discord.ui.View(timeout=300)
    owner_id = interaction.user.id

    yes_btn = discord.ui.Button(label="Yes", style=discord.ButtonStyle.primary)
    no_btn = discord.ui.Button(label="No", style=discord.ButtonStyle.secondary)

    async def on_yes(i2: discord.Interaction):
        if i2.user.id != owner_id:
            await safe_respond(i2, content="This isn't your profile setup session.", ephemeral=True)
            return
        await show_media_edit_choice(i2, cog, count)

    async def on_no(i2: discord.Interaction):
        if i2.user.id != owner_id:
            await safe_respond(i2, content="This isn't your profile setup session.", ephemeral=True)
            return
        await recompute_dating_eligible(i2.user.id)
        missing = await get_missing_dating_requirements(i2.user.id)
        note = "✅ Profile updated!" if not missing else f"✅ Profile updated. Still missing: {', '.join(missing)}."
        await i2.response.edit_message(content=note, embed=None, view=None)

    yes_btn.callback = on_yes
    no_btn.callback = on_no
    view.add_item(yes_btn)
    view.add_item(no_btn)
    await interaction.response.edit_message(embed=embed, view=view)


async def finalize_single_field_edit(interaction: discord.Interaction, cog):
    """After editing one structured field (Region/Gender/Age/Interested In),
    check what's still missing and, if this profile isn't complete yet,
    offer a one-tap continuation into the next missing step rather than
    forcing the whole wizard again."""
    missing = await get_missing_dating_requirements(interaction.user.id)
    if not missing:
        await interaction.response.edit_message(content="✅ Profile updated!", embed=None, view=None)
        return

    step_map = {
        "Region": lambda i, c: show_region_step(i, c, finalize_single_field_edit),
        "Gender": lambda i, c: show_gender_step(i, c, finalize_single_field_edit),
        "Age Group": lambda i, c: show_age_step(i, c, finalize_single_field_edit),
        "Interested In": lambda i, c: show_interested_in_step(i, c, finalize_single_field_edit),
    }
    next_missing = missing[0]
    embed = discord.Embed(
        title="✅ Saved!",
        description=f"You're also still missing: **{', '.join(missing)}**.\nWant to set **{next_missing}** now?",
        color=config.PRIMARY_COLOR
    )
    view = discord.ui.View(timeout=300)
    owner_id = interaction.user.id

    now_btn = discord.ui.Button(label=f"Set {next_missing}", style=discord.ButtonStyle.success)
    later_btn = discord.ui.Button(label="Later", style=discord.ButtonStyle.secondary)

    async def on_now(i2: discord.Interaction):
        if i2.user.id != owner_id:
            await safe_respond(i2, content="This isn't your profile setup session.", ephemeral=True)
            return
        await step_map[next_missing](i2, cog)

    async def on_later(i2: discord.Interaction):
        if i2.user.id != owner_id:
            await safe_respond(i2, content="This isn't your profile setup session.", ephemeral=True)
            return
        await i2.response.edit_message(
            content=f"✅ Saved. Still missing: {', '.join(missing)}.", embed=None, view=None
        )

    now_btn.callback = on_now
    later_btn.callback = on_later
    view.add_item(now_btn)
    view.add_item(later_btn)
    await interaction.response.edit_message(embed=embed, view=view)


class EditChoiceView(discord.ui.View):
    """'What would you like to edit?' menu — the entry point for editing an
    existing profile. Only touches the section the user actually picks."""

    def __init__(self, cog, owner_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await safe_respond(interaction, content="This isn't your profile edit session.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📝 About Me / Intentions / Interests", style=discord.ButtonStyle.primary)
    async def edit_text(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_bio, current_intent, current_interests = "", "", ""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT bio, dating_intent, interests FROM profiles WHERE user_id = ?",
                (interaction.user.id,)
            ) as c:
                row = await c.fetchone()
        if row:
            current_bio = row[0] or ""
            current_intent = row[1] or ""
            try:
                current_interests = ", ".join(json.loads(row[2])) if row[2] else ""
            except Exception:
                current_interests = ""

        modal = ProfileEditModal(
            self.cog,
            current_bio=current_bio,
            current_intent=current_intent,
            current_interests=current_interests,
            is_new_profile=False,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🌎 Region", style=discord.ButtonStyle.secondary)
    async def edit_region(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_region_step(interaction, self.cog, finalize_single_field_edit)

    @discord.ui.button(label="⚧ Gender", style=discord.ButtonStyle.secondary)
    async def edit_gender(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_gender_step(interaction, self.cog, finalize_single_field_edit)

    @discord.ui.button(label="🎂 Age", style=discord.ButtonStyle.secondary)
    async def edit_age(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_age_step(interaction, self.cog, finalize_single_field_edit)

    @discord.ui.button(label="❤️ Interested In", style=discord.ButtonStyle.secondary)
    async def edit_interested_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_interested_in_step(interaction, self.cog, finalize_single_field_edit)

    @discord.ui.button(label="📸 Pictures/Media", style=discord.ButtonStyle.secondary)
    async def edit_media(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_media_edit_prompt(interaction, self.cog)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in EditChoiceView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again.", ephemeral=True)


class ProfileEditModal(discord.ui.Modal, title="Create / Edit Dating Profile"):
    def __init__(self, cog, current_bio="", current_intent="", current_interests="", is_new_profile=True):
        super().__init__()
        self.cog = cog
        self.is_new_profile = is_new_profile

        self.bio = discord.ui.TextInput(
            label="About Me",
            style=discord.TextStyle.paragraph,
            default=current_bio,
            max_length=500,
            required=True
        )
        self.dating_intent = discord.ui.TextInput(
            label="Dating Intentions",
            placeholder="Long-term relationship, casual...",
            default=current_intent,
            max_length=100,
            required=True
        )
        self.interests = discord.ui.TextInput(
            label=f"Interests (comma-separated, max {MAX_INTERESTS})",
            placeholder="Music, Travel, Fitness",
            default=current_interests,
            max_length=150,
            required=False
        )

        self.add_item(self.bio)
        self.add_item(self.dating_intent)
        self.add_item(self.interests)

    async def on_submit(self, interaction: discord.Interaction):
        interests_list = [i.strip() for i in self.interests.value.split(",") if i.strip()][:MAX_INTERESTS]
        guild_id = interaction.guild_id or (interaction.guild.id if interaction.guild else None)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO users (user_id, guild_id, dating_eligible, dating_enabled)
                    VALUES (?, ?, 1, 1)
                    ON CONFLICT(user_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                """, (interaction.user.id, guild_id))

                await db.execute("""
                    INSERT INTO profiles (user_id, guild_id, bio, photos, primary_photo, dating_intent, interests)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        bio = excluded.bio,
                        dating_intent = excluded.dating_intent,
                        interests = excluded.interests,
                        updated_at = CURRENT_TIMESTAMP
                """, (interaction.user.id, guild_id, self.bio.value.strip(), json.dumps([]), None,
                      self.dating_intent.value.strip(), json.dumps(interests_list)))
                await db.commit()

            if self.is_new_profile:
                # Full linear wizard for a brand-new profile
                embed = discord.Embed(title="🌎 Select your region", description="Where are you located?", color=config.PRIMARY_COLOR)

                async def on_region_choice(i2: discord.Interaction, label: str):
                    await apply_role_change(i2, config.REGION_ROLES, label, "location")
                    await show_gender_step(i2, self.cog, lambda i3, c: show_age_step(i3, c, lambda i4, c2: show_interested_in_step(i4, c2, show_media_step)))

                view = ChoiceStepView(list(config.REGION_ROLES.keys()), on_region_choice, interaction.user.id)
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            else:
                # Editing text fields only — save, then check if anything else is missing
                await recompute_dating_eligible(interaction.user.id)
                missing = await get_missing_dating_requirements(interaction.user.id)
                if not missing:
                    await safe_respond(interaction, content="✅ Profile updated!", ephemeral=True)
                else:
                    await safe_respond(
                        interaction,
                        content=f"✅ Profile updated. Still missing: {', '.join(missing)}. Use Edit Profile to set these.",
                        ephemeral=True
                    )

        except Exception:
            logger.exception("Error saving profile from modal")
            await safe_respond(interaction, content="❌ An error occurred while saving your profile.", ephemeral=True)


class PhotoConfirmView(discord.ui.View):
    def __init__(self, cog, ticket_id: int, ticket_owner_id: int, mode: str = "replace", max_items: int = MAX_MEDIA_ITEMS):
        super().__init__(timeout=None)
        self.cog = cog
        self.ticket_id = ticket_id
        self.ticket_owner_id = ticket_owner_id
        self.mode = mode
        self.max_items = max_items

    @discord.ui.button(label="✅ Confirm Media", style=discord.ButtonStyle.green, custom_id="photo_ticket:confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ticket_owner_id:
            await safe_respond(interaction, content="Only the ticket owner can confirm media.", ephemeral=True)
            return

        # Layer 1 — immediate in-memory guard. No await happens between the
        # check and the add, so two near-simultaneous clicks in this process
        # cannot both pass this check.
        if self.ticket_id in self.cog._confirming_tickets:
            await safe_respond(interaction, content="⏳ Your media is already being processed — please wait.", ephemeral=True)
            return
        self.cog._confirming_tickets.add(self.ticket_id)

        try:
            # Layer 2 — disable the button immediately so re-clicking can't
            # even generate a fresh interaction against it.
            button.disabled = True
            button.label = "Processing..."
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)

            # Layer 3 — atomic DB guard. Only the request that actually flips
            # confirmed 0->1 proceeds; this is the real idempotency guarantee,
            # independent of the button state or in-memory set.
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "UPDATE photo_tickets SET confirmed = 1 WHERE ticket_id = ? AND confirmed = 0",
                    (self.ticket_id,)
                )
                await db.commit()
                won_guard = cursor.rowcount > 0

            if not won_guard:
                await safe_respond(interaction, content="✅ This ticket has already been confirmed.", ephemeral=True)
                return

            source_channel = interaction.channel
            found = []
            try:
                async for msg in source_channel.history(limit=200, oldest_first=True):
                    for att in msg.attachments:
                        if att.content_type and (att.content_type.startswith("image/") or att.content_type.startswith("video/")):
                            found.append(att)
                    if len(found) >= self.max_items:
                        break
                found = found[:self.max_items]

                if not found:
                    # Nothing was actually processed — release the guard so they can retry.
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE photo_tickets SET confirmed = 0 WHERE ticket_id = ?", (self.ticket_id,))
                        await db.commit()
                    button.disabled = False
                    button.label = "✅ Confirm Media"
                    try:
                        await interaction.message.edit(view=self)
                    except Exception:
                        pass
                    await safe_respond(interaction, content="No uploaded photos or videos found. Please upload media directly (not links) and press Confirm again.", ephemeral=True)
                    return

                vault_channel = self.cog.bot.get_channel(config.CHANNEL_PHOTO_VAULT)
                if not vault_channel:
                    logger.error("CHANNEL_PHOTO_VAULT is not configured or accessible")
                    await safe_respond(interaction, content="⚠️ Media storage isn't configured correctly. Please contact an admin.", ephemeral=True)
                    return

                new_media = []
                for att in found:
                    try:
                        file = await att.to_file()
                        is_video = bool(att.content_type and att.content_type.startswith("video/"))
                        vault_msg = await vault_channel.send(
                            content=f"{'🎥' if is_video else '📸'} Profile media — <@{interaction.user.id}> (ticket #{self.ticket_id})",
                            file=file
                        )
                        new_media.append({"id": vault_msg.id, "type": "video" if is_video else "photo"})
                    except Exception:
                        logger.exception("Failed to archive a media item to the vault channel")

                if not new_media:
                    await safe_respond(interaction, content="❌ Failed to save media permanently. Please try again.", ephemeral=True)
                    return

                async with aiosqlite.connect(DB_PATH) as db:
                    existing_media = []
                    if self.mode == "append":
                        async with db.execute("SELECT photos FROM profiles WHERE user_id = ?", (interaction.user.id,)) as c:
                            prow = await c.fetchone()
                        if prow and prow[0]:
                            try:
                                existing_media = json.loads(prow[0])
                            except Exception:
                                existing_media = []

                    final_media = (existing_media + new_media)[:MAX_MEDIA_ITEMS]
                    primary = str(final_media[0]["id"]) if final_media else None

                    await db.execute(
                        "UPDATE profiles SET photos = ?, primary_photo = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (json.dumps(final_media), primary, interaction.user.id)
                    )
                    async with db.execute("SELECT channel_id FROM photo_tickets WHERE ticket_id = ?", (self.ticket_id,)) as c:
                        ticket_row = await c.fetchone()
                    await db.commit()

                await recompute_dating_eligible(interaction.user.id)
                missing = await get_missing_dating_requirements(interaction.user.id)
                note = "✅ Media saved to your profile! 🎉 Profile complete." if not missing else \
                    f"✅ Media saved to your profile. Still missing: {', '.join(missing)} — revisit Edit Profile to finish."
                await safe_respond(interaction, content=f"{note} Closing ticket...", ephemeral=True)

                monitor_task = self.cog._photo_ticket_monitors.pop(self.ticket_id, None)
                if monitor_task:
                    monitor_task.cancel()

                ticket_channel_id = ticket_row[0] if ticket_row else None
                if ticket_channel_id:
                    self.cog._photo_ticket_confirm_msgs.pop(ticket_channel_id, None)
                    ticket_channel = self.cog.bot.get_channel(ticket_channel_id)
                    if ticket_channel:
                        try:
                            await ticket_channel.delete(reason="Media upload confirmed by user")
                        except Exception:
                            pass

            except Exception:
                logger.exception("Error during confirm media")
                await safe_respond(interaction, content="❌ An error occurred while confirming media. Try again.", ephemeral=True)
        finally:
            self.cog._confirming_tickets.discard(self.ticket_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Unhandled error in PhotoConfirmView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong. Please try again.", ephemeral=True)


class DiscoveryCardView(discord.ui.View):
    def __init__(self, candidate: dict, media_index: int, cog):
        super().__init__(timeout=300)
        self.candidate = candidate
        self.media_index = media_index
        self.cog = cog
        self._media_cache: dict = {}  # avoids re-downloading unchanged thumbnails while flipping

    async def _render_files(self, guild: discord.Guild) -> List[discord.File]:
        return await self.cog.build_discovery_card_files(self.candidate, self.media_index, guild, cache=self._media_cache)

    @discord.ui.button(label="◀ PREV", style=discord.ButtonStyle.primary, custom_id="discovery:prev")
    async def prev_photo(self, interaction: discord.Interaction, button: discord.ui.Button):
        media = self.candidate.get("media", [])
        if not media:
            await interaction.response.defer()
            return
        self.media_index = (self.media_index - 1) % len(media)
        await interaction.response.defer()
        files = await self._render_files(interaction.guild)
        await interaction.edit_original_response(attachments=files, view=self)

    @discord.ui.button(label="NEXT ▶", style=discord.ButtonStyle.primary, custom_id="discovery:next")
    async def next_photo(self, interaction: discord.Interaction, button: discord.ui.Button):
        media = self.candidate.get("media", [])
        if not media:
            await interaction.response.defer()
            return
        self.media_index = (self.media_index + 1) % len(media)
        await interaction.response.defer()
        files = await self._render_files(interaction.guild)
        await interaction.edit_original_response(attachments=files, view=self)

    @discord.ui.button(label="❤️ LIKE", style=discord.ButtonStyle.green, custom_id=config.ID_DISCOVERY_LIKE)
    async def handle_like(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        liker_id = interaction.user.id
        target_id = self.candidate["user_id"]

        if not await validate_dating_contact(liker_id, target_id):
            await safe_respond(interaction, content="❌ Cannot process action: Safety boundary restriction.", ephemeral=True)
            return

        try:
            is_mutual = await self.cog.record_like(liker_id, target_id)
        except Exception:
            logger.exception("Error recording like")
            await safe_respond(interaction, content="⚠️ Failed to record like.", ephemeral=True)
            return

        if is_mutual:
            try:
                ticket_channel = await self.cog.create_match_ticket(interaction.guild, liker_id, target_id)
                channel_mention = ticket_channel.mention if ticket_channel else "private match room"
                await safe_respond(
                    interaction,
                    content=f"💕 **IT'S A MATCH!** You and <@{target_id}> liked each other!\nPrivate match room created: {channel_mention}",
                    ephemeral=True
                )
            except Exception:
                logger.exception("Failed to create match ticket")
                await safe_respond(interaction, content="⚠️ Match detected but failed to create ticket.", ephemeral=True)

        # Transition straight to the next card in place (like a swipe) rather
        # than stacking a separate confirmation message on top of the old card.
        await self.cog.serve_next_candidate(interaction, edit_message_id=interaction.message.id)

    @discord.ui.button(label="❌ PASS", style=discord.ButtonStyle.secondary, custom_id=config.ID_DISCOVERY_PASS)
    async def handle_pass(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Pass = simply skip this profile and move to the next one. No
        # lasting relationship between the two users beyond "don't show me
        # this person again" — unlike Block, it's not mutual or permanent
        # in terms of contact, and doesn't prevent them from finding you.
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.record_pass(interaction.user.id, self.candidate["user_id"])
        except Exception:
            logger.exception("Failed to record pass")
            await safe_respond(interaction, content="⚠️ Failed to process pass.", ephemeral=True)
            return
        await self.cog.serve_next_candidate(interaction, edit_message_id=interaction.message.id)

    @discord.ui.button(label="🚫 BLOCK", style=discord.ButtonStyle.danger, custom_id=config.ID_DISCOVERY_BLOCK)
    async def handle_block(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Block = a permanent, mutual safety action: unlike Pass, it stops
        # BOTH people from ever seeing, matching with, or contacting each
        # other again (enforced in validate_dating_contact and profile
        # viewing), regardless of who blocked whom.
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT OR IGNORE INTO blocks (user_id, blocked_user_id) VALUES (?, ?)", (interaction.user.id, self.candidate["user_id"]))
                await db.commit()
        except Exception:
            logger.exception("Failed to block candidate")
            await safe_respond(interaction, content="⚠️ Failed to block this person.", ephemeral=True)
            return
        await safe_respond(
            interaction,
            content="🚫 **Blocked.** Unlike Pass, this is mutual and permanent — you will no longer be able to see, match with, or view each other's profiles.",
            ephemeral=True
        )
        await self.cog.serve_next_candidate(interaction, edit_message_id=interaction.message.id)

    @discord.ui.button(label="ℹ️ INFO", style=discord.ButtonStyle.secondary, custom_id="discovery:info")
    async def info_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.show_user_profile(interaction, self.candidate["user_id"])

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Unhandled error in DiscoveryCardView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong processing that action. Please try again.", ephemeral=True)


class ProfileMediaView(discord.ui.View):
    """Read-only image-card media carousel for /profile, /profile-check, and
    self-view — Prev/Next navigation only, no dating actions. If owner_id is
    None, anyone can navigate (used for the public /profile-check post);
    otherwise only that user can (used for ephemeral profile views)."""

    def __init__(self, cog, media: List[dict], owner_id: Optional[int], *, display_name: str,
                 age_group: Optional[str], location: Optional[str], is_verified: bool,
                 tier_text: Optional[str], gender: Optional[str], interested_in: Optional[str],
                 interests: List[str], dating_intent: Optional[str], bio: Optional[str], timeout: int = 600):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.media = media or []
        self.owner_id = owner_id
        self.index = 0
        self._cache: dict = {}
        self.display_name = display_name
        self.age_group = age_group
        self.location = location
        self.is_verified = is_verified
        self.tier_text = tier_text
        self.gender = gender
        self.interested_in = interested_in
        self.interests = interests
        self.dating_intent = dating_intent
        self.bio = bio
        if len(self.media) <= 1:
            self.clear_items()

    async def render_files(self) -> List[discord.File]:
        return await build_card_files(
            self.cog._http_session, self.media, self.index,
            display_name=self.display_name, age_group=self.age_group, location=self.location,
            is_verified=self.is_verified, tier_text=self.tier_text, gender=self.gender,
            interested_in=self.interested_in, interests=self.interests or [],
            dating_intent=self.dating_intent, bio=self.bio, cache=self._cache,
            executor=self.cog._render_executor,
        )

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await safe_respond(interaction, content="This isn't your profile view.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ PREV", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        self.index = (self.index - 1) % len(self.media)
        await interaction.response.defer()
        files = await self.render_files()
        await interaction.edit_original_response(attachments=files, view=self)

    @discord.ui.button(label="NEXT ▶", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        self.index = (self.index + 1) % len(self.media)
        await interaction.response.defer()
        files = await self.render_files()
        await interaction.edit_original_response(attachments=files, view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.exception("Error in ProfileMediaView item %r", item)
        await safe_respond(interaction, content="⚠️ Something went wrong.", ephemeral=True)


async def validate_dating_contact(user_a_id: int, user_b_id: int) -> bool:
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

        if a_row[2] != b_row[2]:
            return False

    return True


class DatingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._photo_ticket_confirm_msgs = {}  # channel_id -> confirm_message_id
        self._photo_ticket_monitors = {}  # ticket_id -> task
        self._confirming_tickets = set()  # in-memory spam-click guard
        self._http_session: Optional[aiohttp.ClientSession] = None
        # Dedicated, small executor for card rendering. Using the default
        # (shared, up to ~32 threads) executor let several users' image
        # renders run fully concurrently, each holding its own set of
        # decoded images — a major contributor to the OOM crash under any
        # real load. Capping this to 2 keeps peak memory bounded regardless
        # of how many people are swiping at once (renders queue briefly
        # instead of piling up in parallel).
        self._render_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="card-render")

    async def cog_load(self):
        self.voice_cleanup_task.start()
        self._http_session = aiohttp.ClientSession()
        # Idempotent schema migration for existing deployments: add mode/max_items
        # to photo_tickets if they aren't already there.
        async with aiosqlite.connect(DB_PATH) as db:
            for stmt in (
                "ALTER TABLE photo_tickets ADD COLUMN mode TEXT DEFAULT 'replace'",
                "ALTER TABLE photo_tickets ADD COLUMN max_items INTEGER DEFAULT 5",
            ):
                try:
                    await db.execute(stmt)
                    await db.commit()
                except Exception:
                    pass  # column already exists
        asyncio.create_task(self._recover_photo_tickets())

    async def cog_unload(self):
        self.voice_cleanup_task.cancel()
        for t in self._photo_ticket_monitors.values():
            t.cancel()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._render_executor.shutdown(wait=False)

    @tasks.loop(minutes=1)
    async def voice_cleanup_task(self):
        now = datetime.datetime.utcnow()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT match_id, voice_channel_id, voice_empty_since, ticket_channel_id FROM matches WHERE status = 'ACTIVE' AND voice_channel_id IS NOT NULL") as cursor:
                active_vcs = await cursor.fetchall()

            for match_id, vc_id, empty_since_str, ticket_id in active_vcs:
                channel = self.bot.get_channel(vc_id)
                if not channel:
                    await db.execute("UPDATE matches SET voice_channel_id = NULL, voice_empty_since = NULL WHERE match_id = ?", (match_id,))
                    await db.commit()
                    continue

                if len(channel.members) == 0:
                    if not empty_since_str:
                        await db.execute("UPDATE matches SET voice_empty_since = ? WHERE match_id = ?", (now.isoformat(), match_id))
                        await db.commit()
                    else:
                        empty_start = datetime.datetime.fromisoformat(empty_since_str)
                        if (now - empty_start).total_seconds() >= 3600:
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
                    if empty_since_str:
                        await db.execute("UPDATE matches SET voice_empty_since = NULL WHERE match_id = ?", (match_id,))
                        await db.commit()

    async def _recover_photo_tickets(self):
        await asyncio.sleep(2)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ticket_id, user_id, channel_id, created_at, mode, max_items FROM photo_tickets WHERE confirmed = 0") as cursor:
                rows = await cursor.fetchall()

        now = datetime.datetime.utcnow()
        for ticket_id, user_id, channel_id, created_at, mode, max_items in rows:
            try:
                created_dt = now
                if isinstance(created_at, str):
                    try:
                        created_dt = datetime.datetime.fromisoformat(created_at)
                    except Exception:
                        try:
                            created_dt = datetime.datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            created_dt = now

                elapsed = (now - created_dt).total_seconds()
                remaining = PHOTO_TICKET_TIMEOUT - int(elapsed)
                if remaining <= 0:
                    ch = self.bot.get_channel(channel_id)
                    if ch:
                        try: await ch.delete(reason="Media ticket expired during downtime")
                        except Exception: pass
                    try:
                        user = await self.bot.fetch_user(user_id)
                        await user.send("❌ Your media upload ticket expired while the bot was offline. Please re-open profile editing to try again.")
                    except Exception:
                        pass
                    continue

                channel = self.bot.get_channel(channel_id)
                if not channel:
                    # Not in cache — likely a DM channel that didn't survive
                    # the restart. Re-resolve it via the user directly.
                    try:
                        user = await self.bot.fetch_user(user_id)
                        dm = await user.create_dm()
                        if dm.id == channel_id:
                            channel = dm
                    except Exception:
                        channel = None
                if channel:
                    view = PhotoConfirmView(self, ticket_id, user_id, mode=mode or "replace", max_items=max_items or MAX_MEDIA_ITEMS)
                    try:
                        msg = await channel.send("When you are ready, press Confirm Media below.", view=view)
                        self._photo_ticket_confirm_msgs[channel.id] = msg.id
                    except Exception:
                        pass

                task = asyncio.create_task(self._photo_ticket_monitor(ticket_id, channel_id, user_id, remaining))
                self._photo_ticket_monitors[ticket_id] = task
            except Exception:
                logger.exception("Error recovering ticket %s", ticket_id)

    async def create_photo_ticket(self, guild: discord.Guild, user: discord.User, mode: str = "replace", max_items: int = MAX_MEDIA_ITEMS):
        category = guild.get_channel(config.CATEGORY_PHOTO_TICKETS) if config.CATEGORY_PHOTO_TICKETS else guild.get_channel(config.CATEGORY_MATCHES)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }
        member = guild.get_member(user.id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        name = f"media-ticket-{clean_username(user.name)}"
        try:
            channel = await guild.create_text_channel(name=name, category=category, overwrites=overwrites)
        except Exception:
            logger.exception("Failed to create ticket channel")
            try:
                await user.send("❌ I couldn't create a private ticket channel in the server. Please ensure the bot has Manage Channels permission.")
            except Exception:
                pass
            return None

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO photo_tickets (user_id, channel_id, mode, max_items) VALUES (?, ?, ?, ?)",
                (user.id, channel.id, mode, max_items)
            )
            ticket_id = cursor.lastrowid
            await db.commit()

        embed = discord.Embed(
            title="Upload your profile media",
            description=(
                f"Upload up to {max_items} item(s) in this ticket **as direct attachments** (photos or videos).\n"
                "Pasted links are not supported — please upload the files themselves.\n"
                "When you are happy with your media, press **Confirm Media** below.\n"
                "If you do not confirm within 10 minutes the ticket will be removed and you'll be notified."
            ),
            color=config.PRIMARY_COLOR
        )
        view = PhotoConfirmView(self, ticket_id, user.id, mode=mode, max_items=max_items)
        try:
            confirm_msg = await channel.send(content=user.mention, embed=embed, view=view)
            self._photo_ticket_confirm_msgs[channel.id] = confirm_msg.id
        except Exception:
            logger.exception("Failed to post confirm message in ticket")

        try:
            dm = await user.create_dm()
            await dm.send(f"I created your media upload ticket: {channel.mention}. Upload media there and press Confirm when ready.", view=PhotoConfirmView(self, ticket_id, user.id, mode=mode, max_items=max_items))
        except Exception:
            pass

        task = asyncio.create_task(self._photo_ticket_monitor(ticket_id, channel.id, user.id, PHOTO_TICKET_TIMEOUT))
        self._photo_ticket_monitors[ticket_id] = task
        return channel

    async def create_dm_media_ticket(self, user: discord.User, mode: str = "replace", max_items: int = MAX_MEDIA_ITEMS):
        """DM equivalent of create_photo_ticket: no guild channel is created —
        the user's DM with the bot IS the ticket, since it's already private
        by nature."""
        try:
            dm = await user.create_dm()
        except Exception:
            logger.exception("Failed to open DM for media ticket")
            return None

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO photo_tickets (user_id, channel_id, mode, max_items) VALUES (?, ?, ?, ?)",
                (user.id, dm.id, mode, max_items)
            )
            ticket_id = cursor.lastrowid
            await db.commit()

        embed = discord.Embed(
            title="Upload your profile media",
            description=(
                f"Upload up to {max_items} item(s) right here in this DM **as direct attachments** (photos or videos).\n"
                "Pasted links are not supported — please upload the files themselves.\n"
                "When you are happy with your media, press **Confirm Media** below.\n"
                "If you do not confirm within 10 minutes this request will expire and you'll be notified."
            ),
            color=config.PRIMARY_COLOR
        )
        view = PhotoConfirmView(self, ticket_id, user.id, mode=mode, max_items=max_items)
        try:
            confirm_msg = await dm.send(embed=embed, view=view)
            self._photo_ticket_confirm_msgs[dm.id] = confirm_msg.id
        except Exception:
            logger.exception("Failed to post confirm message in DM")

        task = asyncio.create_task(self._photo_ticket_monitor(ticket_id, dm.id, user.id, PHOTO_TICKET_TIMEOUT))
        self._photo_ticket_monitors[ticket_id] = task
        return dm

    async def _photo_ticket_monitor(self, ticket_id: int, channel_id: int, user_id: int, timeout: int = PHOTO_TICKET_TIMEOUT):
        try:
            await asyncio.sleep(timeout)
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT confirmed FROM photo_tickets WHERE ticket_id = ?", (ticket_id,)) as c:
                    row = await c.fetchone()
            if row and row[0]:
                return

            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.delete(reason="Media ticket expired (no confirmation)")
                except Exception:
                    pass

            try:
                user = await self.bot.fetch_user(user_id)
                dm = await user.create_dm()
                await dm.send("❌ Your media upload ticket timed out (no confirmation within 10 minutes). You can re-open profile editing to try again.")
            except Exception:
                pass

            self._photo_ticket_confirm_msgs.pop(channel_id, None)
            self._photo_ticket_monitors.pop(ticket_id, None)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in photo ticket monitor")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        ch = message.channel
        if ch.id in self._photo_ticket_confirm_msgs:
            prev_id = self._photo_ticket_confirm_msgs.get(ch.id)
            try:
                if prev_id:
                    prev = await ch.fetch_message(prev_id)
                    try:
                        await prev.delete()
                    except Exception:
                        pass
            except Exception:
                pass

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT ticket_id, user_id, mode, max_items FROM photo_tickets WHERE channel_id = ?", (ch.id,)) as c:
                    row = await c.fetchone()
            if not row:
                return
            ticket_id, owner_id, mode, max_items = row
            new_msg = await ch.send("When you are ready, press Confirm Media below.", view=PhotoConfirmView(self, ticket_id, owner_id, mode=mode or "replace", max_items=max_items or MAX_MEDIA_ITEMS))
            self._photo_ticket_confirm_msgs[ch.id] = new_msg.id

    async def get_weighted_candidate(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT u.dating_pool, u.gender, u.age_group, u.location, r.tier, u.interested_in
                FROM users u
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                WHERE u.user_id = ?
            """, (user_id,)) as c:
                user_row = await c.fetchone()

            if not user_row:
                return None

            user_pool = user_row[0]
            user_gender = user_row[1]
            user_tier = user_row[4]
            user_interested_in = user_row[5]
            user_tier_idx = None
            if user_tier in config.FEMALE_TIER_ORDER:
                user_tier_idx = config.FEMALE_TIER_ORDER.index(user_tier)
            elif user_tier in config.MALE_TIER_ORDER:
                user_tier_idx = config.MALE_TIER_ORDER.index(user_tier)

            query = """
                SELECT u.user_id, u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests,
                       r.tier, r.overall_average,
                       EXISTS(SELECT 1 FROM likes WHERE liker_id = u.user_id AND target_id = ?) as has_liked_user,
                       u.interested_in
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
            cand_gender = cand[2]
            cand_interested_in = cand[11]

            # Reciprocal Gender <-> Interested-In matching. "Everyone" is a
            # wildcard on either side; otherwise both directions must agree.
            if user_interested_in and user_interested_in != "Everyone":
                wanted_gender = INTEREST_TO_GENDER.get(user_interested_in)
                if wanted_gender and cand_gender != wanted_gender:
                    continue
            if cand_interested_in and cand_interested_in != "Everyone":
                cand_wanted_gender = INTEREST_TO_GENDER.get(cand_interested_in)
                if cand_wanted_gender and cand_wanted_gender != user_gender:
                    continue

            if await validate_dating_contact(user_id, cand_id):
                weight = 10
                cand_tier = cand[8]
                cand_tier_idx = None
                if cand_tier in config.FEMALE_TIER_ORDER:
                    cand_tier_idx = config.FEMALE_TIER_ORDER.index(cand_tier)
                elif cand_tier in config.MALE_TIER_ORDER:
                    cand_tier_idx = config.MALE_TIER_ORDER.index(cand_tier)

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
            "media": await resolve_profile_media(self.bot, json.loads(chosen[5])) if chosen[5] else [],
            "dating_intent": chosen[6],
            "interests": json.loads(chosen[7]) if chosen[7] else [],
            "tier": chosen[8] or "Unrated",
            "average_score": chosen[9],
            "has_liked_user": bool(chosen[10]),
            "interested_in": chosen[11],
        }

    async def get_next_liked_you_candidate(self, user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            query = """
                SELECT u.user_id, u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests,
                       r.tier, r.overall_average, u.interested_in
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
                    "media": await resolve_profile_media(self.bot, json.loads(row[5])) if row[5] else [],
                    "dating_intent": row[6],
                    "interests": json.loads(row[7]) if row[7] else [],
                    "tier": row[8] or "Unrated",
                    "average_score": row[9],
                    "interested_in": row[10],
                }

        return None

    async def build_discovery_card_files(self, candidate: dict, media_index: int, guild: discord.Guild, cache: dict = None) -> List[discord.File]:
        member = guild.get_member(candidate["user_id"]) if guild else None
        display_name = member.display_name if member else f"Member {str(candidate['user_id'])[-4:]}"
        is_verified = bool(member and any(r.id == config.ROLE_VERIFIED for r in member.roles))
        tier_text = f"{candidate['tier']} · {candidate['average_score']}/10" if candidate.get('average_score') else candidate.get('tier')

        return await build_card_files(
            self._http_session,
            candidate.get("media", []),
            media_index,
            display_name=display_name,
            age_group=candidate.get('age_group'),
            location=candidate.get('location'),
            is_verified=is_verified,
            tier_text=tier_text,
            gender=candidate.get('gender'),
            interested_in=candidate.get('interested_in'),
            interests=candidate.get('interests') or [],
            dating_intent=candidate.get('dating_intent'),
            bio=candidate.get('bio'),
            cache=cache,
            executor=self._render_executor,
        )

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
            description=f"Congratulations {user_a.mention if user_a else user_a_id} & {user_b.mention if user_b else user_b_id}!\nYou both liked each other. This is your private match ticket to chat and create a voice room.",
            color=config.PRIMARY_COLOR
        )
        try:
            await channel.send(embed=welcome_embed)
        except Exception:
            pass
        return channel

    async def serve_next_candidate(self, interaction: discord.Interaction, edit_message_id: int = None):
        missing = await get_missing_dating_requirements(interaction.user.id)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM profiles WHERE user_id = ? AND bio IS NOT NULL AND bio != ''",
                (interaction.user.id,)
            ) as c:
                has_bio = (await c.fetchone()) is not None

        if not has_bio or missing:
            if interaction.guild:
                profile_link = f"https://discord.com/channels/{interaction.guild.id}/{config.CHANNEL_MY_PROFILE}"
            else:
                profile_link = f"<#{config.CHANNEL_MY_PROFILE}>"
            still_needed = (["a profile with About Me"] if not has_bio else []) + missing
            content = (
                "❌ You need to complete your dating profile before you can start discovering matches!\n"
                f"Still needed: {', '.join(still_needed)}.\n"
                f"Head to {profile_link} to finish setting up."
            )
            if edit_message_id:
                try:
                    await interaction.followup.edit_message(edit_message_id, content=content, attachments=[], view=None)
                    return
                except Exception:
                    pass
            await safe_respond(interaction, content=content, ephemeral=True)
            return

        candidate = await self.get_weighted_candidate(interaction.user.id)
        if not candidate:
            content = "🎉 You have viewed all available candidate profiles in your pool for now!"
            if edit_message_id:
                try:
                    await interaction.followup.edit_message(edit_message_id, content=content, attachments=[], view=None)
                    return
                except Exception:
                    pass
            await safe_respond(interaction, content=content, ephemeral=True)
            return

        view = DiscoveryCardView(candidate, 0, self)
        files = await self.build_discovery_card_files(candidate, 0, interaction.guild, cache=view._media_cache)

        media = candidate.get('media', [])
        max_media = min(5, len(media))
        for i in range(max_media):
            idx = i

            async def jump_callback(interaction2: discord.Interaction, button: discord.ui.Button, index=idx, viewref=view):
                viewref.media_index = index
                await interaction2.response.defer()
                jump_files = await viewref._render_files(interaction2.guild)
                await interaction2.edit_original_response(attachments=jump_files, view=viewref)

            btn = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.secondary, custom_id=f"discovery:jump:{i+1}")
            btn.callback = jump_callback
            view.add_item(btn)

        def make_indicator(idx, total):
            return ' '.join('●' if n == idx else '○' for n in range(total))

        indicator_label = make_indicator(0, max(1, len(media)))
        indicator_btn = discord.ui.Button(label=indicator_label, style=discord.ButtonStyle.gray, disabled=True, custom_id=f"discovery:indicator:{interaction.user.id}:{random.randint(1,100000)}")
        view.add_item(indicator_btn)

        if edit_message_id:
            try:
                await interaction.followup.edit_message(edit_message_id, content=None, attachments=files, view=view)
                return
            except Exception:
                pass
        await safe_respond(interaction, files=files, view=view, ephemeral=True)

    async def serve_next_liked_you_candidate(self, interaction: discord.Interaction):
        candidate = await self.get_next_liked_you_candidate(interaction.user.id)
        if not candidate:
            await safe_respond(interaction, content="🤩 No new profiles currently waiting in your Liked You feed!", ephemeral=True)
            return

        view = DiscoveryCardView(candidate, 0, self)
        files = await self.build_discovery_card_files(candidate, 0, interaction.guild, cache=view._media_cache)
        await safe_respond(interaction, content="🤩 **This person liked your profile!**", files=files, view=view, ephemeral=True)

    async def show_user_profile(self, interaction: discord.Interaction, target_id: int):
        if interaction.user.id != target_id:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT 1 FROM blocks WHERE (user_id = ? AND blocked_user_id = ?) OR (user_id = ? AND blocked_user_id = ?)",
                    (interaction.user.id, target_id, target_id, interaction.user.id)
                ) as c:
                    blocked = await c.fetchone()
            if blocked:
                await safe_respond(interaction, content="❌ This profile is unavailable.", ephemeral=True)
                return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests, r.tier, r.overall_average, x.level, u.interested_in
                FROM users u
                LEFT JOIN profiles p ON u.user_id = p.user_id
                LEFT JOIN rating_results r ON u.user_id = r.user_id
                LEFT JOIN xp x ON u.user_id = x.user_id
                WHERE u.user_id = ?
            """, (target_id,)) as cursor:
                row = await cursor.fetchone()

        if not row or not row[3]:
            ch_mention = f"<#{config.CHANNEL_MY_PROFILE}>" if config.CHANNEL_MY_PROFILE else "`#my-profile`"
            if target_id == interaction.user.id:
                await safe_respond(
                    interaction,
                    content=f"❌ You have not created a profile yet! Please go to {ch_mention} and click **🆕 Create Profile**.",
                    ephemeral=True
                )
            else:
                await safe_respond(interaction, content="❌ This member has not created a dating profile yet.", ephemeral=True)
            return

        media = await resolve_profile_media(self.bot, json.loads(row[4]) if row[4] else [])
        tier_base = f"{row[7]} · {row[8]}/10" if row[8] else (row[7] or None)
        level_val = row[9] or 1
        tier_str = f"{tier_base} · Lvl {level_val}" if tier_base else f"Lvl {level_val}"

        member = interaction.guild.get_member(target_id) if interaction.guild else None
        display_name = member.display_name if member else f"Member {str(target_id)[-4:]}"
        is_verified = bool(member and any(r.id == config.ROLE_VERIFIED for r in member.roles))
        interests_list = json.loads(row[6]) if row[6] else []

        view = ProfileMediaView(
            self, media, owner_id=interaction.user.id,
            display_name=display_name, age_group=row[0], location=row[2],
            is_verified=is_verified, tier_text=tier_str, gender=row[1],
            interested_in=row[10], interests=interests_list,
            dating_intent=row[5], bio=row[3],
        ) if media else None

        if view:
            files = await view.render_files()
        else:
            files = await build_card_files(
                self._http_session, [], 0,
                display_name=display_name, age_group=row[0], location=row[2],
                is_verified=is_verified, tier_text=tier_str, gender=row[1],
                interested_in=row[10], interests=interests_list,
                dating_intent=row[5], bio=row[3],
                executor=self._render_executor,
            )

        await safe_respond(interaction, files=files, view=view, ephemeral=True)

    @app_commands.command(name="profile", description="View a member's dating and rating profile card. Open to everyone.")
    async def view_profile_cmd(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user
        await self.show_user_profile(interaction, target.id)

    @app_commands.command(name="profile-check", description="Post your profile publicly in #profile-check for review. Open to everyone (10 min cooldown).")
    @app_commands.checks.cooldown(1, 600.0, key=lambda i: i.user.id)
    async def profile_check_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        try:
            if config.CHANNEL_PROFILE_CHECK and interaction.channel_id != config.CHANNEL_PROFILE_CHECK:
                await safe_respond(
                    interaction,
                    content=f"❌ This command can only be executed inside <#{config.CHANNEL_PROFILE_CHECK}>!",
                    ephemeral=True
                )
                return

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("""
                    SELECT u.age_group, u.gender, u.location, p.bio, p.photos, p.dating_intent, p.interests, r.tier, r.overall_average, u.interested_in
                    FROM users u
                    LEFT JOIN profiles p ON u.user_id = p.user_id
                    LEFT JOIN rating_results r ON u.user_id = r.user_id
                    WHERE u.user_id = ?
                """, (interaction.user.id,)) as cursor:
                    row = await cursor.fetchone()

            if not row or not row[3]:
                await safe_respond(interaction, content="❌ You have not created a dating profile yet! Set it up in `#my-profile` first.", ephemeral=True)
                return

            media = await resolve_profile_media(self.bot, json.loads(row[4]) if row[4] else [])
            tier_str = f"{row[7]} · {row[8]}/10" if row[8] else (row[7] or None)
            is_verified = any(r.id == config.ROLE_VERIFIED for r in interaction.user.roles)
            interests_list = json.loads(row[6]) if row[6] else []

            # Public post — anyone viewing can flip through the media (owner_id=None)
            view = ProfileMediaView(
                self, media, owner_id=None,
                display_name=interaction.user.display_name, age_group=row[0], location=row[2],
                is_verified=is_verified, tier_text=tier_str, gender=row[1],
                interested_in=row[9], interests=interests_list,
                dating_intent=row[5], bio=row[3],
            ) if media else None

            if view:
                files = await view.render_files()
            else:
                files = await build_card_files(
                    self._http_session, [], 0,
                    display_name=interaction.user.display_name, age_group=row[0], location=row[2],
                    is_verified=is_verified, tier_text=tier_str, gender=row[1],
                    interested_in=row[9], interests=interests_list,
                    dating_intent=row[5], bio=row[3],
                    executor=self._render_executor,
                )

            await safe_respond(
                interaction,
                content="📝 **PUBLIC PROFILE REVIEW** — community members can leave feedback below!",
                files=files, view=view, ephemeral=False
            )
        except Exception:
            logger.exception("Error in /profile-check")
            await safe_respond(interaction, content="❌ An error occurred posting your profile. Please try again.", ephemeral=True)

    @profile_check_cmd.error
    async def profile_check_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            minutes = int(error.retry_after // 60)
            seconds = int(error.retry_after % 60)
            await safe_respond(
                interaction,
                content=f"⏳ Cooldown active: You can post another profile review in **{minutes}m {seconds}s**.",
                ephemeral=True
            )
        else:
            logger.exception("Unhandled error in /profile-check", exc_info=error)
            await safe_respond(interaction, content="❌ An unexpected error occurred.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(DatingCog(bot))
