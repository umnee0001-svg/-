"""업로드 작업 큐 (SQLite).

작업 하나 = 영상 하나 = 업로드 한 번. 상태 흐름은 이렇습니다.

    queued  ─(예약 시각 배정)→ scheduled ─(작업 시작)→ uploading
                                                        ├→ done
                                                        └→ failed ─(재시도)→ scheduled
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("queued", "scheduled", "uploading", "done", "failed", "canceled")

# 재시도 대상 상태 (uploading 은 앱이 중간에 죽은 경우라 복구 대상입니다)
RESUMABLE = ("uploading",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    TEXT    NOT NULL,
    video_path    TEXT    NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    hashtags      TEXT    NOT NULL DEFAULT '[]',   -- JSON 배열
    products      TEXT    NOT NULL DEFAULT '[]',   -- JSON 배열 (쇼핑 태그)
    visibility    TEXT    NOT NULL DEFAULT 'scheduled',
    publish_at    TEXT,                            -- ISO8601(+오프셋). NULL 이면 미배정
    status        TEXT    NOT NULL DEFAULT 'queued',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT    NOT NULL DEFAULT '',
    video_url     TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_channel  ON jobs(channel_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_publish  ON jobs(publish_at);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_job ON logs(job_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _row_to_job(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["hashtags"] = json.loads(job["hashtags"])
    job["products"] = json.loads(job["products"])
    return job


def add_job(conn: sqlite3.Connection, *, channel_id: str, video_path: str,
            title: str = "", description: str = "",
            hashtags: list[str] | None = None,
            products: list[str] | None = None,
            visibility: str = "scheduled",
            publish_at: str | None = None) -> int:
    """작업을 큐에 넣습니다. publish_at 을 주면 그 시각으로 고정, 없으면 스케줄러가 배정합니다."""
    stamp = now_iso()
    cur = conn.execute(
        """INSERT INTO jobs (channel_id, video_path, title, description, hashtags,
                             products, visibility, publish_at, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (channel_id, str(video_path), title, description,
         json.dumps(hashtags or [], ensure_ascii=False),
         json.dumps(products or [], ensure_ascii=False),
         visibility, publish_at,
         "scheduled" if publish_at else "queued", stamp, stamp),
    )
    job_id = int(cur.lastrowid)
    log(conn, job_id, f"큐에 추가: {Path(video_path).name}")
    return job_id


def update_job(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    if not fields:
        return
    allowed = {"channel_id", "video_path", "title", "description", "hashtags",
               "products", "visibility", "publish_at", "status", "attempts",
               "last_error", "video_url"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"수정할 수 없는 항목입니다: {sorted(unknown)}")
    for key in ("hashtags", "products"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?",
                 (*fields.values(), job_id))


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(conn: sqlite3.Connection, *, channel_id: str | None = None,
              status: str | None = None, limit: int = 500) -> list[dict]:
    sql = "SELECT * FROM jobs"
    where, args = [], []
    if channel_id:
        where.append("channel_id=?")
        args.append(channel_id)
    if status:
        where.append("status=?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    # 시각 미정(NULL)은 뒤로, 나머지는 예약 시각 순
    sql += " ORDER BY publish_at IS NULL, publish_at ASC, id ASC LIMIT ?"
    args.append(limit)
    return [_row_to_job(r) for r in conn.execute(sql, args).fetchall()]


def due_jobs(conn: sqlite3.Connection, *, before: str, limit: int = 20) -> list[dict]:
    """지금 올려야 할 작업. 예약 공개 시각보다 lead time 만큼 앞서 올립니다."""
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status='scheduled' AND publish_at IS NOT NULL AND publish_at <= ?
           ORDER BY publish_at ASC LIMIT ?""",
        (before, limit),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def taken_slots(conn: sqlite3.Connection, channel_id: str) -> list[str]:
    """해당 채널이 이미 잡아둔 예약 시각들 (끝난 것 포함, 취소/실패는 제외)."""
    rows = conn.execute(
        """SELECT publish_at FROM jobs
           WHERE channel_id=? AND publish_at IS NOT NULL
             AND status IN ('scheduled','uploading','done')
           ORDER BY publish_at ASC""",
        (channel_id,),
    ).fetchall()
    return [r["publish_at"] for r in rows]


def recover_stuck(conn: sqlite3.Connection) -> int:
    """앱이 업로드 도중 죽었으면 uploading 으로 남습니다. 다시 집어들 수 있게 되돌립니다."""
    cur = conn.execute(
        f"""UPDATE jobs SET status='scheduled', updated_at=?
            WHERE status IN ({','.join('?' * len(RESUMABLE))})""",
        (now_iso(), *RESUMABLE),
    )
    return cur.rowcount


def log(conn: sqlite3.Connection, job_id: int | None, message: str,
        level: str = "info") -> None:
    conn.execute(
        "INSERT INTO logs (job_id, level, message, created_at) VALUES (?,?,?,?)",
        (job_id, level, message, now_iso()),
    )


def list_logs(conn: sqlite3.Connection, *, job_id: int | None = None,
              limit: int = 200) -> list[dict]:
    if job_id is None:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM logs WHERE job_id=? ORDER BY id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
