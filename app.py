import json
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path
from typing import List

import arabic_reshaper
import httpx
from bidi.algorithm import get_display
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont, features


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
ASSETS_DIR = DATA_DIR / "assets"
JOBS_DIR = DATA_DIR / "jobs"
API_KEY = os.getenv("RENDERER_API_KEY", "")
LOGO_PATH = Path(os.getenv("LOGO_PATH", "/app/logo.png"))
TEMPLATE_PATH = Path(os.getenv("TEMPLATE_PATH", "/app/base_template.png"))

ASSETS_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

APP_VERSION = "5.2.0"
BUILD_NAME = "PHOTO_FILL_AND_PRO_SUBTITLES_CLEAN"
TRANSITION_SECONDS = 0.35

# Approved coordinates for the new 1080x1920 template.
BASE_WIDTH = 1080
BASE_HEIGHT = 1920
# Visible opening inside the gold frame.
PHOTO_WINDOW_BOX = (45, 435, 1035, 1289)
# Render the photo slightly larger beneath the frame so no dark gaps can appear.
PHOTO_RENDER_BOX = (37, 427, 1043, 1297)
PHOTO_RADIUS = 52
SUBTITLE_PANEL_BOX = (92, 1065, 988, 1238)
DATE_TEXT_BOX = (405, 344, 770, 410)
CATEGORY_TEXT_BOX = (580, 1308, 985, 1368)
HEADLINE_TEXT_BOX = (78, 1418, 1002, 1692)

app = FastAPI(
    title="MPX Video Renderer",
    version=APP_VERSION,
)


def check_api_key(value: str | None) -> None:
    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="RENDERER_API_KEY is not configured",
        )

    if value != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
        )


def run(command: List[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode})\n"
            f"Command: {' '.join(command)}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )


def safe_job_id(raw_value: str | None) -> str:
    value = str(raw_value or uuid.uuid4()).lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value).strip("-")
    return value[:80] or str(uuid.uuid4())


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    return max(float(result.stdout.strip()), 0.1)


def find_font(bold: bool = False) -> str:
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoKufiArabic-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoKufiArabic-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoKufiArabic-Regular.ttf",
        ]

    for candidate in candidates:
        if Path(candidate).exists():
            return candidate

    pattern = "*Arabic*Bold*.ttf" if bold else "*Arabic*Regular*.ttf"

    for base in (
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ):
        if base.exists():
            matches = list(base.rglob(pattern))
            if matches:
                return str(matches[0])

    return (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    if features.check_feature("raqm"):
        return ImageFont.truetype(
            path,
            size,
            layout_engine=ImageFont.Layout.RAQM,
        )

    return ImageFont.truetype(path, size)


def prepare_arabic(text: str) -> str:
    value = str(text or "").strip()

    if not value:
        return ""

    if features.check_feature("raqm"):
        return value

    return get_display(arabic_reshaper.reshape(value))


def text_bbox(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    stroke_width: int = 0,
):
    kwargs = {
        "font": font,
        "stroke_width": stroke_width,
    }

    if features.check_feature("raqm"):
        kwargs.update(
            direction="rtl",
            language="ar",
        )

    return draw.textbbox(
        (0, 0),
        text,
        **kwargs,
    )


def draw_rtl_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    anchor: str = "ra",
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int, int] | None = None,
) -> None:
    kwargs = {
        "font": font,
        "fill": fill,
        "anchor": anchor,
        "stroke_width": stroke_width,
    }

    if stroke_fill is not None:
        kwargs["stroke_fill"] = stroke_fill

    if features.check_feature("raqm"):
        kwargs.update(
            direction="rtl",
            language="ar",
        )

    draw.text(
        position,
        text,
        **kwargs,
    )


def wrap_rtl_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
    max_lines: int = 3,
) -> List[str]:
    words = str(text or "").split()

    if not words:
        return []

    lines: List[str] = []
    current_line: List[str] = []

    for word in words:
        trial_words = current_line + [word]
        trial_raw = " ".join(trial_words)
        trial_display = prepare_arabic(trial_raw)

        bbox = text_bbox(
            draw=draw,
            text=trial_display,
            font=font,
        )

        trial_width = bbox[2] - bbox[0]

        if trial_width <= max_width:
            current_line = trial_words
        else:
            if current_line:
                lines.append(" ".join(current_line))

            current_line = [word]

            if len(lines) >= max_lines:
                break

    if current_line and len(lines) < max_lines:
        lines.append(" ".join(current_line))

    used_words = sum(len(line.split()) for line in lines)

    if used_words < len(words) and lines:
        lines[-1] = lines[-1].rstrip("،,. ") + "..."

    return lines[:max_lines]




