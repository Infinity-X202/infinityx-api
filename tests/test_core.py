import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.engine import (
    Control,
    Manager,
    _formats_from_info,
    analyze_url,
    extract_public,
    range_header,
    resolve_public,
    search_public,
    write_stream,
)
from app.errors import AppError, TransferStopped
from app.main import create_app
from app.media import convert_media, probe_media
from app.platforms import detect_platform
from app.security import assert_public_url, normalize_user_url, prepare_source_url, render_filename, sanitize_filename, validate_template
from app.store import Store


BLOCKED = [
    "http://127.0.0.1/video.mp4",
    "http://127.0.0.1:9/video.mp4",
    "http://localhost/video.mp4",
    "http://[::1]/video.mp4",
    "http://169.254.169.254/latest/meta-data",
    "http://10.1.2.3/a.mp4",
    "http://192.168.1.20/a.mp4",
    "http://172.16.5.5/a.mp4",
    "http://0.0.0.0/a.mp4",
    "file:///c:/windows/win.ini",
    "http://user:pass@1.1.1.1/a.mp4",
    "javascript:alert(1)",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://printer.local/a.mp4",
    "http://2130706433/a.mp4",
    "http://example.com/underage-video",
]


def test_search_blocks_unsafe_query_and_maps_results(monkeypatch):
    with pytest.raises(AppError):
        search_public("underage test", "youtube")

    class FakeYDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, target, download=False):
            assert "pornhub.com/video/search?search=demo+clip" in target
            return {
                "entries": [
                    {"id": "ph123", "title": "Public Demo", "url": "/view_video.php?viewkey=ph123", "duration": 65, "uploader": "Studio"},
                    {"id": "bad", "title": "underage bad", "url": "https://1.1.1.1/x"},
                    {"id": "priv", "title": "Local", "url": "http://127.0.0.1/x.mp4"},
                ]
            }

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    out = search_public("demo clip", "pornhub")
    assert len(out["results"]) == 1
    assert out["results"][0]["url"] == "https://www.pornhub.com/view_video.php?viewkey=ph123"


def test_saveporn_homepage_is_rejected():
    with pytest.raises(AppError) as exc:
        normalize_user_url("https://www.saveporn.net/it/")
    assert exc.value.code == "needs_video_url"


def test_saveporn_video_link_rewrites_to_pornhub():
    out = prepare_source_url("https://www.saveporn.net/view_video.php?viewkey=ph5e6260ee02354")
    assert "pornhub.com" in out
    assert "viewkey=ph5e6260ee02354" in out


@pytest.mark.parametrize("url", BLOCKED)
def test_rejects_unsafe_urls(url):
    with pytest.raises(AppError):
        assert_public_url(url)


def test_allows_public_literal_ip():
    assert assert_public_url("https://1.1.1.1/clip.mp4").startswith("https://1.1.1.1/")
    assert assert_public_url("https://[2606:4700:4700::1111]/a.mp4").startswith("https://[2606:4700:4700::1111]/")


def test_filename_and_template_are_sanitized():
    assert ".." not in sanitize_filename("../../calc.exe & whoami")
    assert "/" not in sanitize_filename("a/b\\c:d")
    with pytest.raises(AppError):
        validate_template("{title};calc")
    name = render_filename("{title}-{id}.{ext}", {"title": "../bad name", "id": "abc12345", "ext": "mp4"})
    assert name.endswith(".mp4")
    assert ".." not in name
    assert "bad name" in name


