"""
card_renderer.py — generates a styled profile card image (PNG bytes) to
replace the old text-embed profile display.

Renders at 2x internal resolution then downsamples for crisp text/edges.
Fonts are bundled in fonts/ next to this file so rendering quality never
depends on what happens to be installed in the deployment container.
"""
from __future__ import annotations
import io
import math
import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageChops, ImageFilter

SCALE = 2  # internal supersampling factor for crisp text/gradients

CARD_W = 640 * SCALE
CARD_H = 900 * SCALE
PHOTO_H = 560 * SCALE
FILMSTRIP_H = 92 * SCALE
CORNER_RADIUS = 30 * SCALE
BORDER_WIDTH = 10 * SCALE

BG_COLOR = (16, 12, 20)
PANEL_COLOR = (24, 17, 29)
TEXT_WHITE = (252, 252, 253)
TEXT_MUTED = (196, 186, 202)
SHADOW_COLOR = (0, 0, 0, 160)

GRAD_BORDER = [(255, 61, 138), (191, 60, 220), (110, 90, 255), (61, 160, 255)]
GRAD_GENDER = [(93, 111, 255), (150, 90, 255)]
GRAD_INTERESTED = [(255, 78, 145), (255, 61, 100)]
GRAD_INTEREST_TAG = [(56, 189, 190), (61, 130, 246)]
GRAD_TIER = [(255, 205, 92), (255, 145, 66)]
GRAD_VERIFIED = [(61, 200, 255), (91, 120, 255)]
GRAD_ACTIVE_THUMB = [(255, 78, 178), (150, 90, 255)]

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

