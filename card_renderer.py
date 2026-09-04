"""
card_renderer.py — generates a styled profile card image (PNG bytes) to
replace the old text-embed profile display.

Memory-safety notes (this file previously caused OOM crashes in production):
- Photos are decoded via PIL's draft() mode, which uses JPEG's built-in
  DCT downscaling to avoid ever materializing a full-resolution decode of
  a user's original camera photo (which can be 30-40MB+ per image).
- No supersampling: text legibility comes from the bundled font, not from
  rendering at 2x/4x resolution and downsampling, which multiplied peak
  memory use for comparatively little visual benefit.
- Gradients are built via cheap resize operations only — no large rotated
  intermediates.
- Fonts are bundled in fonts/ next to this file so rendering quality never
  depends on what happens to be installed in the deployment container.
"""
from __future__ import annotations
import io
import os
import unicodedata
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageChops

CARD_W = 640
CARD_H = 900
PHOTO_H = 560
FILMSTRIP_H = 88
CORNER_RADIUS = 28
BORDER_WIDTH = 8

MAX_DECODE_DIM = 1600

BG_COLOR = (16, 12, 20)
PANEL_COLOR = (24, 17, 29)
TEXT_WHITE = (252, 252, 253)
TEXT_MUTED = (196, 186, 202)

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
    try:
        img.draft("RGB", (target_w, target_h))
    except Exception:
        pass

    img = ImageOps.exif_transpose(img).convert("RGB")

    if img.width > MAX_DECODE_DIM or img.height > MAX_DECODE_DIM:
        img.thumbnail((MAX_DECODE_DIM, MAX_DECODE_DIM), Image.BILINEAR)

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


def _make_gradient(w, h, colors):
    n = len(colors)
    strip = Image.new("RGB", (n, 1))
    strip.putdata(colors)
    grad = strip.resize((max(w, 2), 1), Image.BILINEAR)
    grad = grad.resize((max(w, 2), max(h, 2)), Image.BILINEAR)
    return grad.crop((0, 0, w, h))


def _draw_gradient_pill(card, x, y, w, h, colors, radius=None):
    radius = radius if radius is not None else h // 2
    grad = _make_gradient(w, h, colors)
    mask = _rounded_mask((w, h), radius)
    card.paste(grad, (x, y), mask)


