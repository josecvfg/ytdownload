import asyncio
import re
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import yt_dlp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
TEMP_DIR = BASE_DIR / "temp"
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = asyncio.create_task(worker_loop())
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="YT Subs Burner", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class JobRequest(BaseModel):
    url: str


class Job:
    def __init__(self, job_id: str, url: str):
        self.id = job_id
        self.url = url
        self.status = "queued"  # queued | downloading | burning | done | error
        self.progress = 0
        self.message = "En cola"
        self.title: Optional[str] = None
        self.output_file: Optional[str] = None
        self.error: Optional[str] = None

    def serialize(self):
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "title": self.title,
            "error": self.error,
            "download_ready": self.status == "done",
        }


jobs: Dict[str, Job] = {}
job_queue: asyncio.Queue = asyncio.Queue()


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name or "video")
    return name.strip()[:100] or "video"


def pick_subtitle_lang(info: dict):
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    if "es" in subs:
        return "es", False
    if "es" in auto:
        return "es", True
    for code in subs:
        if code.startswith("es"):
            return code, False
    for code in auto:
        if code.startswith("es"):
            return code, True
    return None, None


def escape_ffmpeg_path(path: str) -> str:
    # Escapes needed so the path can live inside the subtitles= filter argument
    path = path.replace("\\", "/")
    path = path.replace(":", "\\:")
    path = path.replace("'", "\\'")
    return path


TS_RE = re.compile(
    r"(\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->\s*(\d{2}:)?\d{2}:\d{2}[.,]\d{3}"
)
CUE_TIME_RE = re.compile(
    r"((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")


def _normalize_ts(ts: str) -> str:
    ts = ts.replace(".", ",")
    if ts.count(":") == 1:
        ts = "00:" + ts
    return ts


def vtt_to_clean_srt(vtt_path: Path) -> Path:
    """Convierte un .vtt a .srt con SOLO texto plano: sin cue settings
    (align/position/line), sin tags <c>/<i>, sin timestamps por palabra.
    Así el force_style de ffmpeg controla el 100% del posicionamiento."""
    raw = vtt_path.read_text(encoding="utf-8", errors="ignore")
    lines = raw.splitlines()

    entries = []
    current_times = None
    current_text = []

    def flush():
        nonlocal current_times, current_text
        if current_times and current_text:
            entries.append((current_times, current_text[:]))
        current_times = None
        current_text = []

    for line in lines:
        line = line.rstrip("\ufeff\r\n")
        m = CUE_TIME_RE.search(line)
        if m:
            flush()
            current_times = (_normalize_ts(m.group(1)), _normalize_ts(m.group(2)))
            continue
        stripped = line.strip()
        if stripped == "":
            flush()
            continue
        upper = stripped.upper()
        if upper.startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "KIND:", "LANGUAGE:")):
            continue
        if current_times is None:
            # cue identifier line before the timestamp, ignore
            continue
        clean = TAG_RE.sub("", stripped)
        if clean:
            current_text.append(clean)
    flush()

    # Quita duplicados consecutivos idénticos (comunes en autogenerados)
    srt_lines = []
    idx = 1
    prev_text = None
    for times, text_lines in entries:
        text_joined = " ".join(text_lines)
        if text_joined == prev_text:
            continue
        prev_text = text_joined
        srt_lines.append(str(idx))
        srt_lines.append(f"{times[0]} --> {times[1]}")
        # {\an5} = tag de alineación numpad (centro puro), va DENTRO del
        # texto. Esto es lo único que garantiza el centrado sin importar
        # si ffmpeg interpreta el campo "Alignment" del force_style con
        # la numeración moderna o la numeración vieja de SSA (donde el
        # 5 significa arriba-izquierda en vez de centro).
        first_line = text_lines[0]
        text_lines_tagged = [f"{{\\an5}}{first_line}"] + text_lines[1:]
        srt_lines.extend(text_lines_tagged)
        srt_lines.append("")
        idx += 1

    srt_path = vtt_path.with_suffix(".srt")
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    return srt_path

COOKIES_FILE = BASE_DIR / "cookies.txt"