FONT_BOLD_CANDIDATES = [
    os.path.join(_FONT_DIR, "Poppins-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_SEMIBOLD_CANDIDATES = [
    os.path.join(_FONT_DIR, "Poppins-SemiBold.ttf"),
    os.path.join(_FONT_DIR, "Poppins-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_MEDIUM_CANDIDATES = [
    os.path.join(_FONT_DIR, "Poppins-Medium.ttf"),
    os.path.join(_FONT_DIR, "Poppins-Regular.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_REGULAR_CANDIDATES = [
    os.path.join(_FONT_DIR, "Poppins-Regular.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _load_font(paths, size):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    # Last-resort fallback. Pillow >=9.5 supports a size arg here; older
    # versions ignore it and render tiny — still better than crashing.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size[0], size[1]], radius=radius, fill=255)
    return mask


def _fit_cover(img, target_w, target_h):
    img = ImageOps.exif_transpose(img).convert("RGB")
    src_ratio = img.width / img.height
    tgt_ratio = target_w / target_h
    if src_ratio > tgt_ratio:
        new_h = target_h
        new_w = int(new_h * src_ratio)
    else:
        new_w = target_w
        new_h = int(new_w / src_ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _make_diagonal_gradient(w, h, colors):
    n = len(colors)
    diag = int(math.hypot(w, h)) + 40
    strip = Image.new("RGB", (n, 1))
    strip.putdata(colors)
    grad = strip.resize((diag, max(diag // 3, 40)), Image.BILINEAR)
    grad = grad.resize((diag, diag), Image.BILINEAR)
    grad = grad.rotate(35, expand=True, resample=Image.BILINEAR)
    gw, gh = grad.size
    left = (gw - w) // 2
    top = (gh - h) // 2
    return grad.crop((left, top, left + w, top + h))


def _draw_gradient_pill(card, x, y, w, h, colors, radius=None):
    radius = radius if radius is not None else h // 2
    grad = _make_diagonal_gradient(w, h, colors)
    mask = _rounded_mask((w, h), radius)
    card.paste(grad, (x, y), mask)


def _draw_gradient_ring(card, x, y, size, colors, thickness):
    outer_mask = _rounded_mask((size, size), size // 2)
    inner = Image.new("L", (size, size), 0)
    idraw = ImageDraw.Draw(inner)
    idraw.ellipse([thickness, thickness, size - thickness, size - thickness], fill=255)
    ring = ImageChops.subtract(outer_mask, inner)
    grad = _make_diagonal_gradient(size, size, colors)
    card.paste(grad, (x, y), ring)


def _text_with_shadow(draw, xy, text, font, fill, shadow_offset=2):
    x, y = xy
    so = shadow_offset * SCALE
    draw.text((x + so, y + so), text, font=font, fill=(0, 0, 0, 140))
    draw.text((x, y), text, font=font, fill=fill)


def _draw_pin_icon(draw, x, y, size, color):
    r = size // 2
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
    draw.ellipse([x - r // 2, y - r // 2, x + r // 2, y + r // 2], fill=BG_COLOR)
    draw.polygon([(x - r, y + r * 0.3), (x + r, y + r * 0.3), (x, y + size)], fill=color)


def _draw_gender_icon(draw, cx, cy, size, kind, color=(255, 255, 255)):
    """Hand-drawn gender glyphs — no reliance on font glyph coverage."""
    r = size * 0.32
    lw = max(2, int(size * 0.12))
    if kind == "woman":
        draw.ellipse([cx - r, cy - r * 1.1, cx + r, cy + r * 1.1], outline=color, width=lw)
        draw.line([(cx, cy + r * 1.1), (cx, cy + r * 2.1)], fill=color, width=lw)
        draw.line([(cx - r * 0.6, cy + r * 1.7), (cx + r * 0.6, cy + r * 1.7)], fill=color, width=lw)
    elif kind == "man":
        draw.ellipse([cx - r, cy - r * 0.5, cx + r, cy + r * 1.5], outline=color, width=lw)
        arrow_base = (cx + r * 0.6, cy - r * 0.9)
        draw.line([arrow_base, (cx + r * 1.5, cy - r * 1.8)], fill=color, width=lw)
        draw.line([(cx + r * 1.5, cy - r * 1.8), (cx + r * 0.7, cy - r * 1.8)], fill=color, width=lw)
        draw.line([(cx + r * 1.5, cy - r * 1.8), (cx + r * 1.5, cy - r * 1.0)], fill=color, width=lw)
    else:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=lw)
        draw.line([(cx - r * 0.8, cy), (cx + r * 0.8, cy)], fill=color, width=lw)


def _draw_heart_icon(draw, cx, cy, size, color=(255, 255, 255)):
    r = size * 0.28
    draw.ellipse([cx - r * 1.9, cy - r * 1.1, cx, cy + r * 0.9], fill=color)
    draw.ellipse([cx, cy - r * 1.1, cx + r * 1.9, cy + r * 0.9], fill=color)
    draw.polygon([(cx - r * 1.8, cy + r * 0.2), (cx + r * 1.8, cy + r * 0.2), (cx, cy + r * 2.3)], fill=color)


def _draw_crown_icon(draw, x, y, w, h, color=(255, 255, 255)):
    pts = [
        (x, y + h), (x, y + h * 0.4), (x + w * 0.22, y + h * 0.68),
        (x + w * 0.5, y), (x + w * 0.78, y + h * 0.68), (x + w, y + h * 0.4),
        (x + w, y + h),
    ]
    draw.polygon(pts, fill=color)
    for cx in (x, x + w * 0.5, x + w):
        draw.ellipse([cx - h * 0.09, y - h * 0.09, cx + h * 0.09, y + h * 0.09], fill=color)


def _text_wrap(draw, text, font, max_width):
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_gradient_chip(card, draw, x, y, text, font, colors, icon_fn=None,
                         fg=(255, 255, 255), pad_x=18, pad_y=10, icon_w=0):
    text_w = draw.textlength(text, font=font)
    h = font.size + pad_y * 2 - 4
    w = int(text_w) + pad_x * 2 + icon_w
    _draw_gradient_pill(card, x, y, w, h, colors, radius=h // 2)
    if icon_fn:
        icon_fn(draw, x + pad_x + icon_w // 2, y + h // 2, icon_w * 0.9)
    draw.text((x + pad_x + icon_w, y + pad_y - 2), text, font=font, fill=fg)
    return x + w + 12 * SCALE


def render_profile_card(
    *,
    main_image_bytes,
    is_video,
    thumbnails,
    active_index,
    display_name,
    age_group,
    location,
    is_verified,
    tier_text,
    gender,
    interested_in,
    interests,
    dating_intent,
    bio,
):
    card = Image.new("RGB", (CARD_W, CARD_H), BG_COLOR)

    font_name = _load_font(FONT_BOLD_CANDIDATES, 46 * SCALE)
    font_sub = _load_font(FONT_MEDIUM_CANDIDATES, 25 * SCALE)
    font_chip = _load_font(FONT_SEMIBOLD_CANDIDATES, 21 * SCALE)
    font_small = _load_font(FONT_REGULAR_CANDIDATES, 21 * SCALE)
    font_section = _load_font(FONT_SEMIBOLD_CANDIDATES, 23 * SCALE)

    # ---- Photo / video-placeholder zone ----
    if is_video:
        # Actual video playback happens via a separate native video
        # attachment sent alongside this image (Discord can't play video
        # inside a PNG) — this zone is just a clear visual cue to look below.
        base = _make_diagonal_gradient(CARD_W, PHOTO_H, [(70, 30, 90), (30, 20, 60)])
        card.paste(base, (0, 0))
        draw = ImageDraw.Draw(card)
        r = 60 * SCALE
        cx, cy = CARD_W // 2, int(PHOTO_H * 0.42)
        _draw_gradient_pill(card, cx - r, cy - r, r * 2, r * 2, GRAD_ACTIVE_THUMB, radius=r)
        draw.polygon([
            (cx - int(16 * SCALE), cy - int(26 * SCALE)),
            (cx - int(16 * SCALE), cy + int(26 * SCALE)),
            (cx + int(24 * SCALE), cy)
        ], fill=(255, 255, 255))
        note_font = _load_font(FONT_SEMIBOLD_CANDIDATES, 22 * SCALE)
        note = "Video attached below"
        nw = draw.textlength(note, font=note_font)
        note_y = cy + r + int(24 * SCALE)
        draw.text((CARD_W // 2 - nw / 2, note_y), note, font=note_font, fill=TEXT_WHITE)
        arrow_cx = CARD_W // 2
        arrow_y = note_y + note_font.size + int(10 * SCALE)
        aw, ah = int(14 * SCALE), int(10 * SCALE)
        draw.polygon([
            (arrow_cx - aw, arrow_y), (arrow_cx + aw, arrow_y), (arrow_cx, arrow_y + ah)
        ], fill=TEXT_WHITE)
    elif main_image_bytes:
        try:
            photo = Image.open(io.BytesIO(main_image_bytes))
            photo = _fit_cover(photo, CARD_W, PHOTO_H)
            card.paste(photo, (0, 0))
        except Exception:
            card.paste(Image.new("RGB", (CARD_W, PHOTO_H), (48, 36, 56)), (0, 0))
        draw = ImageDraw.Draw(card)
    else:
        card.paste(Image.new("RGB", (CARD_W, PHOTO_H), (48, 36, 56)), (0, 0))
        draw = ImageDraw.Draw(card)

    # bottom gradient wash for text legibility, tinted warm purple/magenta
    grad_h = int(300 * SCALE)
    gradient = Image.new("RGBA", (CARD_W, grad_h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(gradient)
    for i in range(grad_h):
        t = i / grad_h
        alpha = int(225 * (t ** 0.7))
        r = int(8 + 36 * t)
        g = int(4 + 8 * t)
        b = int(14 + 26 * t)
        gdraw.line([(0, i), (CARD_W, i)], fill=(r, g, b, alpha))
    card_rgba = card.convert("RGBA")
    card_rgba.alpha_composite(gradient, (0, PHOTO_H - grad_h))
    card = card_rgba.convert("RGB")
    draw = ImageDraw.Draw(card)

    pad = 22 * SCALE

    if is_verified:
        badge_r = 30 * SCALE
        vx, vy = CARD_W - pad - badge_r * 2, pad
        _draw_gradient_pill(card, vx, vy, badge_r * 2, badge_r * 2, GRAD_VERIFIED, radius=badge_r)
        cx, cy = vx + badge_r, vy + badge_r
        draw.line(
            [(cx - badge_r * 0.35, cy), (cx - badge_r * 0.1, cy + badge_r * 0.3), (cx + badge_r * 0.4, cy - badge_r * 0.35)],
            fill=(255, 255, 255), width=max(3, int(badge_r * 0.22)), joint="curve"
        )

    if tier_text:
        icon_w = int(34 * SCALE)
        _draw_gradient_chip(card, draw, pad, pad, tier_text, font_chip, GRAD_TIER, icon_w=icon_w, fg=(48, 24, 4))
        _draw_crown_icon(draw, pad + 10 * SCALE, pad + 12 * SCALE, icon_w - 14 * SCALE, (font_chip.size - 12 * SCALE), color=(120, 55, 0))

    total_media = len(thumbnails) if thumbnails else 0
    if total_media:
        counter_text = f"{active_index + 1}/{total_media}"
        tw = draw.textlength(counter_text, font=font_small)
        cx0 = CARD_W - tw - pad - 14 * SCALE
        cy0 = PHOTO_H - 46 * SCALE
        draw.rounded_rectangle(
            [cx0 - 12 * SCALE, cy0 - 6 * SCALE, cx0 + tw + 12 * SCALE, cy0 + 32 * SCALE],
            radius=15 * SCALE, fill=(0, 0, 0)
        )
        draw.text((cx0, cy0), counter_text, font=font_small, fill=TEXT_WHITE)

    name_line = display_name if not age_group else f"{display_name}, {age_group}"
    _text_with_shadow(draw, (pad, PHOTO_H - 98 * SCALE), name_line, font_name, TEXT_WHITE)

    if location:
        _draw_pin_icon(draw, pad + 12 * SCALE, PHOTO_H - 32 * SCALE, 15 * SCALE, (255, 140, 195))
        _text_with_shadow(draw, (pad + 28 * SCALE, PHOTO_H - 44 * SCALE), location, font_sub, TEXT_MUTED, shadow_offset=1)

    # ---- Filmstrip ----
    strip_y = PHOTO_H + 12 * SCALE
    thumb_size = FILMSTRIP_H - 24 * SCALE
    x_cursor = pad
    for i, (thumb_bytes, thumb_is_video) in enumerate(thumbnails[:5]):
        try:
            if thumb_bytes:
                t_img = Image.open(io.BytesIO(thumb_bytes))
                t_img = _fit_cover(t_img, thumb_size, thumb_size)
            else:
                t_img = Image.new("RGB", (thumb_size, thumb_size), (48, 36, 56))
        except Exception:
            t_img = Image.new("RGB", (thumb_size, thumb_size), (48, 36, 56))

        if i == active_index:
            ring_pad = 5 * SCALE
            _draw_gradient_ring(
                card, x_cursor - ring_pad, strip_y - ring_pad,
                thumb_size + ring_pad * 2, GRAD_ACTIVE_THUMB, thickness=int(3.5 * SCALE)
            )

        mask = _rounded_mask((thumb_size, thumb_size), 11 * SCALE)
        card.paste(t_img, (x_cursor, strip_y), mask)
        draw = ImageDraw.Draw(card)

        if i != active_index:
            draw.rounded_rectangle(
                [x_cursor, strip_y, x_cursor + thumb_size, strip_y + thumb_size],
                radius=11 * SCALE, outline=(85, 74, 95), width=int(1.5 * SCALE)
            )

        if thumb_is_video:
            pr = thumb_size * 0.16
            pcx, pcy = x_cursor + thumb_size // 2, strip_y + thumb_size // 2
            draw.ellipse([pcx - pr * 1.4, pcy - pr * 1.4, pcx + pr * 1.4, pcy + pr * 1.4], fill=(0, 0, 0, 150))
            draw.polygon([
                (pcx - pr * 0.6, pcy - pr), (pcx - pr * 0.6, pcy + pr), (pcx + pr * 1.1, pcy)
            ], fill=(255, 255, 255))
        x_cursor += thumb_size + 14 * SCALE

    # ---- Info panel ----
    panel_y = PHOTO_H + FILMSTRIP_H
    draw.rectangle([0, panel_y, CARD_W, CARD_H], fill=PANEL_COLOR)
    # subtle top highlight line for separation/pop
    draw.rectangle([0, panel_y, CARD_W, panel_y + int(2 * SCALE)], fill=(70, 50, 90))

    y = panel_y + 22 * SCALE
    x = pad

    x2 = x
    if gender:
        icon_w = int(30 * SCALE)
        kind = "man" if gender.lower() == "man" else ("woman" if gender.lower() == "woman" else "other")
        x2 = _draw_gradient_chip(
            card, draw, x, y, gender, font_chip, GRAD_GENDER, icon_w=icon_w,
            icon_fn=lambda d, cx, cy, s, k=kind: _draw_gender_icon(d, cx, cy, s, k)
        )
    if interested_in:
        icon_w = int(30 * SCALE)
        _draw_gradient_chip(
            card, draw, x2, y, f"Into {interested_in}", font_chip, GRAD_INTERESTED, icon_w=icon_w,
            icon_fn=lambda d, cx, cy, s: _draw_heart_icon(d, cx, cy, s)
        )
    y += 52 * SCALE

    if interests:
        cx = x
        cy = y
        max_x = CARD_W - pad
        for tag in interests[:8]:
            tag_w = draw.textlength(tag, font=font_chip) + 34 * SCALE
            if cx + tag_w > max_x:
                cx = x
                cy += 48 * SCALE
            cx = _draw_gradient_chip(card, draw, cx, cy, tag, font_chip, GRAD_INTEREST_TAG)
        y = cy + 52 * SCALE
    else:
        y += 8 * SCALE

    if dating_intent:
        draw.text((x, y), "Looking for", font=font_small, fill=TEXT_MUTED)
        y += 26 * SCALE
        draw.text((x, y), dating_intent, font=font_section, fill=TEXT_WHITE)
        y += 42 * SCALE

    if bio:
        max_bio_h = CARD_H - y - 20 * SCALE
        line_h = 27 * SCALE
        max_lines = max(1, int(max_bio_h // line_h))
        lines = _text_wrap(draw, bio, font_small, CARD_W - pad * 2)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            if lines:
                last = lines[-1]
                while draw.textlength(last + "...", font=font_small) > CARD_W - pad * 2 and len(last) > 3:
                    last = last[:-1]
                lines[-1] = last + "..."
        for line in lines:
            draw.text((x, y), line, font=font_small, fill=TEXT_MUTED)
            y += line_h

    # ---- Rounded content + glowing gradient border frame ----
    content_mask = _rounded_mask((CARD_W, CARD_H), CORNER_RADIUS)
    content = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    content.paste(card, (0, 0), content_mask)

    outer_w, outer_h = CARD_W + BORDER_WIDTH * 2, CARD_H + BORDER_WIDTH * 2
    outer_mask = _rounded_mask((outer_w, outer_h), CORNER_RADIUS + BORDER_WIDTH)
    border_grad = _make_diagonal_gradient(outer_w, outer_h, GRAD_BORDER)
    final = Image.new("RGBA", (outer_w, outer_h), (0, 0, 0, 0))
    final.paste(border_grad, (0, 0), outer_mask)
    final.paste(content, (BORDER_WIDTH, BORDER_WIDTH), content)

    # Downsample from 2x supersampled render for crisp anti-aliased result
    final = final.resize((outer_w // SCALE, outer_h // SCALE), Image.LANCZOS)

    buf = io.BytesIO()
    final.save(buf, format="PNG")
    return buf.getvalue()
