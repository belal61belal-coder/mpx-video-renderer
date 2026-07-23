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

ASSETS_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

APP_VERSION = "3.0.2"
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


def create_layout_assets(
    background_path: Path,
    overlay_path: Path,
    headline: str,
    category: str,
    date_text: str,
    width: int,
    height: int,
) -> None:
    # Layout constants for a 1080 x 1920 vertical video.
    scale_x = width / 1080
    scale_y = height / 1920

    def sx(value: int) -> int:
        return int(round(value * scale_x))

    def sy(value: int) -> int:
        return int(round(value * scale_y))

    photo_x = sx(40)
    photo_y = sy(180)
    photo_w = width - sx(80)
    photo_h = sy(640)

    card_x = sx(40)
    card_y = sy(850)
    card_w = width - sx(80)
    card_h = sy(900)

    # Opaque base layer.
    background = Image.new(
        "RGBA",
        (width, height),
        (4, 13, 34, 255),
    )
    bg_draw = ImageDraw.Draw(background)

    # Restrained navy gradient.
    for y in range(height):
        progress = y / max(height - 1, 1)
        r = int(4 + 3 * progress)
        g = int(13 + 9 * progress)
        b = int(34 + 20 * progress)

        bg_draw.line(
            (0, y, width, y),
            fill=(r, g, b, 255),
        )

    # Header panel.
    bg_draw.rounded_rectangle(
        (
            sx(28),
            sy(24),
            width - sx(28),
            sy(150),
        ),
        radius=sx(24),
        fill=(3, 12, 31, 248),
        outline=(255, 255, 255, 18),
        width=max(sx(1), 1),
    )

    # Photo shadow and backing.
    bg_draw.rounded_rectangle(
        (
            photo_x - sx(8),
            photo_y + sy(8),
            photo_x + photo_w + sx(8),
            photo_y + photo_h + sy(18),
        ),
        radius=sx(24),
        fill=(0, 0, 0, 92),
    )

    bg_draw.rounded_rectangle(
        (
            photo_x - sx(3),
            photo_y - sy(3),
            photo_x + photo_w + sx(3),
            photo_y + photo_h + sy(3),
        ),
        radius=sx(20),
        fill=(216, 173, 40, 255),
    )

    # Headline card.
    bg_draw.rounded_rectangle(
        (
            card_x,
            card_y,
            card_x + card_w,
            card_y + card_h,
        ),
        radius=sx(34),
        fill=(4, 16, 38, 255),
        outline=(255, 255, 255, 20),
        width=max(sx(1), 1),
    )

    # Footer separator.
    bg_draw.line(
        (
            sx(54),
            sy(1810),
            width - sx(54),
            sy(1810),
        ),
        fill=(213, 171, 39, 135),
        width=max(sy(2), 1),
    )

    background.convert("RGB").save(
        background_path,
        format="JPEG",
        quality=95,
        optimize=True,
    )

    # Transparent graphics layer.
    overlay = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(overlay)

    regular_font_path = find_font(bold=False)
    bold_font_path = find_font(bold=True)
    latin_regular_path = find_latin_font(bold=False)
    latin_bold_path = find_latin_font(bold=True)

    date_font = ImageFont.truetype(
        latin_bold_path,
        sx(34),
    )
    category_font = load_font(
        bold_font_path,
        sx(38),
    )
    site_font = ImageFont.truetype(
        latin_regular_path,
        sx(32),
    )
    # Premium gold top edge.
    draw.rectangle(
        (0, 0, width, max(sy(7), 1)),
        fill=(216, 173, 40, 255),
    )

    # Date uses a Latin font, so slashes always render correctly.
    date_value = normalize_date_text(date_text)

    if date_value:
        draw.text(
            (sx(58), sy(87)),
            date_value,
            font=date_font,
            fill=(255, 255, 255, 255),
            anchor="lm",
        )

        draw.rounded_rectangle(
            (
                sx(54),
                sy(118),
                sx(255),
                sy(122),
            ),
            radius=sx(2),
            fill=(216, 173, 40, 225),
        )

    # Crop transparent padding from the logo before sizing it.
    if LOGO_PATH.exists():
        logo = crop_transparent_image(
            Image.open(LOGO_PATH)
        )
        logo.thumbnail(
            (sx(150), sy(118)),
            Image.Resampling.LANCZOS,
        )

        logo_x = width - logo.width - sx(50)
        logo_y = sy(37)

        overlay.alpha_composite(
            logo,
            (logo_x, logo_y),
        )

    # Fine photo border.
    draw.rounded_rectangle(
        (
            photo_x,
            photo_y,
            photo_x + photo_w,
            photo_y + photo_h,
        ),
        radius=sx(18),
        outline=(216, 173, 40, 220),
        width=max(sx(2), 1),
    )

    # Category with a minimal professional treatment.
    category_raw = str(category or "الأخبار").strip()
    category_display = prepare_arabic(category_raw)

    category_bbox = text_bbox(
        draw=draw,
        text=category_display,
        font=category_font,
    )

    category_width = category_bbox[2] - category_bbox[0]
    category_height = category_bbox[3] - category_bbox[1]

    category_right = card_x + card_w - sx(42)
    category_top = card_y + sy(62)
    category_pad_x = sx(26)
    category_pad_y = sy(13)

    category_left = (
        category_right
        - category_width
        - category_pad_x * 2
    )
    category_bottom = (
        category_top
        + category_height
        + category_pad_y * 2
    )

    draw.rounded_rectangle(
        (
            category_left,
            category_top,
            category_right,
            category_bottom,
        ),
        radius=sx(20),
        fill=(12, 73, 151, 248),
        outline=(96, 185, 244, 185),
        width=max(sx(2), 1),
    )

    draw_rtl_text(
        draw=draw,
        position=(
            category_right - category_pad_x,
            category_top + category_pad_y - sy(2),
        ),
        text=category_display,
        font=category_font,
        fill=(255, 255, 255, 255),
        anchor="ra",
    )

    # Gold accent beside the title, aligned for RTL reading.
    accent_x = card_x + card_w - sx(42)
    title_top = category_bottom + sy(54)

    draw.rounded_rectangle(
        (
            accent_x - sx(7),
            title_top,
            accent_x,
            card_y + card_h - sy(170),
        ),
        radius=sx(4),
        fill=(216, 173, 40, 245),
    )

    max_title_width = card_w - sx(120)
    max_title_height = sy(420)

    headline_font, headline_lines = choose_headline_font(
        draw=draw,
        headline=headline,
        bold_font_path=bold_font_path,
        max_width=max_title_width,
        max_height=max_title_height,
    )

    line_spacing = sy(24)
    line_heights: List[int] = []

    for line in headline_lines:
        display_line = prepare_arabic(line)
        bbox = text_bbox(
            draw=draw,
            text=display_line,
            font=headline_font,
            stroke_width=1,
        )
        line_heights.append(bbox[3] - bbox[1])

    title_block_height = (
        sum(line_heights)
        + max(len(line_heights) - 1, 0) * line_spacing
    )

    title_area_top = title_top
    title_area_bottom = card_y + card_h - sy(190)
    title_area_height = title_area_bottom - title_area_top

    headline_y = (
        title_area_top
        + max((title_area_height - title_block_height) // 2, 0)
    )

    for line, line_height in zip(
        headline_lines,
        line_heights,
    ):
        display_line = prepare_arabic(line)

        # Soft shadow.
        draw_rtl_text(
            draw=draw,
            position=(
                accent_x - sx(30) + sx(2),
                headline_y + sy(3),
            ),
            text=display_line,
            font=headline_font,
            fill=(0, 0, 0, 115),
            anchor="ra",
        )

        draw_rtl_text(
            draw=draw,
            position=(
                accent_x - sx(30),
                headline_y,
            ),
            text=display_line,
            font=headline_font,
            fill=(255, 255, 255, 255),
            anchor="ra",
        )

        headline_y += line_height + line_spacing

    # Website inside the card, visible but not dominant.
    site_text = SITE_TEXT
    site_bbox = draw.textbbox(
        (0, 0),
        site_text,
        font=site_font,
    )
    site_width = site_bbox[2] - site_bbox[0]

    draw.text(
        (
            card_x + (card_w - site_width) // 2,
            card_y + card_h - sy(92),
        ),
        site_text,
        font=site_font,
        fill=(190, 202, 219, 230),
    )

    draw.rectangle(
        (
            0,
            height - max(sy(7), 1),
            width,
            height,
        ),
        fill=(216, 173, 40, 255),
    )

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

        image_x = int(round(40 * width / 1080))
        image_y = int(round(180 * height / 1920))
        image_w = width - int(round(80 * width / 1080))
        image_h = int(round(640 * height / 1920))

        for index, image_path in enumerate(image_paths, start=1):
            clip_path = job_dir / f"clip-{index:02d}.mp4"

            frames = max(int(seconds_per_image * fps), 1)
            zoom_step = 0.00022

            filter_complex = (
                f"[0:v]"
                f"scale={image_w}:{image_h}:"
                "force_original_aspect_ratio=increase,"
                f"crop={image_w}:{image_h},"
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

        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(middle_silent),
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
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
