import asyncio
import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, unquote, urljoin, urlparse

import httpx

from .errors import AppError, TransferStopped
from .media import convert_media, ensure_ffmpeg, ffmpeg_available, probe_media
from .platforms import detect_platform
from .security import (
    MEDIA_EXTENSIONS,
    assert_public_url,
    prepare_source_url,
    clean_text,
    extension_of,
    friendly_download_error,
    is_audio_extension,
    minor_reference,
    render_filename,
    safe_thumbnail,
    sanitize_filename,
)
from .store import Store

USER_AGENT = "InfinityX/1.0"
FORMAT_ID = re.compile(r"[A-Za-z0-9_\-.+]{1,80}")
REDIRECTS = {301, 302, 303, 307, 308}
_analysis_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def range_header(start: int) -> dict[str, str]:
    if start < 0:
        raise AppError("Invalid resume position.")
    if start == 0:
        return {}
    return {"Range": f"bytes={start}-"}


async def write_stream(chunks, dest: Path, controls, on_progress, total: int | None, max_bytes: int, start_at: int = 0) -> int:
    written = start_at
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_emit = 0.0
    started = time.monotonic()
    base = start_at
    with dest.open("ab" if start_at else "wb") as handle:
        async for chunk in chunks:
            if controls.cancel.is_set():
                raise TransferStopped("cancel", written)
            if controls.pause.is_set():
                raise TransferStopped("pause", written)
            if not chunk:
                continue
            if written + len(chunk) > max_bytes:
                raise AppError("The file is larger than the size limit in Settings.")
            handle.write(chunk)
            written += len(chunk)
            now = time.monotonic()
            if now - last_emit >= 0.2 or (total and written >= total):
                elapsed = max(now - started, 0.001)
                speed = (written - base) / elapsed
                remaining = int((total - written) / speed) if total and speed > 0 and total >= written else None
                on_progress(written, total, speed, remaining)
                last_emit = now
    return written


def _blocked(platform, message: str | None = None, alternative: str | None = None) -> dict:
    return {
        "supported": False,
        "direct": False,
        "platform": platform.public(),
        "title": None,
        "creator": None,
        "thumbnail": None,
        "duration": None,
        "formats": [],
        "limitation": message or platform.limitation,
        "alternative": alternative or platform.alternative,
    }


def _header_size(headers: httpx.Headers, status: int) -> int | None:
    if status == 206:
        match = re.search(r"/(\d+)\s*$", headers.get("content-range", ""))
        if match:
            return int(match.group(1))
    length = headers.get("content-length")
    if length and length.isdigit():
        return int(length)
    return None


async def resolve_public(client: httpx.AsyncClient, url: str, method: str = "GET", headers: dict | None = None):
    current = assert_public_url(url)
    outgoing = {"User-Agent": USER_AGENT, **(headers or {})}
    for _ in range(5):
        request = client.build_request(method, current, headers=outgoing)
        response = await client.send(request, stream=True)
        if response.status_code in REDIRECTS:
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise AppError("The site sent a redirect without a destination.")
            current = assert_public_url(urljoin(current, location))
            if response.status_code == 303:
                method = "GET"
            continue
        return current, response
    raise AppError("This link redirected too many times.")


def _is_media(content_type: str, ext: str) -> bool:
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind.startswith("video/") or kind.startswith("audio/"):
        return True
    return kind == "application/octet-stream" and bool(ext)