def _draw_gradient_ring(card, x, y, size, colors, thickness):
    outer_mask = _rounded_mask((size, size), size // 2)
    inner = Image.new("L", (size, size), 0)
    idraw = ImageDraw.Draw(inner)
    idraw.ellipse([thickness, thickness, size - thickness, size - thickness], fill=255)
    ring = ImageChops.subtract(outer_mask, inner)
    grad = _make_gradient(size, size, colors)
    card.paste(grad, (x, y), ring)


def _draw_pin_icon(draw, x, y, size, color):
    r = size // 2
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
    draw.ellipse([x - r // 2, y - r // 2, x + r // 2, y + r // 2], fill=BG_COLOR)
    draw.polygon([(x - r, y + r * 0.3), (x + r, y + r * 0.3), (x, y + size)], fill=color)


def _draw_gender_icon(draw, cx, cy, size, kind, color=(255, 255, 255)):
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


def _text_with_shadow(draw, xy, text, font, fill, shadow_offset=2):
    x, y = xy
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)


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
                         fg=(255, 255, 255), pad_x=16, pad_y=9, icon_w=0):
    text_w = draw.textlength(text, font=font)
    h = font.size + pad_y * 2 - 4
    w = int(text_w) + pad_x * 2 + icon_w
    _draw_gradient_pill(card, x, y, w, h, colors, radius=h // 2)
    if icon_fn:
        icon_fn(draw, x + pad_x + icon_w // 2, y + h // 2, icon_w * 0.9)
    draw.text((x + pad_x + icon_w, y + pad_y - 2), text, font=font, fill=fg)
    return x + w + 10


def _sanitize_display_text(text):
    """Many Discord display names use 'fancy' stylized unicode — mathematical
    bold/italic/script letter blocks, full-width forms, circled letters, etc.
    — that our bundled font (Poppins) has no glyphs for, rendering as blank
    tofu boxes. NFKD normalization decomposes most of these back to their
    plain-Latin equivalent (that's what those Unicode blocks are FOR —
    compatibility decomposition to the base letter), which the font can
    actually render. Combining marks picked up by decomposition (accents)
    are dropped too, since an accented form may still be missing even once
    the base letter resolves."""
    if not text:
        return text
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def render_profile_card(
    *,
    main_image_bytes,
    is_video,
    video_available=True,
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
    display_name = _sanitize_display_text(display_name)
    card = Image.new("RGB", (CARD_W, CARD_H), BG_COLOR)

    font_name = _load_font(FONT_BOLD_CANDIDATES, 38)
    font_sub = _load_font(FONT_MEDIUM_CANDIDATES, 21)
    font_chip = _load_font(FONT_SEMIBOLD_CANDIDATES, 18)
    font_small = _load_font(FONT_REGULAR_CANDIDATES, 18)
    font_section = _load_font(FONT_SEMIBOLD_CANDIDATES, 20)

    if is_video:
        base = _make_gradient(CARD_W, PHOTO_H, [(70, 30, 90), (30, 20, 60)])
        card.paste(base, (0, 0))
        draw = ImageDraw.Draw(card)
        r = 50
        cx, cy = CARD_W // 2, int(PHOTO_H * 0.42)
        _draw_gradient_pill(card, cx - r, cy - r, r * 2, r * 2, GRAD_ACTIVE_THUMB, radius=r)
        draw.polygon([(cx - 13, cy - 22), (cx - 13, cy + 22), (cx + 20, cy)], fill=(255, 255, 255))
        note_font = font_section
        note = "Video attached below" if video_available else "Video couldn't load in time"
        nw = draw.textlength(note, font=note_font)
        note_y = cy + r + 20
        draw.text((CARD_W // 2 - nw / 2, note_y), note, font=note_font, fill=TEXT_WHITE)
        if video_available:
            arrow_cx = CARD_W // 2
            arrow_y = note_y + note_font.size + 8
            draw.polygon([(arrow_cx - 12, arrow_y), (arrow_cx + 12, arrow_y), (arrow_cx, arrow_y + 8)], fill=TEXT_WHITE)
        else:
            retry_font = font_small
            retry = "Try navigating back to this slide again"
            rw = draw.textlength(retry, font=retry_font)
            draw.text((CARD_W // 2 - rw / 2, note_y + note_font.size + 10), retry, font=retry_font, fill=TEXT_MUTED)
    elif main_image_bytes:
        try:
            with Image.open(io.BytesIO(main_image_bytes)) as opened:
                photo = _fit_cover(opened, CARD_W, PHOTO_H)
            card.paste(photo, (0, 0))
            del photo
        except Exception:
            card.paste(Image.new("RGB", (CARD_W, PHOTO_H), (48, 36, 56)), (0, 0))
        draw = ImageDraw.Draw(card)
    else:
        card.paste(Image.new("RGB", (CARD_W, PHOTO_H), (48, 36, 56)), (0, 0))
        draw = ImageDraw.Draw(card)

    grad_h = 260
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
    del card_rgba, gradient
    draw = ImageDraw.Draw(card)

    pad = 20

    if is_verified:
        badge_r = 26
        vx, vy = CARD_W - pad - badge_r * 2, pad
        _draw_gradient_pill(card, vx, vy, badge_r * 2, badge_r * 2, GRAD_VERIFIED, radius=badge_r)
        cx, cy = vx + badge_r, vy + badge_r
        draw.line(
            [(cx - badge_r * 0.35, cy), (cx - badge_r * 0.1, cy + badge_r * 0.3), (cx + badge_r * 0.4, cy - badge_r * 0.35)],
            fill=(255, 255, 255), width=max(3, int(badge_r * 0.22)), joint="curve"
        )

    if tier_text:
        icon_w = 28
        _draw_gradient_chip(card, draw, pad, pad, tier_text, font_chip, GRAD_TIER, icon_w=icon_w, fg=(48, 24, 4))
        _draw_crown_icon(draw, pad + 8, pad + 10, icon_w - 12, font_chip.size - 10, color=(120, 55, 0))

    total_media = len(thumbnails) if thumbnails else 0
    if total_media:
        counter_text = f"{active_index + 1}/{total_media}"
        tw = draw.textlength(counter_text, font=font_small)
        cx0 = CARD_W - tw - pad - 12
        cy0 = PHOTO_H - 40
        draw.rounded_rectangle([cx0 - 10, cy0 - 5, cx0 + tw + 10, cy0 + 28], radius=13, fill=(0, 0, 0))
        draw.text((cx0, cy0), counter_text, font=font_small, fill=TEXT_WHITE)

    name_line = display_name if not age_group else f"{display_name}, {age_group}"
    _text_with_shadow(draw, (pad, PHOTO_H - 86), name_line, font_name, TEXT_WHITE)

    if location:
        _draw_pin_icon(draw, pad + 10, PHOTO_H - 28, 13, (255, 140, 195))
        _text_with_shadow(draw, (pad + 24, PHOTO_H - 38), location, font_sub, TEXT_MUTED, shadow_offset=1)

    strip_y = PHOTO_H + 10
    thumb_size = FILMSTRIP_H - 22
    x_cursor = pad
    for i, (thumb_bytes, thumb_is_video) in enumerate((thumbnails or [])[:5]):
        try:
            if thumb_bytes:
                with Image.open(io.BytesIO(thumb_bytes)) as opened:
                    t_img = _fit_cover(opened, thumb_size, thumb_size)
            else:
                t_img = Image.new("RGB", (thumb_size, thumb_size), (48, 36, 56))
        except Exception:
            t_img = Image.new("RGB", (thumb_size, thumb_size), (48, 36, 56))

        if i == active_index:
            ring_pad = 4
            _draw_gradient_ring(
                card, x_cursor - ring_pad, strip_y - ring_pad,
                thumb_size + ring_pad * 2, GRAD_ACTIVE_THUMB, thickness=3
            )

        mask = _rounded_mask((thumb_size, thumb_size), 10)
        card.paste(t_img, (x_cursor, strip_y), mask)
        del t_img
        draw = ImageDraw.Draw(card)

        if i != active_index:
            draw.rounded_rectangle(
                [x_cursor, strip_y, x_cursor + thumb_size, strip_y + thumb_size],
                radius=10, outline=(85, 74, 95), width=1
            )

        if thumb_is_video:
            pr = thumb_size * 0.16
            pcx, pcy = x_cursor + thumb_size // 2, strip_y + thumb_size // 2
            draw.ellipse([pcx - pr * 1.4, pcy - pr * 1.4, pcx + pr * 1.4, pcy + pr * 1.4], fill=(0, 0, 0))
            draw.polygon([(pcx - pr * 0.6, pcy - pr), (pcx - pr * 0.6, pcy + pr), (pcx + pr * 1.1, pcy)], fill=(255, 255, 255))
        x_cursor += thumb_size + 12

    panel_y = PHOTO_H + FILMSTRIP_H
    draw.rectangle([0, panel_y, CARD_W, CARD_H], fill=PANEL_COLOR)
    draw.rectangle([0, panel_y, CARD_W, panel_y + 2], fill=(70, 50, 90))

    y = panel_y + 20
    x = pad

    x2 = x
    if gender:
        icon_w = 26
        kind = "man" if gender.lower() == "man" else ("woman" if gender.lower() == "woman" else "other")
        x2 = _draw_gradient_chip(
            card, draw, x, y, gender, font_chip, GRAD_GENDER, icon_w=icon_w,
            icon_fn=lambda d, cx, cy, s, k=kind: _draw_gender_icon(d, cx, cy, s, k)
        )
    if interested_in:
        icon_w = 26
        _draw_gradient_chip(
            card, draw, x2, y, f"Into {interested_in}", font_chip, GRAD_INTERESTED, icon_w=icon_w,
            icon_fn=lambda d, cx, cy, s: _draw_heart_icon(d, cx, cy, s)
        )
    y += 46

    if interests:
        cx = x
        cy = y
        max_x = CARD_W - pad
        for tag in interests[:8]:
            tag_w = draw.textlength(tag, font=font_chip) + 30
            if cx + tag_w > max_x:
                cx = x
                cy += 44
            cx = _draw_gradient_chip(card, draw, cx, cy, tag, font_chip, GRAD_INTEREST_TAG)
        y = cy + 46
    else:
        y += 8

    if dating_intent:
        draw.text((x, y), "Looking for", font=font_small, fill=TEXT_MUTED)
        y += 24
        draw.text((x, y), dating_intent, font=font_section, fill=TEXT_WHITE)
        y += 38

    if bio:
        max_bio_h = CARD_H - y - 18
        line_h = 24
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

    content_mask = _rounded_mask((CARD_W, CARD_H), CORNER_RADIUS)
    content = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    content.paste(card, (0, 0), content_mask)
    del card, content_mask

    outer_w, outer_h = CARD_W + BORDER_WIDTH * 2, CARD_H + BORDER_WIDTH * 2
    outer_mask = _rounded_mask((outer_w, outer_h), CORNER_RADIUS + BORDER_WIDTH)
    border_grad = _make_gradient(outer_w, outer_h, GRAD_BORDER)
    final = Image.new("RGBA", (outer_w, outer_h), (0, 0, 0, 0))
    final.paste(border_grad, (0, 0), outer_mask)
    final.paste(content, (BORDER_WIDTH, BORDER_WIDTH), content)
    del outer_mask, border_grad, content

    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=False)
    del final
    return buf.getvalue()
