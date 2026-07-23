import json
import os
import re
import shutil
import subprocess
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

APP_VERSION = "5.0.0"
SITE_TEXT = "MarketPulseX365.com"
TRANSITION_SECONDS = 0.35

# Approved coordinates for the new 1080x1920 template.
BASE_WIDTH = 1080
BASE_HEIGHT = 1920
PHOTO_BOX = (46, 436, 1030, 1282)
PHOTO_RADIUS = 52
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




def split_narration_chunks(
    narration: str,
    max_words: int = 6,
    min_words: int = 2,
) -> List[str]:
    value = re.sub(
        r"\s+",
        " ",
        str(narration or "").strip(),
    )

    if not value:
        return []

    sentence_parts = re.split(
        r"(?<=[.!؟!؛])\s+|\n+",
        value,
    )

    chunks: List[str] = []

    for sentence in sentence_parts:
        words = sentence.strip().split()

        if not words:
            continue

        while len(words) > max_words:
            chunks.append(" ".join(words[:max_words]))
            words = words[max_words:]

        if words:
            current = " ".join(words)

            if (
                chunks
                and len(words) < min_words
                and (
                    len(chunks[-1].split())
                    + len(words)
                    <= max_words + 2
                )
            ):
                chunks[-1] = (
                    chunks[-1].rstrip()
                    + " "
                    + current
                )
            else:
                chunks.append(current)

    return chunks


def wrap_subtitle_text(text: str) -> str:
    """Split one subtitle cue into at most two visually balanced lines."""
    words = str(text or "").strip().split()

    if len(words) <= 3:
        return " ".join(words)

    best_index = max(1, len(words) // 2)
    best_difference = None

    for index in range(1, len(words)):
        first = " ".join(words[:index])
        second = " ".join(words[index:])
        difference = abs(len(first) - len(second))

        if best_difference is None or difference < best_difference:
            best_difference = difference
            best_index = index

    first_line = " ".join(words[:best_index])
    second_line = " ".join(words[best_index:])

    return first_line + "\n" + second_line


def escape_ass_text(text: str) -> str:
    value = str(text or "")
    value = value.replace("\\", r"\\")
    value = value.replace("{", "(")
    value = value.replace("}", ")")
    value = value.replace("\r", " ")
    value = value.replace("\n", r"\N")
    return value


def ass_timestamp(seconds: float) -> str:
    total_centiseconds = max(
        int(round(float(seconds) * 100)),
        0,
    )

    hours = total_centiseconds // 360000
    remainder = total_centiseconds % 360000
    minutes = remainder // 6000
    remainder %= 6000
    secs = remainder // 100
    centiseconds = remainder % 100

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{secs:02d}."
        f"{centiseconds:02d}"
    )