def _file_title(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    return clean_text(Path(name).stem, 120) or "Direct file"


async def probe_direct(url: str, max_bytes: int) -> dict | None:
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            final, response = await resolve_public(client, url, "HEAD")
        except httpx.HTTPError:
            return None
        if response.status_code in {405, 501}:
            await response.aclose()
            try:
                final, response = await resolve_public(client, url, "GET", {"Range": "bytes=0-0"})
            except httpx.HTTPError:
                return None
        content_type = response.headers.get("content-type", "")
        size = _header_size(response.headers, response.status_code)
        await response.aclose()
    ext = extension_of(final) or extension_of(url)
    if not _is_media(content_type, ext):
        return None
    if size and size > max_bytes:
        raise AppError("The file is larger than the size limit in Settings.")
    suffix = ext.lstrip(".") or ("mp4" if "video" in content_type else "bin")
    audio = is_audio_extension(suffix) or content_type.lower().startswith("audio/")
    return {
        "supported": True,
        "direct": True,
        "final_url": final,
        "title": _file_title(final),
        "creator": "",
        "thumbnail": None,
        "duration": None,
        "formats": [
            {
                "format_id": "original",
                "ext": suffix,
                "height": None,
                "filesize": size,
                "vcodec": None,
                "acodec": None,
                "audio_only": audio,
                "video_only": False,
                "note": "Original file",
            }
        ],
        "limitation": None,
        "alternative": None,
    }


def _formats_from_info(info: dict) -> list[dict]:
    formats = []
    seen = set()
    for item in info.get("formats") or []:
        if item.get("has_drm"):
            continue
        format_id = str(item.get("format_id") or "")
        if not FORMAT_ID.fullmatch(format_id) or format_id in seen:
            continue
        ext = str(item.get("ext") or "")
        if ext in {"mhtml", "xml", "json"}:
            continue
        note = str(item.get("format_note") or "")
        if "storyboard" in note.lower():
            continue
        vcodec = item.get("vcodec")
        acodec = item.get("acodec")
        if vcodec == "none" and acodec == "none":
            continue
        media_url = item.get("url") or ""
        if media_url.startswith(("http://", "https://")):
            try:
                assert_public_url(media_url)
            except AppError:
                continue
        seen.add(format_id)
        audio_only = (vcodec == "none" and acodec not in (None, "none")) or (
            vcodec is None and acodec is None and is_audio_extension(ext)
        )
        video_only = acodec == "none" and vcodec not in (None, "none")
        formats.append(
            {
                "format_id": format_id,
                "ext": ext,
                "height": item.get("height"),
                "filesize": item.get("filesize") or item.get("filesize_approx"),
                "vcodec": None if vcodec in (None, "none") else vcodec,
                "acodec": None if acodec in (None, "none") else acodec,
                "audio_only": audio_only,
                "video_only": video_only,
                "note": clean_text(note, 80),
            }
        )
    formats.sort(key=lambda fmt: ((fmt["height"] or 0), fmt["filesize"] or 0), reverse=True)
    if any(not fmt["audio_only"] for fmt in formats):
        tallest = next((fmt["height"] for fmt in formats if fmt["height"]), None)
        formats.insert(
            0,
            {
                "format_id": "best",
                "ext": next((fmt["ext"] for fmt in formats if not fmt["audio_only"] and fmt["ext"]), "mp4"),
                "height": tallest,
                "filesize": None,
                "vcodec": None,
                "acodec": None,
                "audio_only": False,
                "video_only": False,
                "note": "Best available of the listed streams",
            },
        )
    return formats


def extract_public(url: str, max_bytes: int) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "no_color": True,
        "max_filesize": max_bytes,
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise AppError(friendly_download_error(exc)) from exc
    if not info:
        raise AppError("No authorized public download is available for this link.")
    if info.get("_type") in {"playlist", "multi_video", "compat_list"}:
        raise AppError("Playlists are not downloaded. Paste one video URL.")
    if info.get("is_live"):
        raise AppError("Live streams are not recorded. Open the official player instead.")
    availability = str(info.get("availability") or "").lower()
    if availability in {"private", "needs_auth", "subscriber_only", "premium_only", "login_required"}:
        raise AppError("This media requires a login. InfinityX does not use accounts, cookies, or passwords.")
    title = clean_text(info.get("title") or "Untitled")
    creator = clean_text(info.get("uploader") or info.get("channel") or info.get("creator") or "")
    if minor_reference(title, creator):
        raise AppError("This link is blocked.", code="blocked")
    formats = _formats_from_info(info)
    if not formats:
        raise AppError("No authorized public download is available for this link.")
    return {
        "supported": True,
        "direct": False,
        "title": title,
        "creator": creator,
        "thumbnail": safe_thumbnail(info.get("thumbnail")),
        "duration": info.get("duration"),
        "extractor_id": clean_text(str(info.get("id") or "video"), 80),
        "formats": formats,
        "limitation": None,
        "alternative": None,
    }


def _remember(url: str, result: dict) -> None:
    if result.get("supported"):
        with _cache_lock:
            _analysis_cache[url] = (time.monotonic(), result)


async def analyze_url(url: str, max_bytes: int, adult_confirmed: bool = True) -> dict:
    clean = prepare_source_url(url)
    platform = detect_platform(clean)
    if platform.mode == "live":
        return _blocked(platform)
    with _cache_lock:
        cached = _analysis_cache.get(clean)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    try:
        direct = await probe_direct(clean, max_bytes)
    except AppError as exc:
        if exc.code == "ssrf" or "size limit" in exc.message or "redirect" in exc.message.lower():
            raise
        direct = None
    if direct:
        direct["platform"] = platform.public()
        direct["title"] = clean_text(direct["title"]) or platform.name
        _remember(clean, direct)
        return direct
    try:
        extracted = await asyncio.to_thread(extract_public, clean, max_bytes)
    except AppError as exc:
        alt = platform.alternative or "Usa il player ufficiale del sito se il video è pubblico."
        if platform.mode != "public":
            return _blocked(platform, exc.message or platform.limitation, alt)
        return _blocked(platform, exc.message, alt)
    extracted["platform"] = platform.public()
    _remember(clean, extracted)
    return extracted


def selector_for(format_id: str, video_only: bool, media_type: str) -> str:
    if not FORMAT_ID.fullmatch(format_id):
        raise AppError("Choose one of the listed formats.")
    if format_id == "best":
        if media_type == "audio":
            return "bestaudio/best"
        if ffmpeg_available():
            return "bestvideo+bestaudio/best"
        return "best[acodec!=none][vcodec!=none]/best"
    if media_type == "audio" or not video_only or not ffmpeg_available():
        return format_id
    return f"{format_id}+bestaudio/{format_id}"


def search_public(query: str, source: str = "pornhub") -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    text = clean_text(query, 120)
    if len(text) < 2:
        raise AppError("Scrivi almeno 2 caratteri per cercare.")
    if minor_reference(text):
        raise AppError("Questa ricerca è bloccata.", code="blocked")
    if source == "youtube":
        target = f"ytsearch10:{text}"
    elif source == "pornhub":
        target = f"https://www.pornhub.com/video/search?search={quote_plus(text)}"
    else:
        raise AppError("Scegli una fonte tra Pornhub e YouTube.")

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlist_items": "1-10",
        "socket_timeout": 15,
        "retries": 1,
        "no_color": True,
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=False)
    except DownloadError as exc:
        raise AppError(friendly_download_error(exc)) from exc

    results = []
    for entry in (info or {}).get("entries") or []:
        if not isinstance(entry, dict):
            continue
        title = clean_text(entry.get("title") or "Video", 140)
        creator = clean_text(entry.get("uploader") or entry.get("channel") or "", 80)
        if minor_reference(title, creator):
            continue
        raw_url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
        entry_id = str(entry.get("id") or "").strip()
        if not raw_url and entry_id:
            if source == "youtube":
                raw_url = f"https://www.youtube.com/watch?v={entry_id}"
            elif source == "pornhub":
                raw_url = f"https://www.pornhub.com/view_video.php?viewkey={entry_id}"
        if raw_url.startswith("/") and source == "pornhub":
            raw_url = f"https://www.pornhub.com{raw_url}"
        if not raw_url.startswith(("http://", "https://")):
            continue
        try:
            clean_url = assert_public_url(raw_url)
        except AppError:
            continue
        thumbs = entry.get("thumbnails") or []
        thumb = entry.get("thumbnail") or (thumbs[-1].get("url") if thumbs and isinstance(thumbs[-1], dict) else None)
        results.append(
            {
                "title": title,
                "url": clean_url,
                "thumbnail": safe_thumbnail(thumb),
                "duration": entry.get("duration"),
                "creator": creator,
                "platform": source,
            }
        )
        if len(results) >= 10:
            break
    return {"source": source, "query": text, "results": results}


