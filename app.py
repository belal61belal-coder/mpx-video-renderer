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

APP_VERSION = "4.1.0"
SITE_TEXT = "MarketPulseX365.com"
TRANSITION_SECONDS = 0.35

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



def normalize_date_text(value: str) -> str:
    raw = str(value or "").strip()

    if not raw:
        return ""

    digits = re.sub(r"\D", "", raw)

    if len(digits) == 8:
        return f"{digits[0:2]}/{digits[2:4]}/{digits[4:8]}"

    return raw



def split_narration_chunks(
    narration: str,
    max_words: int = 9,
    min_words: int = 4,
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
    words = str(text or "").strip().split()

    if len(words) <= 6:
        return " ".join(words)

    split_at = (len(words) + 1) // 2
    first_line = " ".join(words[:split_at])
    second_line = " ".join(words[split_at:])

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
    image_bottom: int,
) -> int:
    chunks = split_narration_chunks(narration)

    if not chunks:
        output_path.write_text(
            "",
            encoding="utf-8",
        )
        return 0

    total_words = sum(
        max(len(chunk.split()), 1)
        for chunk in chunks
    )

    scale = min(
        width / 1080,
        height / 1920,
    )

    font_size = max(
        int(round(48 * scale)),
        34,
    )

    margin_left = max(
        int(round(92 * width / 1080)),
        50,
    )

    margin_right = margin_left

    # Places the changing subtitle inside the lower part of the news image.
    subtitle_bottom = image_bottom - max(
        int(round(72 * height / 1920)),
        45,
    )

    margin_vertical = max(
        height - subtitle_bottom,
        20,
    )

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
Style: MPXSubtitle,Noto Sans Arabic,{font_size},&H00FFFFFF,&H00FFFFFF,&H00101822,&H78030B18,-1,0,0,0,100,100,0,0,3,10,0,2,{margin_left},{margin_right},{margin_vertical},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: List[str] = []
    elapsed_words = 0
    safe_duration = max(float(duration), 0.1)

    for index, chunk in enumerate(chunks):
        chunk_words = max(len(chunk.split()), 1)
        start_seconds = (
            safe_duration
            * elapsed_words
            / total_words
        )

        elapsed_words += chunk_words

        end_seconds = (
            safe_duration
            * elapsed_words
            / total_words
        )

        if index == len(chunks) - 1:
            end_seconds = safe_duration

        subtitle_text = escape_ass_text(
            wrap_subtitle_text(chunk)
        )

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
    if not TEMPLATE_PATH.exists():
        raise RuntimeError(
            f"Template file not found: {TEMPLATE_PATH}"
        )

    source_template = Image.open(
        TEMPLATE_PATH
    ).convert("RGBA")

    template_width, template_height = source_template.size

    if template_width <= 0 or template_height <= 0:
        raise RuntimeError("Invalid template dimensions")

    target_template = source_template.resize(
        (width, height),
        Image.Resampling.LANCZOS,
    )

    # Full opaque template used as the background.
    target_template.convert("RGB").save(
        background_path,
        format="JPEG",
        quality=96,
        optimize=True,
    )

    # Transparent overlay preserving every fixed design element.
    overlay = target_template.copy()
    draw = ImageDraw.Draw(overlay)

    scale_x = width / template_width
    scale_y = height / template_height
    scale = min(scale_x, scale_y)

    def sx(value: float) -> int:
        return int(round(value * scale_x))

    def sy(value: float) -> int:
        return int(round(value * scale_y))

    # Clear only the inner news-image area.
    # The original gold frame and rounded corners remain untouched.
    photo_left = sx(43)
    photo_top = sy(458)
    photo_right = sx(898)
    photo_bottom = sy(1082)
    photo_radius = max(sx(42), 1)

    transparent_mask = Image.new(
        "L",
        (width, height),
        0,
    )
    mask_draw = ImageDraw.Draw(transparent_mask)
    mask_draw.rounded_rectangle(
        (
            photo_left,
            photo_top,
            photo_right,
            photo_bottom,
        ),
        radius=photo_radius,
        fill=255,
    )

    transparent_layer = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 0),
    )

    overlay.paste(
        transparent_layer,
        (0, 0),
        transparent_mask,
    )

    regular_font_path = find_font(bold=False)
    bold_font_path = find_font(bold=True)

    date_font = load_font(
        regular_font_path,
        max(int(round(33 * scale)), 22),
    )

    category_font = load_font(
        bold_font_path,
        max(int(round(38 * scale)), 26),
    )

    # -------------------------
    # Date
    # -------------------------
    normalized_date = normalize_date_text(date_text)

    if normalized_date:
        date_box_left = sx(115)
        date_box_top = sy(64)
        date_box_right = sx(326)
        date_box_bottom = sy(155)

        date_bbox = draw.textbbox(
            (0, 0),
            normalized_date,
            font=date_font,
        )

        date_width = date_bbox[2] - date_bbox[0]
        date_height = date_bbox[3] - date_bbox[1]

        date_x = (
            date_box_left
            + date_box_right
            - date_width
        ) // 2

        date_y = (
            date_box_top
            + date_box_bottom
            - date_height
        ) // 2 - int(round(3 * scale))

        draw.text(
            (date_x, date_y),
            normalized_date,
            font=date_font,
            fill=(255, 255, 255, 255),
        )

    # -------------------------
    # Category badge
    # -------------------------
    category_value = str(
        category or "الأخبار"
    ).strip()

    category_display = prepare_arabic(
        category_value
    )

    category_box_left = sx(500)
    category_box_top = sy(1125)
    category_box_right = sx(865)
    category_box_bottom = sy(1187)

    category_max_width = (
        category_box_right
        - category_box_left
        - sx(34)
    )

    selected_category_font = None

    for base_size in (
        38,
        36,
        34,
        32,
        30,
        28,
    ):
        candidate_font = load_font(
            bold_font_path,
            max(int(round(base_size * scale)), 24),
        )

        candidate_bbox = text_bbox(
            draw=draw,
            text=category_display,
            font=candidate_font,
        )

        candidate_width = (
            candidate_bbox[2]
            - candidate_bbox[0]
        )

        if candidate_width <= category_max_width:
            selected_category_font = candidate_font
            break

    if selected_category_font is None:
        selected_category_font = load_font(
            bold_font_path,
            max(int(round(28 * scale)), 24),
        )

    category_bbox = text_bbox(
        draw=draw,
        text=category_display,
        font=selected_category_font,
    )

    category_width = (
        category_bbox[2]
        - category_bbox[0]
    )
    category_height = (
        category_bbox[3]
        - category_bbox[1]
    )

    category_center_x = (
        category_box_left
        + category_box_right
    ) // 2

    category_center_y = (
        category_box_top
        + category_box_bottom
    ) // 2

    draw_rtl_text(
        draw=draw,
        position=(
            category_center_x
            + category_width // 2,
            category_center_y
            - category_height // 2
            - int(round(2 * scale)),
        ),
        text=category_display,
        font=selected_category_font,
        fill=(4, 20, 49, 255),
        anchor="ra",
    )

    # -------------------------
    # Headline
    # -------------------------
    headline_right = sx(853)
    headline_left = sx(75)
    headline_top = sy(1218)
    headline_bottom = sy(1443)
    headline_max_width = (
        headline_right
        - headline_left
    )

    selected_font = None
    selected_lines: List[str] = []

    for base_size in (
        58,
        55,
        52,
        49,
        46,
        43,
        40,
    ):
        candidate_font = load_font(
            bold_font_path,
            max(
                int(round(base_size * scale)),
                28,
            ),
        )

        candidate_lines = wrap_rtl_text(
            text=headline,
            font=candidate_font,
            max_width=headline_max_width,
            draw=draw,
            max_lines=3,
        )

        total_height = 0
        line_gap = max(
            int(round(14 * scale)),
            8,
        )

        for line in candidate_lines:
            display_line = prepare_arabic(line)
            bbox = text_bbox(
                draw=draw,
                text=display_line,
                font=candidate_font,
                stroke_width=1,
            )
            total_height += bbox[3] - bbox[1]

        if candidate_lines:
            total_height += (
                len(candidate_lines) - 1
            ) * line_gap

        if total_height <= (
            headline_bottom
            - headline_top
        ):
            selected_font = candidate_font
            selected_lines = candidate_lines
            break

    if selected_font is None:
        selected_font = load_font(
            bold_font_path,
            max(int(round(40 * scale)), 28),
        )
        selected_lines = wrap_rtl_text(
            text=headline,
            font=selected_font,
            max_width=headline_max_width,
            draw=draw,
            max_lines=3,
        )

    line_gap = max(
        int(round(14 * scale)),
        8,
    )

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
        line_metrics.append(
            (display_line, line_height)
        )
        total_text_height += line_height

    if line_metrics:
        total_text_height += (
            len(line_metrics) - 1
        ) * line_gap

    current_y = (
        headline_top
        + (
            headline_bottom
            - headline_top
            - total_text_height
        ) // 2
    )

    for display_line, line_height in line_metrics:
        draw_rtl_text(
            draw=draw,
            position=(
                headline_right,
                current_y,
            ),
            text=display_line,
            font=selected_font,
            fill=(255, 255, 255, 255),
            anchor="ra",
            stroke_width=1,
            stroke_fill=(0, 0, 0, 115),
        )

        current_y += line_height + line_gap

    overlay.save(
        overlay_path,
        format="PNG",
    )

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
        "20",
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

        image_x = int(round(43 * width / 941))
        image_y = int(round(458 * height / 1672))
        image_w = int(round((898 - 43) * width / 941))
        image_h = int(round((1082 - 458) * height / 1672))

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
                    "20",
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
                    "20",
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
