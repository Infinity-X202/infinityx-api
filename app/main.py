import asyncio
import json
import logging
import mimetypes
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .engine import Manager, analyze_url, search_public
from .errors import AppError
from .media import ffmpeg_available
from .platforms import all_platforms
from .security import validate_template
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


class AnalyzeIn(BaseModel):
    url: str
    adult_confirmed: bool = True


class SearchIn(BaseModel):
    query: str
    source: str = "pornhub"


class DownloadIn(BaseModel):
    url: str
    format_id: str
    media_type: str
    convert_to: str | None = None
    filename_template: str | None = None
    adult_confirmed: bool = True


class SettingsIn(BaseModel):
    max_concurrent: int = Field(ge=1, le=3)
    max_bytes: int = Field(ge=1_000_000, le=4_000_000_000)
    filename_template: str
    notify: bool
    max_minutes: int = Field(ge=1, le=180)


class ExtractIn(BaseModel):
    target: str


class RenameIn(BaseModel):
    filename: str


def public_job(row: dict | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    raw_meta = data.get("metadata")
    if isinstance(raw_meta, str) and raw_meta:
        try:
            data["metadata"] = json.loads(raw_meta)
        except json.JSONDecodeError:
            data["metadata"] = None
    data.pop("filepath", None)
    data["progress"] = round(float(data.get("progress") or 0), 1)
    return data


def _writable_data_dir() -> Path:
    env_dir = os.environ.get("DATA_DIR")
    candidates = [Path(env_dir)] if env_dir else [ROOT / "data", Path("/tmp/infinityx-data")]
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError as exc:
            last_error = exc
    raise OSError(f"No writable data directory: {last_error}")


def create_app(data_dir: Path | None = None) -> FastAPI:
    root = data_dir or _writable_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "infinityx.db")
    manager = Manager(store, root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.bind(asyncio.get_running_loop())
        manager.recover()
        heart = asyncio.create_task(manager.heartbeat())
        try:
            yield
        finally:
            heart.cancel()
            await manager.shutdown()

    app = FastAPI(title="InfinityX Video Downloader", lifespan=lifespan)
    app.state.store = store
    app.state.manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    @app.exception_handler(AppError)
    async def app_error(_request, exc: AppError):
        body = {"detail": exc.message, "code": exc.code, **exc.extra}
        return JSONResponse(status_code=exc.status, content=body)

    @app.exception_handler(Exception)
    async def unexpected(_request, exc: Exception):
        logging.exception("unhandled error")
        return JSONResponse(status_code=500, content={"detail": "Unexpected error", "code": "error"})

    @app.get("/api/health")
    def health():
        return {
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "conversion": ffmpeg_available(),
            "version": "1.0.0",
        }

    @app.get("/api/platforms")
    def platforms():
        return all_platforms()

    @app.get("/api/stats")
    def stats():
        counts = store.counts()
        recent = [public_job(row) for row in store.list_jobs("library")[:6]]
        return {**counts, "recent": recent, "platforms": all_platforms()}

    @app.get("/api/settings")
    def get_settings():
        return store.settings()

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn):
        values = body.model_dump()
        values["filename_template"] = validate_template(values["filename_template"])
        saved = store.save_settings(values)
        if manager.cond is not None:
            async with manager.cond:
                manager.cond.notify_all()
        return saved

    @app.post("/api/analyze")
    async def analyze(body: AnalyzeIn):
        return await analyze_url(body.url, store.settings()["max_bytes"], body.adult_confirmed)

    @app.post("/api/search")
    async def search(body: SearchIn):
        return await asyncio.to_thread(search_public, body.query, body.source)

    @app.get("/api/downloads")
    def downloads(scope: str = "all"):
        if scope not in {"all", "queue", "history", "library"}:
            raise AppError("Unknown list.")
        return [public_job(row) for row in store.list_jobs(scope)]

    @app.post("/api/downloads")
    async def create_download(body: DownloadIn):
        if body.media_type not in {"video", "audio"}:
            raise AppError("Choose video or audio.")
        if body.convert_to not in {None, "mp3", "wav", "m4a", "mp4", "webm"}:
            raise AppError("That conversion is not available.")
        if body.filename_template:
            validate_template(body.filename_template)
        job = await manager.enqueue(body.model_dump())
        return public_job(job)

    @app.post("/api/downloads/{job_id}/pause")
    async def pause(job_id: str):
        return public_job(await manager.pause(job_id))

    @app.post("/api/downloads/{job_id}/resume")
    async def resume(job_id: str):
        return public_job(await manager.resume(job_id))

    @app.post("/api/downloads/{job_id}/cancel")
    async def cancel(job_id: str):
        return public_job(await manager.cancel(job_id))

    @app.post("/api/downloads/{job_id}/retry")
    async def retry(job_id: str):
        return public_job(await manager.retry(job_id))

    @app.post("/api/library/{job_id}/extract")
    def extract(job_id: str, body: ExtractIn):
        return public_job(manager.extract_audio(job_id, body.target))

    @app.patch("/api/library/{job_id}")
    def rename(job_id: str, body: RenameIn):
        return public_job(manager.rename(job_id, body.filename))

    @app.delete("/api/library/{job_id}")
    def delete_file(job_id: str):
        return public_job(manager.remove_library_file(job_id))

    @app.get("/api/library/{job_id}/media")
    def media(job_id: str, download: bool = False):
        row = store.get(job_id)
        if not row or row["removed"] or not row.get("filepath"):
            raise AppError("That file is not available.", status=404)
        path = manager.guarded(row["filepath"])
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        disposition = "attachment" if download else "inline"
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type=disposition)

    @app.get("/api/events")
    async def events():
        queue = manager.subscribe()

        async def stream():
            try:
                yield "data: {\"type\": \"ready\"}\n\n"
                while True:
                    event = await queue.get()
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                manager.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


app = create_app()
