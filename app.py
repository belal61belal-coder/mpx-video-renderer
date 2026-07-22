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
from PIL import Image, ImageDraw, ImageFont, ImageOps, features


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
ASSETS_DIR = DATA_DIR / "assets"
JOBS_DIR = DATA_DIR / "jobs"
API_KEY = os.getenv("RENDERER_API_KEY", "")
LOGO_PATH = Path(os.getenv("LOGO_PATH", "/app/logo.png"))

ASSETS_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

APP_VERSION = "2.1.0"

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
    max_lines: int = 2,
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


def rounded_image(
    image: Image.Image,
    size: tuple[int, int],
    radius: int,
) -> Image.Image:
    fitted = ImageOps.fit(
        image.convert("RGB"),
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    ).convert("RGBA")

    mask = Image.new("L", size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (0, 0, size[0], size[1]),
        radius=radius,
        fill=255,
    )

    fitted.putalpha(mask)
    return fitted


def create_news_frame(
    output_path: Path,
    source_image_path: Path,
    headline: str,
    category: str,
    date_text: str,
    width: int,
    height: int,
) -> None:
    canvas = Image.new(
        "RGBA",
        (width, height),
        (5, 17, 42, 255),
    )

    draw = ImageDraw.Draw(canvas)

    # خلفية زرقاء داكنة خفيفة
    for y in range(height):
        progress = y / max(height - 1, 1)
        r = int(5 + 4 * progress)
        g = int(17 + 11 * progress)
        b = int(42 + 25 * progress)

        draw.line(
            (0, y, width, y),
            fill=(r, g, b, 255),
        )

    regular_font_path = find_font(bold=False)
    bold_font_path = find_font(bold=True)

    date_font = load_font(regular_font_path, 34)
    category_font = load_font(bold_font_path, 38)
    headline_font = load_font(bold_font_path, 58)

    # خطوط الهوية
    draw.rectangle(
        (0, 0, width, 8),
        fill=(230, 183, 40, 255),
    )

    draw.rectangle(
        (0, height - 8, width, height),
        fill=(230, 183, 40, 255),
    )

    # اللوجو أكبر قليلًا
    if LOGO_PATH.exists():
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo.thumbnail(
            (245, 245),
            Image.Resampling.LANCZOS,
        )

        canvas.alpha_composite(
            logo,
            (width - logo.width - 38, 24),
        )

    # التاريخ
    date_value = str(date_text or "").strip()

    if date_value:
        date_bbox = draw.textbbox(
            (0, 0),
            date_value,
            font=date_font,
        )

        date_width = date_bbox[2] - date_bbox[0]
        date_height = date_bbox[3] - date_bbox[1]

        box_x = 48
        box_y = 60
        pad_x = 22
        pad_y = 14

        draw.rounded_rectangle(
            (
                box_x,
                box_y,
                box_x + date_width + pad_x * 2,
                box_y + date_height + pad_y * 2,
            ),
            radius=18,
            fill=(4, 19, 50, 225),
            outline=(230, 183, 40, 180),
            width=2,
        )

        draw.text(
            (
                box_x + pad_x,
                box_y + pad_y - 3,
            ),
            date_value,
            font=date_font,
            fill=(255, 255, 255, 255),
        )

    # صورة الخبر كبيرة
    image_box_x = 30
    image_box_y = 220
    image_box_w = width - 60
    image_box_h = 1030

    source_image = Image.open(source_image_path).convert("RGB")
    article_image = rounded_image(
        source_image,
        (image_box_w, image_box_h),
        radius=36,
    )

    # ظل بسيط للصورة
    shadow = Image.new(
        "RGBA",
        (image_box_w + 24, image_box_h + 24),
        (0, 0, 0, 0),
    )
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (12, 12, image_box_w + 12, image_box_h + 12),
        radius=40,
        fill=(0, 0, 0, 95),
    )

    canvas.alpha_composite(
        shadow,
        (image_box_x - 12, image_box_y - 5),
    )

    canvas.alpha_composite(
        article_image,
        (image_box_x, image_box_y),
    )

    # فاصل ذهبي أسفل الصورة
    draw.rounded_rectangle(
        (
            48,
            image_box_y + image_box_h + 26,
            width - 48,
            image_box_y + image_box_h + 32,
        ),
        radius=3,
        fill=(230, 183, 40, 220),
    )

    # التصنيف أكبر وأوضح
    category_raw = str(category or "الأخبار").strip()
    category_display = prepare_arabic(category_raw)

    category_bbox = text_bbox(
        draw=draw,
        text=category_display,
        font=category_font,
    )

    category_width = category_bbox[2] - category_bbox[0]
    category_height = category_bbox[3] - category_bbox[1]

    category_right = width - 50
    category_top = 1325
    category_pad_x = 30
    category_pad_y = 16

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
        radius=28,
        fill=(13, 91, 190, 245),
        outline=(91, 185, 255, 200),
        width=2,
    )

    draw_rtl_text(
        draw=draw,
        position=(
            category_right - category_pad_x,
            category_top + category_pad_y - 2,
        ),
        text=category_display,
        font=category_font,
        fill=(255, 255, 255, 255),
        anchor="ra",
    )

    # العنوان أعلى قليلًا وبخط أكبر
    headline_lines = wrap_rtl_text(
        text=headline,
        font=headline_font,
        max_width=width - 100,
        draw=draw,
        max_lines=3,
    )

    headline_y = category_bottom + 42
    line_spacing = 18

    for line in headline_lines:
        display_line = prepare_arabic(line)

        draw_rtl_text(
            draw=draw,
            position=(
                width - 50,
                headline_y,
            ),
            text=display_line,
            font=headline_font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 140),
            anchor="ra",
        )

        line_bbox = text_bbox(
            draw=draw,
            text=display_line,
            font=headline_font,
            stroke_width=2,
        )

        headline_y += (
            line_bbox[3]
            - line_bbox[1]
            + line_spacing
        )

    canvas.convert("RGB").save(
        output_path,
        format="JPEG",
        quality=95,
        optimize=True,
    )


def normalize_video(
    source: Path,
    target: Path,
    width: int,
    height: int,
    fps: int,
) -> None:
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            (
                f"scale={width}:{height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"fps={fps},"
                "format=yuv420p"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
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

        for index, image_path in enumerate(image_paths, start=1):
            frame_path = job_dir / f"frame-{index:02d}.jpg"
            clip_path = job_dir / f"clip-{index:02d}.mp4"

            create_news_frame(
                output_path=frame_path,
                source_image_path=image_path,
                headline=headline,
                category=category,
                date_text=date_text,
                width=width,
                height=height,
            )

            run(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-framerate",
                    str(fps),
                    "-i",
                    str(frame_path),
                    "-t",
                    f"{seconds_per_image:.3f}",
                    "-vf",
                    "format=yuv420p",
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

        normalize_video(intro_source, intro, width, height, fps)
        normalize_video(middle, middle_normalized, width, height, fps)
        normalize_video(outro_source, outro, width, height, fps)

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