def base_ydl_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True}
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def run_download(job: Job):
    job.status = "downloading"
    job.message = "Obteniendo información del video"

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(job.url, download=False)

    job.title = sanitize_filename(info.get("title", "video"))
    duration = info.get("duration") or 0
    lang, is_auto = pick_subtitle_lang(info)

    outtmpl = str(TEMP_DIR / f"{job.id}.%(ext)s")

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                pct = downloaded / total * 100
                job.progress = int(pct * 0.5)  # 0-50%
                job.message = f"Descargando video ({int(pct)}%)"
        elif d["status"] == "finished":
            job.progress = 50
            job.message = "Descarga completa, preparando subtítulos"

    ydl_opts = {
        **base_ydl_opts(),
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }

    if lang:
        ydl_opts["writesubtitles"] = not is_auto
        ydl_opts["writeautomaticsub"] = is_auto
        ydl_opts["subtitleslangs"] = [lang]
        ydl_opts["subtitlesformat"] = "vtt"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([job.url])

    video_file = None
    sub_file = None
    for f in TEMP_DIR.glob(f"{job.id}.*"):
        if f.suffix.lower() in (".mp4", ".mkv", ".webm"):
            video_file = f
        elif f.suffix.lower() == ".vtt":
            # Convertimos nosotros mismos a srt limpio (sin cue settings)
            sub_file = vtt_to_clean_srt(f)

    if not video_file:
        raise RuntimeError("No se pudo descargar el video")

    return video_file, sub_file, duration


def run_burn(job: Job, video_file: Path, sub_file: Optional[Path], duration: float):
    job.status = "burning"
    job.message = "Quemando subtítulos" if sub_file else "Procesando video (sin subtítulos disponibles)"

    final_name = f"{job.id}_{job.title}.mp4"
    output_path = DOWNLOAD_DIR / final_name

    # Estilo: caja semitransparente detrás del texto (BorderStyle=3 + BackColour).
    # El centrado real lo pone el tag {\an5} dentro del texto (ver
    # vtt_to_clean_srt), así que aquí ya no dependemos del campo Alignment.
    # BackColour alpha: &H00 = opaco, &HFF = invisible. &HD8 ~ 85% transparente.
    style = (
        "FontName=Arial Black,Bold=0,FontSize=32,Spacing=0.3,"
        "PrimaryColour=&H00FFFFFF,"  # Opaque White text
        "BackColour=&H80000000,"  # 50% transparent black box (&H80 alpha)
        "BorderStyle=4,"  # 4 = Opaque background box mode
        "Outline=0,"  # Removes outline
        "Shadow=0"  # Disables drop shadow
    )

    vf_filters = []
    if sub_file:
        sub_path_escaped = escape_ffmpeg_path(str(sub_file))
        vf_filters.append(f"subtitles='{sub_path_escaped}':force_style='{style}'")
    vf = ",".join(vf_filters)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_file),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k",
        "-progress", "pipe:1", "-nostats",
        str(output_path),
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in process.stdout:
        line = line.strip()
        if line.startswith("out_time_ms=") and duration:
            try:
                out_ms = int(line.split("=")[1])
                frac = min(out_ms / 1_000_000 / duration, 1.0)
                job.progress = 50 + int(frac * 50)
                job.message = f"Quemando subtítulos ({job.progress}%)"
            except (ValueError, ZeroDivisionError):
                pass
        elif line.startswith("progress=") and line.endswith("end"):
            job.progress = 99

    process.wait()
    if process.returncode != 0:
        raise RuntimeError("ffmpeg falló al procesar el video")

    return output_path


def cleanup_temp(job_id: str):
    for f in TEMP_DIR.glob(f"{job_id}.*"):
        try:
            f.unlink()
        except OSError:
            pass


def process_job(job: Job):
    try:
        video_file, sub_file, duration = run_download(job)
        output_path = run_burn(job, video_file, sub_file, duration)
        job.output_file = str(output_path)
        job.progress = 100
        job.status = "done"
        job.message = "Listo para descargar"
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = str(exc)
        job.message = "Error"
    finally:
        cleanup_temp(job.id)


async def worker_loop():
    loop = asyncio.get_event_loop()
    while True:
        job_id = await job_queue.get()
        job = jobs.get(job_id)
        if job:
            await loop.run_in_executor(None, process_job, job)
        job_queue.task_done()


@app.post("/api/jobs")
async def create_job(req: JobRequest):
    job_id = uuid.uuid4().hex[:8]
    job = Job(job_id, req.url.strip())
    jobs[job_id] = job
    await job_queue.put(job_id)
    return job.serialize()


@app.get("/api/jobs")
async def list_jobs():
    return [j.serialize() for j in jobs.values()]


@app.get("/api/jobs/{job_id}/download")
async def download_job_file(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.output_file:
        return {"error": "not ready"}
    filename = f"{job.title}.mp4"
    return FileResponse(job.output_file, filename=filename, media_type="video/mp4")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