@pytest.mark.parametrize(
    ("url", "platform_id", "mode"),
    [
        ("https://www.youtube.com/watch?v=abc", "youtube", "public"),
        ("https://youtu.be/abc", "youtube", "public"),
        ("https://www.tiktok.com/@user/video/1", "tiktok", "public"),
        ("https://www.instagram.com/reel/abc/", "instagram", "public"),
        ("https://x.com/user/status/1", "x", "public"),
        ("https://twitter.com/user/status/1", "x", "public"),
        ("https://vimeo.com/123", "vimeo", "public"),
        ("https://www.twitch.tv/videos/1", "twitch", "public"),
        ("https://www.dailymotion.com/video/x1", "dailymotion", "public"),
        ("https://www.reddit.com/r/videos/comments/abc/title/", "reddit", "public"),
        ("https://www.pornhub.com/view_video.php?viewkey=ph1", "pornhub", "public"),
        ("https://www.xvideos.com/video1/title", "xvideos", "public"),
        ("https://www.xnxx.com/video-abc/title", "xnxx", "public"),
        ("https://xhamster.com/videos/title-1", "xhamster", "public"),
        ("https://www.redtube.com/1", "redtube", "public"),
        ("https://www.youporn.com/watch/1", "youporn", "public"),
        ("https://spankbang.com/abc/video/title", "spankbang", "public"),
        ("https://www.eporner.com/video-abc/title/", "eporner", "public"),
        ("https://txxx.com/videos/1/title/", "txxx", "public"),
        ("https://beeg.com/1", "beeg", "public"),
        ("https://hqporner.com/hdporn/1.html", "hqporner", "public"),
        ("https://www.brazzers.com/video/1", "brazzers", "paywall"),
        ("https://www.chaturbate.com/room/", "chaturbate", "live"),
        ("https://stripchat.com/room", "stripchat", "live"),
        ("https://www.literotica.com/s/story", "literotica", "text"),
        ("https://www.imagefap.com/pictures/1", "imagefap", "image"),
        ("https://example.com/file.mp4", "other", "public"),
    ],
)
def test_platform_detection(url, platform_id, mode):
    platform = detect_platform(url)
    assert platform.id == platform_id
    assert platform.mode == mode


def test_live_cam_blocked_immediately():
    live = asyncio.run(analyze_url("https://chaturbate.com/someone", 1000))
    assert live["supported"] is False
    assert "not recorded" in (live["limitation"] or "").lower()


def test_format_list_keeps_only_real_public_streams():
    info = {
        "formats": [
            {"format_id": "18", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": "mp4a.40.2", "filesize": 1000, "url": "https://1.1.1.1/360.mp4"},
            {"format_id": "137", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 5000, "url": "https://1.1.1.1/1080.mp4", "format_note": "1080p"},
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 400, "url": "https://8.8.8.8/audio.m4a"},
            {"format_id": "720p", "ext": "mp4", "height": 720, "vcodec": None, "acodec": None, "url": "https://1.1.1.1/720.mp4"},
            {"format_id": "drm1", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "has_drm": True, "url": "https://1.1.1.1/drm.mp4"},
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none", "format_note": "storyboard", "url": "https://1.1.1.1/sb.mhtml"},
            {"format_id": "local", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "mp4a", "url": "http://127.0.0.1/secret.mp4"},
        ]
    }
    found = {item["format_id"]: item for item in _formats_from_info(info)}
    assert set(found) == {"best", "18", "137", "140", "720p"}
    assert found["720p"]["audio_only"] is False
    assert found["137"]["height"] == 1080
    assert found["140"]["audio_only"] is True
    assert found["137"]["video_only"] is True
    assert found["best"]["note"].startswith("Best available")


def test_extractor_refuses_playlist_live_and_private(monkeypatch):
    payloads = []

    class FakeYDL:
        def __init__(self, _options):
            self.payload = payloads[-1]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            return self.payload

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    payloads.append({"_type": "playlist", "entries": []})
    with pytest.raises(AppError, match="Playlist"):
        extract_public("https://1.1.1.1/list", 1000)
    payloads.append({"_type": "video", "is_live": True, "title": "Live", "id": "1", "formats": []})
    with pytest.raises(AppError, match="Live"):
        extract_public("https://1.1.1.1/live", 1000)
    payloads.append({"_type": "video", "availability": "private", "title": "Hidden", "id": "1", "formats": []})
    with pytest.raises(AppError, match="login"):
        extract_public("https://1.1.1.1/private", 1000)


