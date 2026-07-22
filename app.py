import json, os, re, shutil, subprocess, uuid
from pathlib import Path
from typing import List
import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
ASSETS_DIR = DATA_DIR / "assets"
JOBS_DIR = DATA_DIR / "jobs"
API_KEY = os.getenv("RENDERER_API_KEY", "")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="MPX Video Renderer", version="1.0.0")

def auth(value):
    if not API_KEY:
        raise HTTPException(500, "RENDERER_API_KEY is not configured")
    if value != API_KEY:
        raise HTTPException(401, "Invalid API key")

def run(cmd: List[str]):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr or p.stdout)

def safe_id(value):
    value = re.sub(r"[^a-z0-9_-]+", "-", str(value or uuid.uuid4()).lower()).strip("-")
    return value[:80] or str(uuid.uuid4())

def duration(path: Path):
    p = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1",str(path)],
        capture_output=True, text=True, check=True
    )
    return max(float(p.stdout.strip()), 0.1)

def normalize(src: Path, dst: Path, width: int, height: int, fps: int):
    run([
        "ffmpeg","-y","-i",str(src),
        "-vf",f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},format=yuv420p",
        "-c:v","libx264","-preset","veryfast","-crf","20",
        "-c:a","aac","-ar","48000","-ac","2","-b:a","192k",
        "-movflags","+faststart",str(dst)
    ])

@app.get("/health")
def health():
    return {
        "status":"ok",
        "intro_exists":(ASSETS_DIR/"intro.mp4").exists(),
        "outro_exists":(ASSETS_DIR/"outro.mp4").exists()
    }

@app.post("/assets")
async def upload_assets(
    intro: UploadFile = File(...),
    outro: UploadFile = File(...),
    x_api_key: str | None = Header(default=None)
):
    auth(x_api_key)
    for upload, name in [(intro,"intro.mp4"),(outro,"outro.mp4")]:
        with (ASSETS_DIR/name).open("wb") as f:
            shutil.copyfileobj(upload.file, f)
    return {"status":"ok"}

@app.post("/render")
async def render(
    request: Request,
    payload: str = Form(...),
    audio: UploadFile = File(...),
    x_api_key: str | None = Header(default=None)
):
    auth(x_api_key)
    try:
        cfg = json.loads(payload)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid payload JSON: {e}")

    image_urls = cfg.get("image_urls") or []
    if not image_urls:
        raise HTTPException(400, "image_urls must be a non-empty list")

    intro_src, outro_src = ASSETS_DIR/"intro.mp4", ASSETS_DIR/"outro.mp4"
    if not intro_src.exists() or not outro_src.exists():
        raise HTTPException(400, "Upload intro.mp4 and outro.mp4 first")

    job_id = safe_id(cfg.get("job_id"))
    width, height, fps = int(cfg.get("width",1080)), int(cfg.get("height",1920)), int(cfg.get("fps",30))
    job = JOBS_DIR/job_id
    if job.exists():
        shutil.rmtree(job)
    job.mkdir(parents=True)

    audio_path = job/"narration.mp3"
    with audio_path.open("wb") as f:
        shutil.copyfileobj(audio.file, f)

    try:
        async with httpx.AsyncClient(headers={"User-Agent":"MPX-Renderer/1.0"}) as client:
            images = []
            for i, url in enumerate(image_urls, 1):
                r = await client.get(str(url), follow_redirects=True, timeout=45)
                r.raise_for_status()
                p = job/f"image-{i:02d}.jpg"
                p.write_bytes(r.content)
                images.append(p)

        sec = duration(audio_path) / len(images)
        clips = []
        for i, image in enumerate(images, 1):
            clip = job/f"clip-{i:02d}.mp4"
            run([
                "ffmpeg","-y","-loop","1","-t",f"{sec:.3f}","-i",str(image),
                "-vf",f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},format=yuv420p",
                "-an","-c:v","libx264","-preset","veryfast","-crf","20",str(clip)
            ])
            clips.append(clip)

        (job/"clips.txt").write_text("\n".join(f"file '{p.as_posix()}'" for p in clips), encoding="utf-8")
        run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(job/"clips.txt"),"-c","copy",str(job/"middle-silent.mp4")])
        run([
            "ffmpeg","-y","-i",str(job/"middle-silent.mp4"),"-i",str(audio_path),
            "-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac","-ar","48000","-ac","2","-b:a","192k",
            "-shortest","-movflags","+faststart",str(job/"middle.mp4")
        ])

        intro, middle, outro = job/"intro.mp4", job/"middle-normalized.mp4", job/"outro.mp4"
        normalize(intro_src, intro, width, height, fps)
        normalize(job/"middle.mp4", middle, width, height, fps)
        normalize(outro_src, outro, width, height, fps)

        (job/"final.txt").write_text("\n".join(f"file '{p.as_posix()}'" for p in [intro,middle,outro]), encoding="utf-8")
        final = job/f"{job_id}-final.mp4"
        run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(job/"final.txt"),"-c","copy","-movflags","+faststart",str(final)])
    except Exception as e:
        raise HTTPException(500, str(e))

    return {
        "status":"completed",
        "job_id":job_id,
        "video_url":str(request.base_url).rstrip("/") + f"/files/{job_id}/{final.name}",
        "file_name":final.name,
        "duration_seconds":duration(final)
    }

@app.get("/files/{job_id}/{file_name}")
def get_file(job_id: str, file_name: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    path = JOBS_DIR/safe_id(job_id)/Path(file_name).name
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4", filename=path.name)