def create_ass_subtitles(
    output_path: Path,
    narration: str,
    duration: float,
    width: int,
    height: int,
    image_left: int,
    image_right: int,
    image_bottom: int,
) -> int:
    """Create readable Arabic subtitles inside the lower part of the news image.

    The timing is distributed by word count. This is intentionally deterministic
    and requires no external transcription service.
    """
    chunks = split_narration_chunks(narration)

    if not chunks:
        output_path.write_text("", encoding="utf-8")
        return 0

    total_words = sum(max(len(chunk.split()), 1) for chunk in chunks)
    scale = min(width / 1080, height / 1920)

    font_size = max(int(round(54 * scale)), 42)

    # Constrain subtitles to the safe inner width of the news image.
    inner_padding = max(int(round(72 * scale)), 48)
    margin_left = max(image_left + inner_padding, 20)
    margin_right = max(width - image_right + inner_padding, 20)

    # Keep the subtitle clearly above the lower gold frame.
    subtitle_bottom = image_bottom - max(
        int(round(62 * scale)),
        42,
    )
    margin_vertical = max(height - subtitle_bottom, 20)

    header = f"""[Script Info]
Title: MarketPulseX365 synchronized narration
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: MPXSubtitle,Noto Sans Arabic,{font_size},&H00FFFFFF,&H00FFFFFF,&H00180B03,&H98180B03,-1,0,0,0,100,100,0,0,3,5,0,2,{margin_left},{margin_right},{margin_vertical},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: List[str] = []
    elapsed_words = 0
    safe_duration = max(float(duration), 0.1)

    for index, chunk in enumerate(chunks):
        chunk_words = max(len(chunk.split()), 1)
        start_seconds = safe_duration * elapsed_words / total_words
        elapsed_words += chunk_words
        end_seconds = safe_duration * elapsed_words / total_words

        if index == len(chunks) - 1:
            end_seconds = safe_duration

        # Prevent ultra-short flashes.
        if end_seconds - start_seconds < 0.55:
            end_seconds = min(start_seconds + 0.55, safe_duration)

        subtitle_text = escape_ass_text(wrap_subtitle_text(chunk))

        events.append(
            "Dialogue: 0,"
            f"{ass_timestamp(start_seconds)},"
            f"{ass_timestamp(end_seconds)},"
            "MPXSubtitle,,0,0,0,,"
            f"{subtitle_text}"
        )

    output_path.write_text(
        header + "\n".join(events) + "\n",
        encoding="utf-8",
    )

    return len(events)

def escape_ffmpeg_filter_path(path: Path) -> str:
    value = str(path)
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    return value

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
    photo_left = sx(PHOTO_BOX[0])
    photo_top = sy(PHOTO_BOX[1])
    photo_right = sx(PHOTO_BOX[2])
    photo_bottom = sy(PHOTO_BOX[3])
    photo_radius = max(int(round(PHOTO_RADIUS * scale)), 1)

    transparent_mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(transparent_mask)
    mask_draw.rounded_rectangle(
        (photo_left, photo_top, photo_right, photo_bottom),
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
        "raqm_enabled": features.check_feature("raqm"),
        "intro_exists": (ASSETS_DIR / "intro.mp4").exists(),
        "outro_exists": (ASSETS_DIR / "outro.mp4").exists(),
        "logo_exists": LOGO_PATH.exists(),
        "template_exists": TEMPLATE_PATH.exists(),
        "subtitles_engine": "libass",
        "template_reference": "approved-template-2026-07-final-v5",
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

        image_x = int(round(PHOTO_BOX[0] * width / BASE_WIDTH))
        image_y = int(round(PHOTO_BOX[1] * height / BASE_HEIGHT))
        image_w = int(round((PHOTO_BOX[2] - PHOTO_BOX[0]) * width / BASE_WIDTH))
        image_h = int(round((PHOTO_BOX[3] - PHOTO_BOX[1]) * height / BASE_HEIGHT))

        for index, image_path in enumerate(image_paths, start=1):
            clip_path = job_dir / f"clip-{index:02d}.mp4"

            frames = max(int(seconds_per_image * fps), 1)
            zoom_step = 0.00018

            filter_complex = (
                f"[0:v]"
                f"scale={image_w}:{image_h}:"
                "force_original_aspect_ratio=increase,"
                f"crop={image_w}:{image_h},"
                f"zoompan="
                f"z='min(max(pzoom,1.0)+{zoom_step},1.018)':"
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
        subtitles_path = job_dir / "subtitles.ass"

        subtitle_segments = create_ass_subtitles(
            output_path=subtitles_path,
            narration=narration,
            duration=audio_duration,
            width=width,
            height=height,
            image_left=image_x,
            image_right=image_x + image_w,
            image_bottom=image_y + image_h,
        )

        middle_command = [
            "ffmpeg",
            "-y",
            "-i",
            str(middle_silent),
            "-i",
            str(audio_path),
        ]

        if subtitle_segments > 0:
            escaped_subtitles_path = (
                escape_ffmpeg_filter_path(
                    subtitles_path
                )
            )

            subtitle_filter = (
                "subtitles="
                f"filename='{escaped_subtitles_path}':"
                "fontsdir='/usr/share/fonts'"
            )

            middle_command.extend(
                [
                    "-vf",
                    subtitle_filter,
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
                    "-c:v",
                    "copy",
                ]
            )

        middle_command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
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