def test_range_header_and_stream_controls(tmp_path):
    assert range_header(0) == {}
    assert range_header(1200) == {"Range": "bytes=1200-"}
    control = Control()

    async def chunks():
        yield b"a" * 40
        control.pause.set()
        yield b"b" * 40

    with pytest.raises(TransferStopped) as paused:
        asyncio.run(write_stream(chunks(), tmp_path / "a.part", control, lambda *_: None, 80, 1000))
    assert paused.value.kind == "pause"
    assert paused.value.written == 40

    control = Control()

    async def more():
        yield b"abc"
        control.cancel.set()
        yield b"def"

    with pytest.raises(TransferStopped) as cancelled:
        asyncio.run(write_stream(more(), tmp_path / "b.part", control, lambda *_: None, None, 1000))
    assert cancelled.value.kind == "cancel"

    async def huge():
        yield b"x" * 50

    with pytest.raises(AppError, match="size limit"):
        asyncio.run(write_stream(huge(), tmp_path / "c.part", Control(), lambda *_: None, None, 10))


def test_redirect_to_private_host_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret.mp4"})
        return httpx.Response(200, headers={"Content-Type": "video/mp4", "Content-Length": "4"})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            await resolve_public(client, "https://1.1.1.1/start")

    with pytest.raises(AppError):
        asyncio.run(run())


def test_history_persists_and_interrupted_downloads_pause(tmp_path):
    store = Store(tmp_path / "one.db")
    job = store.create_download({"url": "https://1.1.1.1/a.mp4", "media_type": "video", "format_id": "original", "direct": True, "title": "Clip"})
    store.update(job["id"], status="downloading", progress=40)
    reopened = Store(tmp_path / "one.db")
    assert reopened.get(job["id"])["progress"] == 40
    reopened.mark_interrupted()
    assert reopened.get(job["id"])["status"] == "paused"
    sample = tmp_path / "kept.mp3"
    sample.write_bytes(b"12345")
    reopened.update(job["id"], status="completed", filepath=str(sample), removed=0)
    assert reopened.counts()["storage_bytes"] == 5
    assert reopened.counts()["completed"] == 1


def test_ffmpeg_audio_and_video_conversion(tmp_path):
    tone = tmp_path / "tone.wav"
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", str(tone)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
        check=True,
        capture_output=True,
    )
    mp3 = convert_media(tone, tmp_path / "tone.mp3", "mp3", tmp_path)
    webm = convert_media(clip, tmp_path / "clip.webm", "webm", tmp_path)
    assert mp3.stat().st_size > 100
    assert webm.stat().st_size > 100
    assert probe_media(mp3)["audio_codec"] == "mp3"
    assert probe_media(webm)["video_codec"] == "vp9"