class Control:
    def __init__(self) -> None:
        self.pause = threading.Event()
        self.cancel = threading.Event()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


class Manager:
    def __init__(self, store: Store, root: Path) -> None:
        self.store = store
        self.root = root
        self.downloads = root / "downloads"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.controls: dict[str, Control] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.active = 0
        self.cond: asyncio.Condition | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.cond = asyncio.Condition()

    def publish(self, event: dict) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        for queue in list(self.subscribers):
            loop.call_soon_threadsafe(self._offer, queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(15)
            self.publish({"type": "ping"})

    def _ensure(self, job_id: str) -> Control:
        control = self.controls.get(job_id)
        if control is None:
            control = Control()
            self.controls[job_id] = control
        return control

    def limit(self) -> int:
        return max(1, min(3, int(self.store.settings()["max_concurrent"])))

    async def _acquire(self, control: Control) -> str:
        assert self.cond is not None
        async with self.cond:
            while self.active >= self.limit() and not control.cancel.is_set() and not control.pause.is_set():
                await self.cond.wait()
            if control.cancel.is_set():
                return "cancel"
            if control.pause.is_set():
                return "pause"
            self.active += 1
            return "run"

    async def _release(self) -> None:
        assert self.cond is not None
        async with self.cond:
            self.active = max(0, self.active - 1)
            self.cond.notify_all()

    def spawn(self, job_id: str) -> None:
        existing = self.tasks.get(job_id)
        if existing and not existing.done():
            return
        self.tasks[job_id] = asyncio.create_task(self._run(job_id))

    def recover(self) -> None:
        self.store.mark_interrupted()
        for row in self.store.list_jobs("queue"):
            if row["status"] == "queued":
                self._ensure(row["id"])
                self.spawn(row["id"])

    async def enqueue(self, payload: dict) -> dict:
        if len(self.store.list_jobs("queue")) >= 50:
            raise AppError("The queue is full. Finish or cancel a download first.")
        analysis = await analyze_url(payload["url"], self.store.settings()["max_bytes"], payload.get("adult_confirmed", False))
        if not analysis["supported"]:
            raise AppError(analysis["limitation"] or "This link cannot be downloaded.")
        chosen = next((item for item in analysis["formats"] if item["format_id"] == payload["format_id"]), None)
        if chosen is None:
            raise AppError("Choose one of the listed formats.")
        convert_to = payload.get("convert_to")
        media_type = payload["media_type"]
        if convert_to in {"mp3", "wav", "m4a"} and media_type != "audio":
            raise AppError("Audio conversion requires the audio option.")
        if convert_to in {"mp4", "webm"} and media_type != "video":
            raise AppError("Video conversion requires the video option.")
        if media_type == "audio" and not chosen["audio_only"] and convert_to not in {"mp3", "wav", "m4a"} and chosen["format_id"] != "best":
            raise AppError("Choose an audio format, or convert the file to MP3, WAV, or M4A.")
        template = payload.get("filename_template") or self.store.settings()["filename_template"]
        job = self.store.create_download(
            {
                "url": prepare_source_url(payload["url"]),
                "platform": analysis["platform"]["id"],
                "title": analysis.get("title") or "Untitled",
                "creator": analysis.get("creator") or "",
                "thumbnail": analysis.get("thumbnail"),
                "media_type": media_type,
                "format_id": chosen["format_id"],
                "convert_to": convert_to,
                "bytes_total": chosen.get("filesize"),
                "direct": analysis.get("direct", False),
                "template": template,
            }
        )
        self._ensure(job["id"])
        self.spawn(job["id"])
        self.publish({"type": "queued", "id": job["id"]})
        return job

    async def pause(self, job_id: str) -> dict:
        row = self._require(job_id)
        if row["status"] not in {"queued", "downloading"}:
            raise AppError("This download cannot be paused.")
        self._ensure(job_id).pause.set()
        assert self.cond is not None
        async with self.cond:
            self.cond.notify_all()
        if row["status"] == "queued":
            self.store.update(job_id, status="paused")
        return self.store.get(job_id)

    async def resume(self, job_id: str) -> dict:
        row = self._require(job_id)
        if row["status"] != "paused":
            raise AppError("This download is not paused.")
        control = self._ensure(job_id)
        control.pause.clear()
        control.cancel.clear()
        self.store.update(job_id, status="queued", error=None)
        self.spawn(job_id)
        return self.store.get(job_id)

    async def cancel(self, job_id: str) -> dict:
        row = self._require(job_id)
        if row["status"] in {"completed", "cancelled"}:
            raise AppError("This download is already finished.")
        control = self._ensure(job_id)
        control.cancel.set()
        assert self.cond is not None
        async with self.cond:
            self.cond.notify_all()
        if row["status"] in {"queued", "paused"}:
            self.store.update(job_id, status="cancelled", finished_at=_iso())
        return self.store.get(job_id)

    async def retry(self, job_id: str) -> dict:
        row = self._require(job_id)
        if row["status"] not in {"failed", "cancelled"}:
            raise AppError("Only a failed or cancelled download can be retried.")
        control = self._ensure(job_id)
        control.pause.clear()
        control.cancel.clear()
        self.store.update(job_id, status="queued", error=None)
        self.spawn(job_id)
        return self.store.get(job_id)

    async def _run(self, job_id: str) -> None:
        control = self._ensure(job_id)
        gate = await self._acquire(control)
        if gate != "run":
            status = "cancelled" if gate == "cancel" else "paused"
            fields = {"status": status}
            if status == "cancelled":
                fields["finished_at"] = _iso()
            self.store.update(job_id, **fields)
            self.publish({"type": status, "id": job_id})
            return
        try:
            row = self.store.get(job_id)
            if not row or row["status"] in {"cancelled", "completed"}:
                return
            self.store.update(job_id, status="downloading", error=None)
            self.publish({"type": "progress", "id": job_id})
            if row["direct"]:
                path = await self._download_http(row, control)
            else:
                path = await asyncio.to_thread(self._download_ytdlp, row, control)
            final = await asyncio.to_thread(self._finish_file, self.store.get(job_id), path)
            meta = await asyncio.to_thread(probe_media, final)
            self.store.update(
                job_id,
                status="completed",
                progress=100,
                speed=None,
                eta=0,
                filepath=str(final),
                filename=final.name,
                bytes_done=final.stat().st_size,
                bytes_total=final.stat().st_size,
                metadata=json.dumps(meta),
                finished_at=_iso(),
                error=None,
            )
            fresh = self.store.get(job_id)
            self.publish({"type": "complete", "id": job_id, "title": fresh["title"]})
        except TransferStopped as stopped:
            self._stop(job_id, stopped.kind, stopped.written)
        except AppError as exc:
            self.store.update(job_id, status="failed", error=exc.message, finished_at=_iso())
            self.publish({"type": "failed", "id": job_id})
        except Exception:
            logging.exception("download failed for %s", job_id)
            kind = "cancelled" if control.cancel.is_set() else "paused" if control.pause.is_set() else "failed"
            if kind == "failed":
                self.store.update(job_id, status="failed", error="No authorized public download is available for this link.", finished_at=_iso())
            else:
                fields = {"status": kind, "error": None}
                if kind == "cancelled":
                    fields["finished_at"] = _iso()
                self.store.update(job_id, **fields)
            self.publish({"type": kind, "id": job_id})
        finally:
            await self._release()

    def _stop(self, job_id: str, kind: str, written: int) -> None:
        if kind == "cancel":
            self.store.update(job_id, status="cancelled", bytes_done=written, finished_at=_iso(), error=None)
            self.publish({"type": "cancelled", "id": job_id})
            return
        self.store.update(job_id, status="paused", bytes_done=written, error=None)
        self.publish({"type": "paused", "id": job_id})

    def _progress(self, job_id: str, done: int, total: int | None, speed: float | None, eta: int | None) -> None:
        percent = min(100.0, (done / total) * 100) if total else 0.0
        self.store.update(
            job_id,
            progress=percent,
            bytes_done=done,
            bytes_total=total,
            speed=speed,
            eta=eta,
            status="downloading",
        )
        self.publish({"type": "progress", "id": job_id, "progress": round(percent, 1), "speed": speed, "eta": eta})

    async def _download_http(self, row: dict, control: Control) -> Path:
        folder = self.downloads / row["id"]
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / "download.part"
        start_at = part.stat().st_size if part.exists() else 0
        settings = self.store.settings()
        timeout = httpx.Timeout(settings["max_minutes"] * 60, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            final, response = await resolve_public(client, row["url"], "GET", range_header(start_at))
            status = response.status_code
            if status >= 400:
                await response.aclose()
                raise AppError(f"The file server returned status {status}.")
            if start_at and status == 200:
                start_at = 0
                part.unlink(missing_ok=True)
            total = _header_size(response.headers, status)
            if total and start_at and status == 206:
                total = _header_size(response.headers, 206)
            if total and total > settings["max_bytes"]:
                await response.aclose()
                raise AppError("The file is larger than the size limit in Settings.")

            async def chunks():
                async for chunk in response.aiter_bytes(64 * 1024):
                    yield chunk

            try:
                await write_stream(
                    chunks(),
                    part,
                    control,
                    lambda done, total_size, speed, eta: self._progress(row["id"], done, total_size, speed, eta),
                    total,
                    settings["max_bytes"],
                    start_at,
                )
            finally:
                await response.aclose()
        ext = extension_of(final) or ".bin"
        target = folder / f"source{ext}"
        if target.exists():
            target.unlink()
        part.replace(target)
        return target

    def _download_ytdlp(self, row: dict, control: Control) -> Path:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadCancelled, DownloadError

        cached = _analysis_cache.get(row["url"], (0, None))[1]
        chosen = None
        if cached:
            chosen = next((item for item in cached["formats"] if item["format_id"] == row["format_id"]), None)
        video_only = bool(chosen and chosen.get("video_only"))
        selector = selector_for(row["format_id"], video_only, row["media_type"])
        folder = self.downloads / row["id"]
        folder.mkdir(parents=True, exist_ok=True)
        settings = self.store.settings()
        started = time.monotonic()
        last_bytes = -1
        last_change = started
        finished: list[str] = []
        checked: set[str] = set()

        def hook(status: dict) -> None:
            nonlocal last_bytes, last_change
            media_url = (status.get("info_dict") or {}).get("url")
            if isinstance(media_url, str) and media_url.startswith(("http://", "https://")) and media_url not in checked:
                assert_public_url(media_url)
                checked.add(media_url)
            if control.cancel.is_set() or time.monotonic() - started > settings["max_minutes"] * 60:
                raise DownloadCancelled("INFINITYX_CANCEL")
            if control.pause.is_set():
                raise DownloadCancelled("INFINITYX_PAUSE")
            if status.get("status") == "downloading":
                done = int(status.get("downloaded_bytes") or 0)
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                if done != last_bytes:
                    last_bytes = done
                    last_change = time.monotonic()
                elif time.monotonic() - last_change > 180:
                    raise DownloadCancelled("INFINITYX_CANCEL")
                self._progress(row["id"], done, int(total) if total else None, status.get("speed"), status.get("eta"))
            elif status.get("status") == "finished" and status.get("filename"):
                finished.append(status["filename"])

        ffmpeg_bin = ensure_ffmpeg()
        options = {
            "format": selector,
            **({"ffmpeg_location": str(ffmpeg_bin.parent)} if ffmpeg_bin else {}),
            "outtmpl": str(folder / "%(id)s.%(ext)s"),
            "paths": {"home": str(folder)},
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "windowsfilenames": True,
            "continuedl": True,
            "overwrites": False,
            "noprogress": True,
            "no_color": True,
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": 30,
            "max_filesize": settings["max_bytes"],
            "progress_hooks": [hook],
            "concurrent_fragment_downloads": 1,
            "writesubtitles": False,
            "writeautomaticsub": False,
        }
        try:
            with YoutubeDL(options) as ydl:
                ydl.download([row["url"]])
        except DownloadCancelled as exc:
            kind = "pause" if control.pause.is_set() or "PAUSE" in str(exc) else "cancel"
            raise TransferStopped(kind, int(row.get("bytes_done") or 0)) from exc
        except DownloadError as exc:
            if control.pause.is_set():
                raise TransferStopped("pause", int(row.get("bytes_done") or 0)) from exc
            if control.cancel.is_set():
                raise TransferStopped("cancel", int(row.get("bytes_done") or 0)) from exc
            raise AppError(friendly_download_error(exc)) from exc
        candidates = [Path(name) for name in finished if Path(name).is_file()]
        if not candidates:
            candidates = [path for path in folder.iterdir() if path.is_file() and not path.name.endswith(".part")]
        if not candidates:
            raise AppError("The download finished without a media file.")
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _finish_file(self, row: dict, source: Path) -> Path:
        source = source.resolve()
        if not source.is_relative_to(self.downloads.resolve()):
            raise AppError("The downloaded file was outside the media folder.")
        target_kind = row.get("convert_to")
        working = source
        if target_kind and source.suffix.lower() != f".{target_kind}":
            converted = source.with_name(f"{source.stem}.converted.{target_kind}")
            working = convert_media(source, converted, target_kind, self.root)
        ext = working.suffix.lstrip(".") or "bin"
        template = row.get("template") or self.store.settings()["filename_template"]
        name = render_filename(
            template,
            {
                "title": row.get("title") or "download",
                "creator": row.get("creator") or "",
                "platform": row.get("platform") or "video",
                "date": _stamp(),
                "ext": ext,
                "id": row["id"][:8],
            },
        )
        if not name.lower().endswith(f".{ext.lower()}"):
            name = f"{Path(name).stem}.{ext}"
        destination = source.parent / sanitize_filename(name)
        if working.resolve() != destination.resolve():
            if destination.exists():
                destination.unlink()
            working.replace(destination)
        if source.exists() and source.resolve() != destination.resolve():
            source.unlink()
        try:
            pc_downloads = Path.home() / "Downloads" / "InfinityX"
            pc_downloads.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, pc_downloads / destination.name)
        except OSError:
            pass
        return destination

    def _require(self, job_id: str) -> dict:
        row = self.store.get(job_id)
        if row is None:
            raise AppError("That download does not exist.", status=404)
        return row

    def guarded(self, filepath: str) -> Path:
        path = Path(filepath).resolve()
        if not path.is_relative_to(self.downloads.resolve()) or not path.is_file():
            raise AppError("That file is not available.", status=404)
        return path

    def extract_audio(self, job_id: str, target: str) -> dict:
        if target not in {"mp3", "wav", "m4a"}:
            raise AppError("Choose MP3, WAV, or M4A.")
        row = self._require(job_id)
        if row["status"] != "completed" or row["removed"] or not row.get("filepath"):
            raise AppError("Choose a file that is still in the library.", status=404)
        source = self.guarded(row["filepath"])
        folder = self.downloads / f"{row['id']}-audio"
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f"extract.{target}"
        convert_media(source, output, target, self.root)
        template = self.store.settings()["filename_template"]
        name = render_filename(
            template,
            {
                "title": row.get("title") or "audio",
                "creator": row.get("creator") or "",
                "platform": row.get("platform") or "audio",
                "date": _stamp(),
                "ext": target,
                "id": row["id"][:8],
            },
        )
        if not name.lower().endswith(f".{target}"):
            name = f"{Path(name).stem}.{target}"
        final = folder / sanitize_filename(name)
        if final.exists():
            final.unlink()
        output.replace(final)
        created = self.store.create_library_file(
            {
                "url": row["url"],
                "platform": row["platform"],
                "title": row["title"],
                "creator": row["creator"],
                "media_type": "audio",
                "format_id": target,
                "convert_to": target,
                "filepath": str(final),
                "filename": final.name,
                "bytes_done": final.stat().st_size,
                "bytes_total": final.stat().st_size,
                "metadata": json.dumps(probe_media(final)),
                "template": template,
            }
        )
        return created

    def rename(self, job_id: str, filename: str) -> dict:
        row = self._require(job_id)
        if row["status"] != "completed" or not row.get("filepath"):
            raise AppError("Only a completed library file can be renamed.")
        path = self.guarded(row["filepath"])
        ext = path.suffix.lower()
        if ext not in MEDIA_EXTENSIONS:
            raise AppError("This file type cannot be renamed.")
        stem = sanitize_filename(Path(filename).stem)
        destination = path.with_name(f"{stem}{ext}")
        if destination.resolve() != path.resolve():
            if destination.exists():
                raise AppError("A file with that name already exists.")
            path.rename(destination)
        return self.store.update(job_id, filepath=str(destination), filename=destination.name)

    def remove_library_file(self, job_id: str) -> dict:
        row = self._require(job_id)
        if row.get("filepath"):
            try:
                path = self.guarded(row["filepath"])
            except AppError:
                path = None
            if path and path.exists():
                path.unlink()
        return self.store.update(job_id, removed=1)

    async def shutdown(self) -> None:
        for control in self.controls.values():
            control.cancel.set()
        if self.cond is not None:
            async with self.cond:
                self.cond.notify_all()
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