def find_latin_font(bold: bool = False) -> str:
    preferred_names = (
        (
            "NotoSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
            "LiberationSans-Bold.ttf",
        )
        if bold
        else (
            "NotoSans-Regular.ttf",
            "DejaVuSans.ttf",
            "LiberationSans-Regular.ttf",
        )
    )

    for base in (
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ):
        if not base.exists():
            continue

        for name in preferred_names:
            matches = list(base.rglob(name))

            if matches:
                return str(matches[0])

    return find_font(bold=bold)


def normalize_date_text(value: str) -> str:
    raw = str(value or "").strip()

    if not raw:
        return ""

    digits = re.sub(r"\D", "", raw)

    if len(digits) == 8:
        day = digits[0:2]
        month = digits[2:4]
        year = digits[4:8]
        return f"{day}/{month}/{year}"

    return raw


def crop_transparent_image(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    bbox = alpha.getbbox()

    if bbox:
        return rgba.crop(bbox)

    return rgba


def choose_headline_font(
    draw: ImageDraw.ImageDraw,
    headline: str,
    bold_font_path: str,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, List[str]]:
    for size in range(70, 49, -2):
        font = load_font(bold_font_path, size)
        lines = wrap_rtl_text(
            text=headline,
            font=font,
            max_width=max_width,
            draw=draw,
            max_lines=3,
        )

        if not lines:
            return font, []

        total_height = 0

        for line in lines:
            display_line = prepare_arabic(line)
            bbox = text_bbox(
                draw=draw,
                text=display_line,
                font=font,
                stroke_width=1,
            )
            total_height += bbox[3] - bbox[1]

        total_height += max(len(lines) - 1, 0) * 24

        if total_height <= max_height:
            return font, lines

    fallback_font = load_font(bold_font_path, 50)
    fallback_lines = wrap_rtl_text(
        text=headline,
        font=fallback_font,
        max_width=max_width,
        draw=draw,
        max_lines=3,
    )

    return fallback_font, fallback_lines




def sanitize_subtitle_text(value: str) -> str:
    """Normalize Arabic subtitle text and remove invisible bidi controls.

    The removed controls are useful in editors but can appear as square glyphs
    in libass or fallback fonts. Pillow renders the cleaned, reshaped text into
    PNG cards, so the result is stable across Docker images.
    """
    cleaned = unicodedata.normalize("NFC", str(value or ""))
    cleaned = re.sub(
        r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]",
        "",
        cleaned,
    )
    cleaned = cleaned.replace("�", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def split_professional_subtitle_chunks(
    narration: str,
    max_words: int = 7,
    min_words: int = 3,
    max_chars: int = 54,
) -> List[str]:
    """Create readable subtitle phrases while respecting punctuation."""
    value = sanitize_subtitle_text(narration)

    if not value:
        return []

    # Keep punctuation attached to its phrase and prefer a break after it.
    words = value.split()
    chunks: List[str] = []
    current: List[str] = []

    for word in words:
        current.append(word)
        current_text = " ".join(current)
        ends_phrase = bool(re.search(r"[،؛:,.!?؟]$", word))

        should_break = (
            len(current) >= max_words
            or len(current_text) >= max_chars
            or (len(current) >= min_words and ends_phrase)
        )

        if should_break:
            chunks.append(current_text)
            current = []

    if current:
        tail = " ".join(current)
        if (
            chunks
            and len(current) < min_words
            and len(chunks[-1].split()) + len(current) <= max_words + 2
            and len(chunks[-1]) + 1 + len(tail) <= max_chars + 12
        ):
            chunks[-1] = chunks[-1].rstrip() + " " + tail
        else:
            chunks.append(tail)

    return [chunk.strip() for chunk in chunks if chunk.strip()]


def detect_audio_pause_points(audio_path: Path, duration: float) -> List[float]:
    """Detect short natural pauses in narration for better cue boundaries."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(audio_path),
            "-af",
            "silencedetect=noise=-38dB:d=0.12",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )

    starts = [
        float(value)
        for value in re.findall(r"silence_start:\s*([0-9.]+)", result.stderr)
    ]
    ends = [
        float(value)
        for value in re.findall(r"silence_end:\s*([0-9.]+)", result.stderr)
    ]

    points: List[float] = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else start
        point = (start + end) / 2.0
        if 0.25 < point < max(duration - 0.25, 0.25):
            points.append(point)

    return sorted(set(points))


def build_subtitle_timeline(
    narration: str,
    duration: float,
    audio_path: Path,
) -> List[tuple[float, float, str]]:
    """Distribute cues by spoken-text weight and snap changes to pauses."""
    chunks = split_professional_subtitle_chunks(narration)

    if not chunks:
        return []

    safe_duration = max(float(duration), 0.1)
    weights: List[float] = []

    for chunk in chunks:
        letters = len(re.sub(r"\s+", "", chunk))
        pause_weight = 0.0
        if re.search(r"[.!?؟]$", chunk):
            pause_weight = 8.0
        elif re.search(r"[،؛,:]$", chunk):
            pause_weight = 4.0
        weights.append(max(float(letters) + pause_weight, 1.0))

    total_weight = sum(weights)
    boundaries = [0.0]
    elapsed = 0.0
    for weight in weights[:-1]:
        elapsed += weight
        boundaries.append(safe_duration * elapsed / total_weight)
    boundaries.append(safe_duration)

    pause_points = detect_audio_pause_points(audio_path, safe_duration)
    snapped = [0.0]

    for target in boundaries[1:-1]:
        previous = snapped[-1]
        candidates = [
            point
            for point in pause_points
            if point >= previous + 0.90 and point <= safe_duration - 0.90
        ]
        if candidates:
            nearest = min(candidates, key=lambda point: abs(point - target))
            if abs(nearest - target) <= 0.80:
                target = nearest
        target = max(target, previous + 0.90)
        snapped.append(min(target, safe_duration - 0.90))

    snapped.append(safe_duration)

    # Repair any compressed final cues while keeping the timeline monotonic.
    for index in range(len(snapped) - 2, 0, -1):
        if snapped[index + 1] - snapped[index] < 0.90:
            snapped[index] = max(snapped[index + 1] - 0.90, snapped[index - 1] + 0.90)

    timeline: List[tuple[float, float, str]] = []
    for index, chunk in enumerate(chunks):
        start = max(snapped[index], 0.0)
        end = min(snapped[index + 1], safe_duration)
        if end > start:
            timeline.append((start, end, chunk))

    return timeline


def create_subtitle_cards(
    job_dir: Path,
    narration: str,
    duration: float,
    audio_path: Path,
    width: int,
    height: int,
) -> List[tuple[Path, float, float]]:
    """Render each Arabic cue as a polished rounded PNG card.

    This avoids the tight per-line black boxes and occasional missing Arabic
    glyphs produced by font fallback inside ASS/libass.
    """
    timeline = build_subtitle_timeline(narration, duration, audio_path)

    if not timeline:
        return []

    scale_x = width / BASE_WIDTH
    scale_y = height / BASE_HEIGHT
    scale = min(scale_x, scale_y)

    panel_left = int(round(SUBTITLE_PANEL_BOX[0] * scale_x))
    panel_top = int(round(SUBTITLE_PANEL_BOX[1] * scale_y))
    panel_right = int(round(SUBTITLE_PANEL_BOX[2] * scale_x))
    panel_bottom = int(round(SUBTITLE_PANEL_BOX[3] * scale_y))
    panel_width = max(panel_right - panel_left, 1)
    panel_height = max(panel_bottom - panel_top, 1)

    bold_font_path = find_font(bold=True)
    cards: List[tuple[Path, float, float]] = []

    for index, (start, end, cue_text) in enumerate(timeline, start=1):
        card = Image.new("RGBA", (panel_width, panel_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)

        radius = max(int(round(24 * scale)), 16)
        border_width = max(int(round(2 * scale)), 2)
        accent_width = max(int(round(4 * scale)), 3)

        draw.rounded_rectangle(
            (1, 1, panel_width - 2, panel_height - 2),
            radius=radius,
            fill=(2, 13, 33, 214),
            outline=(226, 177, 61, 210),
            width=border_width,
        )
        draw.rounded_rectangle(
            (
                int(round(26 * scale)),
                int(round(12 * scale)),
                panel_width - int(round(26 * scale)),
                int(round(12 * scale)) + accent_width,
            ),
            radius=max(accent_width // 2, 1),
            fill=(238, 190, 72, 225),
        )

        max_text_width = panel_width - int(round(72 * scale))
        max_text_height = panel_height - int(round(40 * scale))
        selected_font = None
        selected_lines: List[str] = []

        for base_size in range(50, 37, -2):
            font = load_font(
                bold_font_path,
                max(int(round(base_size * scale)), 36),
            )
            lines = wrap_rtl_text(
                text=sanitize_subtitle_text(cue_text),
                font=font,
                max_width=max_text_width,
                draw=draw,
                max_lines=2,
            )

            line_heights = []
            for line in lines:
                bbox = text_bbox(
                    draw=draw,
                    text=prepare_arabic(line),
                    font=font,
                    stroke_width=1,
                )
                line_heights.append(bbox[3] - bbox[1])

            line_gap = max(int(round(12 * scale)), 8)
            total_height = sum(line_heights) + max(len(lines) - 1, 0) * line_gap

            if lines and total_height <= max_text_height:
                selected_font = font
                selected_lines = lines
                break

        if selected_font is None:
            selected_font = load_font(bold_font_path, max(int(round(38 * scale)), 34))
            selected_lines = wrap_rtl_text(
                text=sanitize_subtitle_text(cue_text),
                font=selected_font,
                max_width=max_text_width,
                draw=draw,
                max_lines=2,
            )

        prepared_lines = [prepare_arabic(line) for line in selected_lines]
        line_gap = max(int(round(12 * scale)), 8)
        heights = []
        for line in prepared_lines:
            bbox = text_bbox(
                draw=draw,
                text=line,
                font=selected_font,
                stroke_width=1,
            )
            heights.append(bbox[3] - bbox[1])

        total_height = sum(heights) + max(len(heights) - 1, 0) * line_gap
        cursor_y = (panel_height - total_height) // 2 - int(round(1 * scale))

        for line, line_height in zip(prepared_lines, heights):
            center_y = cursor_y + line_height // 2
            # Soft shadow plus crisp white text.
            draw.text(
                (panel_width // 2 + 2, center_y + 3),
                line,
                font=selected_font,
                fill=(0, 0, 0, 170),
                anchor="mm",
            )
            draw.text(
                (panel_width // 2, center_y),
                line,
                font=selected_font,
                fill=(255, 255, 255, 255),
                stroke_width=max(int(round(1 * scale)), 1),
                stroke_fill=(4, 10, 24, 230),
                anchor="mm",
            )
            cursor_y += line_height + line_gap

        card_path = job_dir / f"subtitle-card-{index:02d}.png"
        card.save(card_path, format="PNG", optimize=True)
        cards.append((card_path, start, end))

    return cards

def create_layout_assets(
    background_path: Path,
    overlay_path: Path,
    headline: str,
    category: str,
    date_text: str,
    width: int,
    height: int,
) -> None:
    """Build the fixed template and transparent graphics overlay.

    Reference template: the approved 1080x1920 MarketPulseX365 layout.
    Only the inner news-image area is made transparent. The logo, calendar
    icon, gold frame, website and social icons remain part of the template.
    """
    if not TEMPLATE_PATH.exists():
        raise RuntimeError(f"Template file not found: {TEMPLATE_PATH}")

    source_template = Image.open(TEMPLATE_PATH).convert("RGBA")
    template_width, template_height = source_template.size

    if template_width <= 0 or template_height <= 0:
        raise RuntimeError("Invalid template dimensions")

    target_template = source_template.resize(
        (width, height),
        Image.Resampling.LANCZOS,
    )

    # Opaque template background.
    target_template.convert("RGB").save(
        background_path,
        format="JPEG",
        quality=97,
        optimize=True,
    )

    overlay = target_template.copy()
    draw = ImageDraw.Draw(overlay)

    # Coordinates are based on the approved 1080x1920 template.
    scale_x = width / 1080
    scale_y = height / 1920
    scale = min(scale_x, scale_y)

    def sx(value: float) -> int:
        return int(round(value * scale_x))

    def sy(value: float) -> int:
        return int(round(value * scale_y))

    # News image inner area for the approved new template.
    photo_left = sx(PHOTO_WINDOW_BOX[0])
    photo_top = sy(PHOTO_WINDOW_BOX[1])
    photo_right = sx(PHOTO_WINDOW_BOX[2])
    photo_bottom = sy(PHOTO_WINDOW_BOX[3])
    photo_radius = max(int(round(PHOTO_RADIUS * scale)), 1)

    transparent_mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(transparent_mask)
    mask_draw.rounded_rectangle(
        (photo_left, photo_top, photo_right - 1, photo_bottom - 1),
        radius=photo_radius,
        fill=255,
    )

    transparent_layer = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 0),
    )
    overlay.paste(transparent_layer, (0, 0), transparent_mask)

    regular_font_path = find_font(bold=False)
    bold_font_path = find_font(bold=True)
    latin_font_path = find_latin_font(bold=False)

    # -------------------------
    # Date: use a Latin font and LTR rendering so '/' never becomes a box.
    # The calendar icon is already drawn on the left of the template box.
    # -------------------------
    normalized_date = normalize_date_text(date_text)

    if normalized_date:
        date_font = load_font(
            latin_font_path,
            max(int(round(40 * scale)), 27),
        )
        date_center_x = sx((DATE_TEXT_BOX[0] + DATE_TEXT_BOX[2]) / 2)
        date_center_y = sy((DATE_TEXT_BOX[1] + DATE_TEXT_BOX[3]) / 2)

        draw.text(
            (date_center_x, date_center_y),
            normalized_date,
            font=date_font,
            fill=(255, 255, 255, 255),
            anchor="mm",
        )

    # -------------------------
    # Category: one centered dark-navy label inside the gold badge.
    # -------------------------
    category_value = str(category or "الأخبار").strip()
    category_display = prepare_arabic(category_value)

    category_left = sx(CATEGORY_TEXT_BOX[0])
    category_top = sy(CATEGORY_TEXT_BOX[1])
    category_right = sx(CATEGORY_TEXT_BOX[2])
    category_bottom = sy(CATEGORY_TEXT_BOX[3])
    category_max_width = category_right - category_left - sx(20)

    selected_category_font = None

    for base_size in (38, 36, 34, 32, 30, 28, 26):
        candidate_font = load_font(
            bold_font_path,
            max(int(round(base_size * scale)), 24),
        )
        bbox = text_bbox(
            draw=draw,
            text=category_display,
            font=candidate_font,
        )
        if bbox[2] - bbox[0] <= category_max_width:
            selected_category_font = candidate_font
            break

    if selected_category_font is None:
        selected_category_font = load_font(
            bold_font_path,
            max(int(round(28 * scale)), 24),
        )

    draw_rtl_text(
        draw=draw,
        position=(
            (category_left + category_right) // 2,
            (category_top + category_bottom) // 2 - sy(1),
        ),
        text=category_display,
        font=selected_category_font,
        fill=(4, 18, 43, 255),
        anchor="mm",
    )

    # -------------------------
    # Headline: up to three lines, automatically fitted and centered.
    # -------------------------
    headline_left = sx(HEADLINE_TEXT_BOX[0])
    headline_top = sy(HEADLINE_TEXT_BOX[1])
    headline_right = sx(HEADLINE_TEXT_BOX[2])
    headline_bottom = sy(HEADLINE_TEXT_BOX[3])
    headline_max_width = headline_right - headline_left
    headline_max_height = headline_bottom - headline_top

    selected_font = None
    selected_lines: List[str] = []
    selected_gap = 0

    for base_size in (46, 44, 42, 40, 38, 36, 34):
        candidate_font = load_font(
            bold_font_path,
            max(int(round(base_size * scale)), 26),
        )
        candidate_lines = wrap_rtl_text(
            text=headline,
            font=candidate_font,
            max_width=headline_max_width,
            draw=draw,
            max_lines=3,
        )
        line_gap = max(int(round(14 * scale)), 8)
        heights = []

        for line in candidate_lines:
            display_line = prepare_arabic(line)
            bbox = text_bbox(
                draw=draw,
                text=display_line,
                font=candidate_font,
                stroke_width=1,
            )
            heights.append(bbox[3] - bbox[1])

        total_height = sum(heights) + max(len(heights) - 1, 0) * line_gap

        if candidate_lines and total_height <= headline_max_height:
            selected_font = candidate_font
            selected_lines = candidate_lines
            selected_gap = line_gap
            break

    if selected_font is None:
        selected_font = load_font(
            bold_font_path,
            max(int(round(34 * scale)), 26),
        )
        selected_lines = wrap_rtl_text(
            text=headline,
            font=selected_font,
            max_width=headline_max_width,
            draw=draw,
            max_lines=3,
        )
        selected_gap = max(int(round(12 * scale)), 8)

    line_metrics = []
    total_text_height = 0

    for line in selected_lines:
        display_line = prepare_arabic(line)
        bbox = text_bbox(
            draw=draw,
            text=display_line,
            font=selected_font,
            stroke_width=1,
        )
        line_height = bbox[3] - bbox[1]
        line_metrics.append((display_line, line_height))
        total_text_height += line_height

    if line_metrics:
        total_text_height += (len(line_metrics) - 1) * selected_gap

    current_y = headline_top + max(
        (headline_max_height - total_text_height) // 2,
        0,
    )

    headline_center_x = (headline_left + headline_right) // 2

    for display_line, line_height in line_metrics:
        draw_rtl_text(
            draw=draw,
            position=(headline_center_x, current_y + line_height // 2),
            text=display_line,
            font=selected_font,
            fill=(255, 255, 255, 255),
            anchor="mm",
            stroke_width=1,
            stroke_fill=(0, 0, 0, 105),
        )
        current_y += line_height + selected_gap

    overlay.save(overlay_path, format="PNG")

def normalize_video(
    source: Path,
    target: Path,
    width: int,
    height: int,
    fps: int,
    fade_in: bool = False,
    fade_out: bool = False,
) -> None:
    duration = probe_duration(source)
    video_filters = [
        (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=increase"
        ),
        f"crop={width}:{height}",
        f"fps={fps}",
        "format=yuv420p",
    ]

    audio_filters: List[str] = []

    if fade_in:
        video_filters.append(
            f"fade=t=in:st=0:d={TRANSITION_SECONDS}"
        )
        audio_filters.append(
            f"afade=t=in:st=0:d={TRANSITION_SECONDS}"
        )

    if fade_out and duration > TRANSITION_SECONDS:
        fade_start = max(duration - TRANSITION_SECONDS, 0)
        video_filters.append(
            f"fade=t=out:st={fade_start:.3f}:d={TRANSITION_SECONDS}"
        )
        audio_filters.append(
            f"afade=t=out:st={fade_start:.3f}:d={TRANSITION_SECONDS}"
        )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        ",".join(video_filters),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
    ]

    if audio_filters:
        command.extend(
            [
                "-af",
                ",".join(audio_filters),
            ]
        )

    command.extend(
        [
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )

    run(command)


def download_file(
    client: httpx.Client,
    url: str,
    target: Path,
) -> None:
    response = client.get(
        url,
        follow_redirects=True,
        timeout=45,
    )

    response.raise_for_status()
    target.write_bytes(response.content)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "build": BUILD_NAME,
        "raqm_enabled": features.check_feature("raqm"),
        "intro_exists": (ASSETS_DIR / "intro.mp4").exists(),
        "outro_exists": (ASSETS_DIR / "outro.mp4").exists(),
        "logo_exists": LOGO_PATH.exists(),
        "template_exists": TEMPLATE_PATH.exists(),
        "subtitles_engine": "pillow-cards-pause-sync-v2",
        "template_reference": "approved-template-2026-07-photo-fill-v2",
    }


@app.post("/assets")
async def upload_assets(
    intro: UploadFile = File(...),
    outro: UploadFile = File(...),
    x_api_key: str | None = Header(default=None),
) -> dict:
    check_api_key(x_api_key)

    intro_path = ASSETS_DIR / "intro.mp4"
    outro_path = ASSETS_DIR / "outro.mp4"

    with intro_path.open("wb") as file:
        shutil.copyfileobj(intro.file, file)

    with outro_path.open("wb") as file:
        shutil.copyfileobj(outro.file, file)

    return {
        "status": "ok",
        "intro": str(intro_path),
        "outro": str(outro_path),
    }


@app.post("/render")
def render(
    request: Request,
    payload: str = Form(...),
    audio: UploadFile = File(...),
    x_api_key: str | None = Header(default=None),
) -> dict:
    check_api_key(x_api_key)

    try:
        config = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid payload JSON: {exc}",
        ) from exc

    image_urls = config.get("image_urls") or []

    if not isinstance(image_urls, list) or not image_urls:
        raise HTTPException(
            status_code=400,
            detail="image_urls must be a non-empty list",
        )

    intro_source = ASSETS_DIR / "intro.mp4"
    outro_source = ASSETS_DIR / "outro.mp4"

    if not intro_source.exists() or not outro_source.exists():
        raise HTTPException(
            status_code=400,
            detail="Upload intro.mp4 and outro.mp4 first",
        )

    job_id = safe_job_id(config.get("job_id"))
    headline = str(config.get("headline") or "").strip()
    category = str(config.get("category") or "الأخبار").strip()
    date_text = str(config.get("date") or "").strip()
    narration = str(config.get("narration") or "").strip()
    width = int(config.get("width", 1080))
    height = int(config.get("height", 1920))
    fps = int(config.get("fps", 30))

    job_dir = JOBS_DIR / job_id

    if job_dir.exists():
        shutil.rmtree(job_dir)

    job_dir.mkdir(parents=True)

    audio_path = job_dir / "narration.mp3"

    with audio_path.open("wb") as file:
        shutil.copyfileobj(audio.file, file)

    background_path = job_dir / "background.jpg"
    overlay_path = job_dir / "overlay.png"

    create_layout_assets(
        background_path=background_path,
        overlay_path=overlay_path,
        headline=headline,
        category=category,
        date_text=date_text,
        width=width,
        height=height,
    )

    try:
        with httpx.Client(
            headers={
                "User-Agent": f"MPX-Video-Renderer/{APP_VERSION}"
            }
        ) as client:
            image_paths: List[Path] = []

            for index, url in enumerate(image_urls, start=1):
                image_path = job_dir / f"image-{index:02d}.jpg"

                download_file(
                    client,
                    str(url),
                    image_path,
                )

                image_paths.append(image_path)

        audio_duration = probe_duration(audio_path)
        seconds_per_image = audio_duration / len(image_paths)
        clip_paths: List[Path] = []
        subtitle_segments = 0

        image_x = int(round(PHOTO_RENDER_BOX[0] * width / BASE_WIDTH))
        image_y = int(round(PHOTO_RENDER_BOX[1] * height / BASE_HEIGHT))
        image_w = int(round((PHOTO_RENDER_BOX[2] - PHOTO_RENDER_BOX[0]) * width / BASE_WIDTH))
        image_h = int(round((PHOTO_RENDER_BOX[3] - PHOTO_RENDER_BOX[1]) * height / BASE_HEIGHT))
        # H.264 works most reliably with even dimensions.
        image_w = max((image_w // 2) * 2, 2)
        image_h = max((image_h // 2) * 2, 2)

        for index, image_path in enumerate(image_paths, start=1):
            clip_path = job_dir / f"clip-{index:02d}.mp4"

            frames = max(int(seconds_per_image * fps), 1)
            zoom_step = 0.00016

            filter_complex = (
                f"[0:v]"
                f"scale={image_w}:{image_h}:"
                "force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={image_w}:{image_h}:(iw-ow)/2:(ih-oh)/2,"
                "setsar=1,"
                f"zoompan="
                f"z='min(max(pzoom,1.0)+{zoom_step},1.022)':"
                f"x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':"
                f"d={frames}:"
                f"s={image_w}x{image_h}:"
                f"fps={fps}"
                "[photo];"
                "[1:v]"
                f"scale={width}:{height},"
                "format=rgba"
                "[background];"
                "[2:v]"
                f"scale={width}:{height},"
                "format=rgba"
                "[graphics];"
                "[background][photo]"
                f"overlay={image_x}:{image_y}:shortest=1"
                "[base];"
                "[base][graphics]"
                "overlay=0:0:shortest=1,"
                f"fps={fps},"
                "format=yuv420p"
            )

            run(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    str(image_path),
                    "-loop",
                    "1",
                    "-i",
                    str(background_path),
                    "-loop",
                    "1",
                    "-i",
                    str(overlay_path),
                    "-filter_complex",
                    filter_complex,
                    "-t",
                    f"{seconds_per_image:.3f}",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-movflags",
                    "+faststart",
                    str(clip_path),
                ]
            )

            clip_paths.append(clip_path)

        clips_list = job_dir / "clips.txt"

        clips_list.write_text(
            "\n".join(
                f"file '{path.as_posix()}'"
                for path in clip_paths
            ),
            encoding="utf-8",
        )

        middle_silent = job_dir / "middle-silent.mp4"

        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(clips_list),
                "-c",
                "copy",
                str(middle_silent),
            ]
        )

        middle = job_dir / "middle.mp4"

        subtitle_cards = create_subtitle_cards(
            job_dir=job_dir,
            narration=narration,
            duration=audio_duration,
            audio_path=audio_path,
            width=width,
            height=height,
        )
        subtitle_segments = len(subtitle_cards)

        middle_command = [
            "ffmpeg",
            "-y",
            "-i",
            str(middle_silent),
            "-i",
            str(audio_path),
        ]

        if subtitle_cards:
            for card_path, _, _ in subtitle_cards:
                middle_command.extend(
                    [
                        "-loop",
                        "1",
                        "-framerate",
                        str(fps),
                        "-i",
                        str(card_path),
                    ]
                )

            panel_x = int(round(SUBTITLE_PANEL_BOX[0] * width / BASE_WIDTH))
            panel_y = int(round(SUBTITLE_PANEL_BOX[1] * height / BASE_HEIGHT))

            filter_parts: List[str] = ["[0:v]format=rgba[v0]"]
            current_label = "v0"

            for card_index, (_, start, end) in enumerate(subtitle_cards, start=2):
                cue_duration = max(end - start, 0.20)
                fade_duration = min(0.14, cue_duration / 3.0)
                fade_out_start = max(cue_duration - fade_duration, 0.0)
                card_label = f"card{card_index}"
                next_label = f"v{card_index - 1}"

                filter_parts.append(
                    f"[{card_index}:v]format=rgba,"
                    f"trim=duration={cue_duration:.3f},"
                    "setpts=PTS-STARTPTS,"
                    f"fade=t=in:st=0:d={fade_duration:.3f}:alpha=1,"
                    f"fade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f}:alpha=1,"
                    f"setpts=PTS+{start:.3f}/TB"
                    f"[{card_label}]"
                )
                filter_parts.append(
                    f"[{current_label}][{card_label}]"
                    f"overlay={panel_x}:{panel_y}:"
                    "eof_action=pass:shortest=0"
                    f"[{next_label}]"
                )
                current_label = next_label

            filter_parts.append(f"[{current_label}]format=yuv420p[vout]")

            middle_command.extend(
                [
                    "-filter_complex",
                    ";".join(filter_parts),
                    "-map",
                    "[vout]",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                ]
            )
        else:
            middle_command.extend(
                [
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                ]
            )

        middle_command.extend(
            [
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(middle),
            ]
        )

        run(middle_command)

        intro = job_dir / "intro-normalized.mp4"
        middle_normalized = job_dir / "middle-normalized.mp4"
        outro = job_dir / "outro-normalized.mp4"

        normalize_video(
            intro_source,
            intro,
            width,
            height,
            fps,
            fade_in=False,
            fade_out=True,
        )

        normalize_video(
            middle,
            middle_normalized,
            width,
            height,
            fps,
            fade_in=True,
            fade_out=True,
        )

        normalize_video(
            outro_source,
            outro,
            width,
            height,
            fps,
            fade_in=True,
            fade_out=False,
        )

        concat_list = job_dir / "final.txt"

        concat_list.write_text(
            "\n".join(
                f"file '{path.as_posix()}'"
                for path in [
                    intro,
                    middle_normalized,
                    outro,
                ]
            ),
            encoding="utf-8",
        )

        final_path = job_dir / f"{job_id}-final.mp4"

        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(final_path),
            ]
        )

    except (
        httpx.HTTPError,
        RuntimeError,
        subprocess.CalledProcessError,
        OSError,
    ) as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc

    file_url = (
        str(request.base_url).rstrip("/")
        + f"/files/{job_id}/"
        + final_path.name
    )

    return {
        "status": "completed",
        "version": APP_VERSION,
        "build": BUILD_NAME,
        "raqm_enabled": features.check_feature("raqm"),
        "job_id": job_id,
        "video_url": file_url,
        "file_name": final_path.name,
        "duration_seconds": probe_duration(final_path),
        "subtitles_enabled": subtitle_segments > 0,
        "subtitle_segments": subtitle_segments,
    }


@app.get("/files/{job_id}/{file_name}")
def get_file(
    job_id: str,
    file_name: str,
    x_api_key: str | None = Header(default=None),
):
    check_api_key(x_api_key)

    safe_id = safe_job_id(job_id)
    safe_name = Path(file_name).name
    path = JOBS_DIR / safe_id / safe_name

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="File not found",
        )

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=safe_name,
    )