def test_api_security_settings_and_local_audio(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        blocked = client.post("/api/analyze", json={"url": "http://127.0.0.1/a.mp4"})
        assert blocked.status_code == 400
        assert blocked.json()["code"] == "ssrf"

        live = client.post("/api/analyze", json={"url": "https://www.livejasmin.com/room"})
        assert live.status_code == 200
        assert live.json()["supported"] is False

        image = client.post("/api/analyze", json={"url": "https://www.pornpics.com/galleries/1"})
        assert image.json()["platform"]["mode"] == "image"

        stats = client.get("/api/stats").json()
        assert stats["completed"] == 0
        assert any(item["id"] == "youtube" for item in stats["platforms"])

        rejected = client.put(
            "/api/settings",
            json={"max_concurrent": 9, "max_bytes": 2000000, "filename_template": "{title}.{ext}", "notify": True, "max_minutes": 30},
        )
        assert rejected.status_code == 422
        saved = client.put(
            "/api/settings",
            json={"max_concurrent": 1, "max_bytes": 2000000, "filename_template": "{title}.{ext}", "notify": False, "max_minutes": 30},
        )
        assert saved.status_code == 200
        assert saved.json()["max_concurrent"] == 1
        assert client.get("/api/settings").json()["notify"] is False

        refused = client.post(
            "/api/downloads",
            json={"url": "https://chaturbate.com/room", "format_id": "original", "media_type": "video"},
        )
        assert refused.status_code == 400

        folder = app.state.manager.downloads / "seed"
        folder.mkdir(parents=True)
        tone = folder / "tone.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=523:duration=1", "-c:a", "pcm_s16le", str(tone)],
            check=True,
            capture_output=True,
        )
        created = app.state.store.create_library_file(
            {
                "url": "local",
                "platform": "local",
                "title": "Preview tone",
                "creator": "InfinityX",
                "filepath": str(tone),
                "filename": tone.name,
                "media_type": "audio",
                "bytes_done": tone.stat().st_size,
                "bytes_total": tone.stat().st_size,
            }
        )
        extracted = client.post(f"/api/library/{created['id']}/extract", json={"target": "mp3"})
        assert extracted.status_code == 200
        assert extracted.json()["filename"].endswith(".mp3")
        renamed = client.patch(f"/api/library/{created['id']}", json={"filename": "../bad:name"})
        assert renamed.status_code == 200
        assert ".." not in renamed.json()["filename"]
        assert renamed.json()["filename"].endswith(".wav")
        media = client.get(f"/api/library/{extracted.json()['id']}/media")
        assert media.status_code == 200
        assert media.content[:3] == b"ID3" or media.headers["content-type"].startswith("audio/")
        removed = client.delete(f"/api/library/{created['id']}")
        assert removed.status_code == 200
        assert client.get(f"/api/library/{created['id']}/media").status_code == 404
        history = client.get("/api/downloads?scope=history").json()
        assert any(item["id"] == created["id"] for item in history)


def test_resume_appends_partial_file(tmp_path):
    dest = tmp_path / "file.part"
    dest.write_bytes(b"hello")

    async def chunks():
        yield b" world"

    written = asyncio.run(write_stream(chunks(), dest, Control(), lambda *_args: None, 11, 1000, 5))
    assert written == 11
    assert dest.read_bytes() == b"hello world"


def test_manager_pause_before_start(tmp_path):
    app_dir = tmp_path / "data"
    app_dir.mkdir()
    store = Store(app_dir / "infinityx.db")
    manager = Manager(store, app_dir)

    async def scenario():
        manager.bind(asyncio.get_running_loop())
        job = store.create_download({"url": "https://1.1.1.1/a.mp4", "media_type": "video", "format_id": "original", "direct": True, "title": "Queued"})
        manager._ensure(job["id"]).pause.set()
        manager.spawn(job["id"])
        await asyncio.wait_for(manager.tasks[job["id"]], timeout=3)
        return store.get(job["id"])["status"]

    assert asyncio.run(scenario()) == "paused"


def test_direct_public_download_when_network_allows(tmp_path):
    url = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"
    app = create_app(tmp_path / "net")
    with TestClient(app) as client:
        analyzed = client.post("/api/analyze", json={"url": url})
        if analyzed.status_code != 200 or not analyzed.json().get("direct"):
            pytest.skip(analyzed.text)
        created = client.post(
            "/api/downloads",
            json={"url": url, "format_id": "original", "media_type": "video", "convert_to": None},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        status = created.json()["status"]
        for _ in range(80):
            if status in {"completed", "failed", "cancelled"}:
                break
            import time

            time.sleep(0.5)
            rows = client.get("/api/downloads").json()
            match = next(item for item in rows if item["id"] == job_id)
            status = match["status"]
            if status == "downloading":
                assert "progress" in match
        if status != "completed":
            pytest.skip(f"sample download ended as {status}")
        media = client.get(f"/api/library/{job_id}/media")
        assert media.status_code == 200
        assert len(media.content) > 10000
        assert client.get("/api/stats").json()["completed"] >= 1
        assert client.get("/api/downloads?scope=history").json()[0]["status"] == "completed"
