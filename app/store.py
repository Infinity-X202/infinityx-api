import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    FIELDS = {
        "url",
        "platform",
        "title",
        "creator",
        "thumbnail",
        "media_type",
        "format_id",
        "convert_to",
        "status",
        "progress",
        "speed",
        "eta",
        "bytes_done",
        "bytes_total",
        "filepath",
        "filename",
        "error",
        "direct",
        "removed",
        "metadata",
        "template",
        "updated_at",
        "finished_at",
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    platform TEXT,
                    title TEXT,
                    creator TEXT,
                    thumbnail TEXT,
                    media_type TEXT,
                    format_id TEXT,
                    convert_to TEXT,
                    status TEXT NOT NULL,
                    progress REAL DEFAULT 0,
                    speed REAL,
                    eta INTEGER,
                    bytes_done INTEGER DEFAULT 0,
                    bytes_total INTEGER,
                    filepath TEXT,
                    filename TEXT,
                    error TEXT,
                    direct INTEGER DEFAULT 0,
                    removed INTEGER DEFAULT 0,
                    metadata TEXT,
                    template TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            defaults = {
                "max_concurrent": "2",
                "max_bytes": str(1536 * 1024 * 1024),
                "filename_template": "{title}-{id}.{ext}",
                "notify": "true",
                "max_minutes": "90",
            }
            for key, value in defaults.items():
                self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
            self.conn.commit()

    def settings(self) -> dict:
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        raw = {row["key"]: row["value"] for row in rows}
        return {
            "max_concurrent": int(raw.get("max_concurrent", "2")),
            "max_bytes": int(raw.get("max_bytes", str(1536 * 1024 * 1024))),
            "filename_template": raw.get("filename_template", "{title}-{id}.{ext}"),
            "notify": raw.get("notify", "true") == "true",
            "max_minutes": int(raw.get("max_minutes", "90")),
        }

    def save_settings(self, values: dict) -> dict:
        mapping = {
            "max_concurrent": str(values["max_concurrent"]),
            "max_bytes": str(values["max_bytes"]),
            "filename_template": values["filename_template"],
            "notify": "true" if values["notify"] else "false",
            "max_minutes": str(values["max_minutes"]),
        }
        with self._lock:
            for key, value in mapping.items():
                self.conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            self.conn.commit()
        return self.settings()

    def create_download(self, fields: dict) -> dict:
        now = utcnow()
        job_id = uuid.uuid4().hex
        record = {
            "id": job_id,
            "url": fields["url"],
            "platform": fields.get("platform"),
            "title": fields.get("title") or "Untitled",
            "creator": fields.get("creator") or "",
            "thumbnail": fields.get("thumbnail"),
            "media_type": fields["media_type"],
            "format_id": fields["format_id"],
            "convert_to": fields.get("convert_to"),
            "status": "queued",
            "progress": 0,
            "speed": None,
            "eta": None,
            "bytes_done": 0,
            "bytes_total": fields.get("bytes_total"),
            "filepath": None,
            "filename": None,
            "error": None,
            "direct": 1 if fields.get("direct") else 0,
            "removed": 0,
            "metadata": None,
            "template": fields.get("template"),
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        columns = ", ".join(record)
        marks = ", ".join("?" for _ in record)
        with self._lock:
            self.conn.execute(f"INSERT INTO downloads ({columns}) VALUES ({marks})", tuple(record.values()))
            self.conn.commit()
        return self.get(job_id)

    def create_library_file(self, fields: dict) -> dict:
        now = utcnow()
        job_id = uuid.uuid4().hex
        record = {
            "id": job_id,
            "url": fields.get("url") or "local",
            "platform": fields.get("platform") or "local",
            "title": fields.get("title") or "Audio",
            "creator": fields.get("creator") or "",
            "thumbnail": None,
            "media_type": fields.get("media_type") or "audio",
            "format_id": fields.get("format_id") or "local",
            "convert_to": fields.get("convert_to"),
            "status": "completed",
            "progress": 100,
            "speed": None,
            "eta": None,
            "bytes_done": fields.get("bytes_done") or 0,
            "bytes_total": fields.get("bytes_total"),
            "filepath": fields["filepath"],
            "filename": fields["filename"],
            "error": None,
            "direct": 1,
            "removed": 0,
            "metadata": fields.get("metadata"),
            "template": fields.get("template"),
            "created_at": now,
            "updated_at": now,
            "finished_at": now,
        }
        columns = ", ".join(record)
        marks = ", ".join("?" for _ in record)
        with self._lock:
            self.conn.execute(f"INSERT INTO downloads ({columns}) VALUES ({marks})", tuple(record.values()))
            self.conn.commit()
        return self.get(job_id)

    def update(self, job_id: str, **fields) -> dict:
        unknown = set(fields) - self.FIELDS
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE downloads SET {assignments} WHERE id=?",
                (*fields.values(), job_id),
            )
            self.conn.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, scope: str) -> list[dict]:
        queries = {
            "all": "SELECT * FROM downloads ORDER BY created_at DESC LIMIT 300",
            "queue": "SELECT * FROM downloads WHERE status IN ('queued','downloading','paused') AND removed=0 ORDER BY created_at ASC",
            "history": "SELECT * FROM downloads WHERE status IN ('completed','failed','cancelled') ORDER BY COALESCE(finished_at, updated_at) DESC LIMIT 300",
            "library": "SELECT * FROM downloads WHERE status='completed' AND removed=0 AND filepath IS NOT NULL ORDER BY finished_at DESC LIMIT 300",
        }
        if scope not in queries:
            raise KeyError(scope)
        with self._lock:
            rows = self.conn.execute(queries[scope]).fetchall()
        return [dict(row) for row in rows]

    def mark_interrupted(self) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE downloads SET status='paused', updated_at=? WHERE status='downloading'",
                (utcnow(),),
            )
            self.conn.commit()

    def counts(self) -> dict:
        with self._lock:
            completed = self.conn.execute("SELECT COUNT(*) AS n FROM downloads WHERE status='completed'").fetchone()["n"]
            failed = self.conn.execute("SELECT COUNT(*) AS n FROM downloads WHERE status='failed'").fetchone()["n"]
            active = self.conn.execute(
                "SELECT COUNT(*) AS n FROM downloads WHERE status IN ('queued','downloading','paused') AND removed=0"
            ).fetchone()["n"]
            paths = self.conn.execute(
                "SELECT filepath FROM downloads WHERE status='completed' AND removed=0 AND filepath IS NOT NULL"
            ).fetchall()
        storage = 0
        for row in paths:
            file_path = Path(row["filepath"])
            if file_path.is_file():
                storage += file_path.stat().st_size
        return {
            "completed": completed,
            "failed": failed,
            "active": active,
            "storage_bytes": storage,
        }
