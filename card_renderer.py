"""
card_renderer.py — generates a styled profile card image (PNG bytes) to
replace the old text-embed profile display, per the new image-based UI.
"""
from __future__ import annotations
import io
import math
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageChops

CARD_W = 640
CARD_H = 900
PHOTO_H = 560
FILMSTRIP_H = 84
CORNER_RADIUS = 28
BORDER_WIDTH = 10

BG_COLOR = (18, 14, 22)
PANEL_COLOR = (28, 20, 32)
TEXT_WHITE = (248, 248, 250)
TEXT_MUTED = (200, 190, 205)

GRAD_BORDER = [(255, 61, 138), (191, 60, 220), (110, 90, 255), (61, 160, 255)]
GRAD_GENDER = [(93, 111, 255), (150, 90, 255)]
GRAD_INTERESTED = [(255, 78, 145), (255, 61, 100)]
GRAD_INTEREST_TAG = [(56, 189, 190), (61, 130, 246)]
GRAD_TIER = [(255, 196, 77), (255, 138, 61)]
GRAD_VERIFIED = [(61, 200, 255), (91, 120, 255)]
GRAD_ACTIVE_THUMB = [(255, 78, 178), (150, 90, 255)]

FONT_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def _load_font(paths, size):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
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


def _draw_pin_icon(draw, x, y, size, color):
    r = size // 2
    draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
    draw.ellipse([x - r // 2, y - r // 2, x + r // 2, y + r // 2], fill=BG_COLOR)
    draw.polygon([(x - r, y + r * 0.3), (x + r, y + r * 0.3), (x, y + size)], fill=color)


def _draw_check_badge(card, draw, x, y, size):
    _draw_gradient_pill(card, x, y, size, size, GRAD_VERIFIED, radius=size // 2)
    cx, cy = x + size * 0.28, y + size * 0.53
    draw.line(
        [(cx, cy), (cx + size * 0.18, cy + size * 0.18), (cx + size * 0.5, cy - size * 0.22)],
        fill=(255, 255, 255), width=max(2, size // 8), joint="curve"
    )


def _draw_crown(draw, x, y, w, h, color=(255, 255, 255)):
    pts = [
        (x, y + h), (x, y + h * 0.35), (x + w * 0.25, y + h * 0.65),
        (x + w * 0.5, y), (x + w * 0.75, y + h * 0.65), (x + w, y + h * 0.35),
        (x + w, y + h), (x, y + h)
    ]
    draw.polygon(pts, fill=color)


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


def _draw_gradient_chip(card, draw, x, y, text, font, colors, fg=(255, 255, 255),
                         pad_x=16, pad_y=8, icon_w=0):
    text_w = draw.textlength(text, font=font)
    h = font.size + pad_y * 2 - 4
    w = int(text_w) + pad_x * 2 + icon_w
    _draw_gradient_pill(card, x, y, w, h, colors, radius=h // 2)
    draw.text((x + pad_x + icon_w, y + pad_y - 2), text, font=font, fill=fg)
    return x + w + 10


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

    font_name = _load_font(FONT_PATHS_BOLD, 42)
    font_sub = _load_font(FONT_PATHS_REGULAR, 24)
    font_chip = _load_font(FONT_PATHS_BOLD, 20)
    font_small = _load_font(FONT_PATHS_REGULAR, 20)
    font_section = _load_font(FONT_PATHS_BOLD, 22)

    if main_image_bytes:
        try:
            photo = Image.open(io.BytesIO(main_image_bytes))
            photo = _fit_cover(photo, CARD_W, PHOTO_H)
        except Exception:
            photo = Image.new("RGB", (CARD_W, PHOTO_H), (52, 40, 60))
    else:
        photo = Image.new("RGB", (CARD_W, PHOTO_H), (52, 40, 60))

    card.paste(photo, (0, 0))
    draw = ImageDraw.Draw(card)

    if is_video:
        r = 46
        cx, cy = CARD_W // 2, PHOTO_H // 2
        overlay = Image.new("RGBA", (CARD_W, PHOTO_H), (0, 0, 0, 90))
        card_rgba = card.convert("RGBA")
        card_rgba.alpha_composite(overlay)
        card = card_rgba.convert("RGB")
        draw = ImageDraw.Draw(card)
        _draw_gradient_pill(card, cx - r, cy - r, r * 2, r * 2, GRAD_ACTIVE_THUMB, radius=r)
        draw.polygon([(cx - 14, cy - 24), (cx - 14, cy + 24), (cx + 22, cy)], fill=(255, 255, 255))

    grad_h = 280
    gradient = Image.new("RGBA", (CARD_W, grad_h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(gradient)
    for i in range(grad_h):
        t = i / grad_h
        alpha = int(215 * t)
        r = int(10 + 40 * t)
        g = int(6 + 10 * t)
        b = int(16 + 30 * t)
        gdraw.line([(0, i), (CARD_W, i)], fill=(r, g, b, alpha))
    card_rgba = card.convert("RGBA")
    card_rgba.alpha_composite(gradient, (0, PHOTO_H - grad_h))
    card = card_rgba.convert("RGB")
    draw = ImageDraw.Draw(card)

    if is_verified:
        badge_r = 27
        _draw_check_badge(card, draw, CARD_W - 44 - badge_r * 2, 22, badge_r * 2)

    if tier_text:
        icon_w = 30
        _draw_gradient_chip(card, draw, 20, 20, tier_text, font_chip, GRAD_TIER, icon_w=icon_w)
        _draw_crown(draw, 32, 26, 18, 14, color=(90, 40, 0))

    total_media = len(thumbnails) if thumbnails else 0
    if total_media:
        counter_text = f"{active_index + 1}/{total_media}"
        tw = draw.textlength(counter_text, font=font_small)
        cx0 = CARD_W - tw - 34
        cy0 = PHOTO_H - 42
        draw.rounded_rectangle([cx0 - 10, cy0 - 6, cx0 + tw + 10, cy0 + 30], radius=14, fill=(0, 0, 0))
        draw.text((cx0, cy0), counter_text, font=font_small, fill=TEXT_WHITE)

    name_line = display_name if not age_group else f"{display_name}, {age_group}"
    draw.text((26, PHOTO_H - 94), name_line, font=font_name, fill=TEXT_WHITE)

    if location:
        _draw_pin_icon(draw, 36, PHOTO_H - 30, 14, (255, 130, 190))
        draw.text((50, PHOTO_H - 42), location, font=font_sub, fill=TEXT_MUTED)

    strip_y = PHOTO_H + 10
    thumb_size = FILMSTRIP_H - 20
    x_cursor = 20
    for i, (thumb_bytes, thumb_is_video) in enumerate(thumbnails[:5]):
        try:
            if thumb_bytes:
                t_img = Image.open(io.BytesIO(thumb_bytes))
                t_img = _fit_cover(t_img, thumb_size, thumb_size)
            else:
                t_img = Image.new("RGB", (thumb_size, thumb_size), (52, 40, 60))
        except Exception:
            t_img = Image.new("RGB", (thumb_size, thumb_size), (52, 40, 60))

        if i == active_index:
            ring_pad = 4
            _draw_gradient_ring(
                card, x_cursor - ring_pad, strip_y - ring_pad,
                thumb_size + ring_pad * 2, GRAD_ACTIVE_THUMB, thickness=3
            )

        mask = _rounded_mask((thumb_size, thumb_size), 10)
        card.paste(t_img, (x_cursor, strip_y), mask)
        draw = ImageDraw.Draw(card)

        if i != active_index:
            draw.rounded_rectangle(
                [x_cursor, strip_y, x_cursor + thumb_size, strip_y + thumb_size],
                radius=10, outline=(80, 70, 90), width=1
            )

        if thumb_is_video:
            draw.polygon([
                (x_cursor + thumb_size // 2 - 6, strip_y + thumb_size // 2 - 9),
                (x_cursor + thumb_size // 2 - 6, strip_y + thumb_size // 2 + 9),
                (x_cursor + thumb_size // 2 + 9, strip_y + thumb_size // 2),
            ], fill=(255, 255, 255))
        x_cursor += thumb_size + 12

    panel_y = PHOTO_H + FILMSTRIP_H
    draw.rectangle([0, panel_y, CARD_W, CARD_H], fill=PANEL_COLOR)

    y = panel_y + 18
    x = 24

    x2 = x
    if gender:
        gender_symbol = "\u2642" if gender.lower() == "man" else ("\u2640" if gender.lower() == "woman" else "\u2b21")
        x2 = _draw_gradient_chip(card, draw, x, y, f"{gender_symbol} {gender}", font_chip, GRAD_GENDER)
    if interested_in:
        _draw_gradient_chip(card, draw, x2, y, f"\u2665 Into {interested_in}", font_chip, GRAD_INTERESTED)
    y += 46

    if interests:
        cx = x
        cy = y
        max_x = CARD_W - 24
        for tag in interests[:8]:
            tag_w = draw.textlength(tag, font=font_chip) + 28
            if cx + tag_w > max_x:
                cx = x
                cy += 42
            cx = _draw_gradient_chip(card, draw, cx, cy, tag, font_chip, GRAD_INTEREST_TAG)
        y = cy + 46
    else:
        y += 6

    if dating_intent:
        draw.text((x, y), f"Looking for: {dating_intent}", font=font_section, fill=TEXT_WHITE)
        y += 34

    if bio:
        max_bio_h = CARD_H - y - 16
        line_h = 24
        max_lines = max(1, max_bio_h // line_h)
        lines = _text_wrap(draw, bio, font_small, CARD_W - 48)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            if lines:
                last = lines[-1]
                while draw.textlength(last + "...", font=font_small) > CARD_W - 48 and len(last) > 3:
                    last = last[:-1]
                lines[-1] = last + "..."
        for line in lines:
            draw.text((x, y), line, font=font_small, fill=TEXT_MUTED)
            y += line_h

    content_mask = _rounded_mask((CARD_W, CARD_H), CORNER_RADIUS)
    content = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    content.paste(card, (0, 0), content_mask)

    outer_w, outer_h = CARD_W + BORDER_WIDTH * 2, CARD_H + BORDER_WIDTH * 2
    outer_mask = _rounded_mask((outer_w, outer_h), CORNER_RADIUS + BORDER_WIDTH)
    border_grad = _make_diagonal_gradient(outer_w, outer_h, GRAD_BORDER)
    final = Image.new("RGBA", (outer_w, outer_h), (0, 0, 0, 0))
    final.paste(border_grad, (0, 0), outer_mask)
    final.paste(content, (BORDER_WIDTH, BORDER_WIDTH), content)

    buf = io.BytesIO()
    final.save(buf, format="PNG")
    return buf.getvalue()
