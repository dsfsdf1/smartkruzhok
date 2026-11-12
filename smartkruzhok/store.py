import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

TTL = 30 * 60


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA secure_delete = ON;
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY,
                chat INTEGER NOT NULL,
                message INTEGER NOT NULL,
                state TEXT NOT NULL,
                expires REAL NOT NULL,
                UNIQUE (chat, message)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                clip TEXT NOT NULL,
                chat INTEGER NOT NULL,
                kind TEXT NOT NULL,
                dedupe TEXT NOT NULL UNIQUE,
                args TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                sent INTEGER NOT NULL DEFAULT 0,
                announced INTEGER NOT NULL DEFAULT 0,
                sending INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_job_per_chat ON jobs(chat)
                WHERE status IN ('queued', 'running');
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY,
                body TEXT NOT NULL,
                created INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback_prompts (
                chat INTEGER PRIMARY KEY,
                message INTEGER NOT NULL,
                expires REAL NOT NULL
            );
        """)
        with self.db:
            if not any(column["name"] == "cancel_requested" for column in self.db.execute("PRAGMA table_info(jobs)")):
                self.db.execute("ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
            self.db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")

    def uploaded(self, chat, message):
        return self.db.execute(
            "SELECT 1 FROM clips WHERE chat = ? AND message = ?", (chat, message),
        ).fetchone() is not None

    def upload(self, chat, message, file_id, progress=None):
        if self.uploaded(chat, message):
            return None
        clip_id = uuid4().hex[:16]
        with self.db:
            self.clear_prompts(chat)
            self.db.execute(
                "INSERT INTO clips VALUES (?, ?, ?, ?, ?)",
                (clip_id, chat, message, json.dumps({"revision": 0}), time.time() + TTL),
            )
            self._enqueue(clip_id, chat, "ingest", f"upload:{chat}:{message}",
                          {"file_id": file_id, "progress": progress})
        return clip_id

    def _enqueue(self, clip_id, chat, kind, key, args):
        self.db.execute(
            "INSERT INTO jobs(clip, chat, kind, dedupe, args, created) VALUES (?, ?, ?, ?, ?, ?)",
            (clip_id, chat, kind, key, json.dumps(args), time.time()),
        )

    def enqueue(self, clip, kind, key, args, *, revise=False):
        if self.enqueued(key):
            return False
        with self.db:
            self._enqueue(clip["id"], clip["chat"], kind, key, args)
            if revise:
                clip["state"]["revision"] += 1
                clip["state"].pop("prompt", None)
                self.db.execute("UPDATE clips SET state = ? WHERE id = ?",
                                (json.dumps(clip["state"]), clip["id"]))
        return True

    def enqueued(self, key):
        return self.db.execute("SELECT 1 FROM jobs WHERE dedupe = ?", (key,)).fetchone() is not None

    def clip(self, clip_id, chat=None, *, active=True):
        row = self.db.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if row is None or (chat is not None and row["chat"] != chat):
            return None
        if active and row["expires"] < time.time():
            return None
        return dict(row) | {"state": json.loads(row["state"])}

    def save(self, clip, *, refresh=False, new_prompt=False):
        expires = time.time() + TTL if refresh else clip["expires"]
        with self.db:
            if new_prompt:
                self.clear_prompts(clip["chat"])
            self.db.execute(
                "UPDATE clips SET state = ?, expires = ? WHERE id = ?",
                (json.dumps(clip["state"]), expires, clip["id"]),
            )
        clip["expires"] = expires

    def prompt(self, chat, message=None):
        row = self.db.execute("""
            SELECT id FROM clips
            WHERE chat = ? AND expires >= ? AND json_type(state, '$.prompt.message') = 'integer'
                AND (? IS NULL OR json_extract(state, '$.prompt.message') = ?)
            ORDER BY json_extract(state, '$.prompt.message') DESC LIMIT 1
        """, (chat, time.time(), message, message)).fetchone()
        return self.clip(row["id"], chat) if row else None

    def open_feedback(self, chat, message):
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM feedback_prompts WHERE expires < ?", (now,))
            self.db.execute("""
                INSERT INTO feedback_prompts (chat, message, expires) VALUES (?, ?, ?)
                ON CONFLICT(chat) DO UPDATE SET message = excluded.message, expires = excluded.expires
            """, (chat, message, now + TTL))

    def feedback_prompt(self, chat, message):
        return self.db.execute("""
            SELECT 1 FROM feedback_prompts WHERE chat = ? AND message = ? AND expires >= ?
        """, (chat, message, time.time())).fetchone() is not None

    def save_feedback(self, chat, message, body):
        now = time.time()
        with self.db:
            removed = self.db.execute("""
                DELETE FROM feedback_prompts WHERE chat = ? AND message = ? AND expires >= ?
            """, (chat, message, now)).rowcount
            if not removed:
                return False
            self.db.execute("INSERT INTO feedback (body, created) VALUES (?, ?)", (body, int(now)))
        return True

    def recent_feedback(self, limit, before=None):
        return self.db.execute("""
            SELECT id, body, created FROM feedback
            WHERE (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?
        """, (before, before, limit)).fetchall()

    def busy(self, chat):
        return self.active_job(chat) is not None

    def active_job(self, chat):
        return self.db.execute("""
            SELECT * FROM jobs WHERE chat = ? AND status IN ('queued', 'running')
        """, (chat,)).fetchone()

    def clear_prompts(self, chat):
        self.db.execute("""
            UPDATE clips SET state = json_remove(state, '$.prompt')
            WHERE chat = ? AND json_type(state, '$.prompt') IS NOT NULL
        """, (chat,))

    def reset_chat(self, chat):
        with self.db:
            self.clear_prompts(chat)
            job = self.active_job(chat)
            if job is not None:
                self._cancel(job)
        return job

    def take(self):
        with self.db:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (row["id"],))
        return dict(row) | {"args": json.loads(row["args"])}

    def job(self, job_id, clip_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id = ? AND clip = ?", (job_id, clip_id)).fetchone()
        return dict(row) | {"args": json.loads(row["args"])} if row else None

    def mark(self, job, *, status=None, sent=None, announced=None, sending=None, args=None):
        job.update({k: v for k, v in {
            "status": status, "sent": sent, "announced": announced, "sending": sending, "args": args,
        }.items() if v is not None})
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status = ?, sent = ?, announced = ?, sending = ?, args = ? WHERE id = ?",
                (job["status"], job["sent"], job["announced"], job["sending"],
                 json.dumps(job["args"]), job["id"]),
            )

    def retry(self, job):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status = 'queued', cancel_requested = 0 WHERE id = ? AND status = 'failed'",
                (job["id"],),
            )

    def request_cancel(self, chat, message):
        job = self.active_job(chat)
        if job is None or json.loads(job["args"]).get("progress") != message:
            return None
        with self.db:
            self._cancel(job)
        return job

    def _cancel(self, job):
        if job["status"] == "queued" and not job["sending"]:
            self.db.execute("UPDATE jobs SET status = 'cancelled', args = '{}' WHERE id = ?", (job["id"],))
        else:
            self.db.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job["id"],))

    def cancelling(self, job_id):
        return bool(self.db.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])

    def expired(self):
        rows = self.db.execute("""
            SELECT id FROM clips WHERE expires < ? AND state != '{}' AND NOT EXISTS (
                SELECT 1 FROM jobs WHERE jobs.clip = clips.id AND status IN ('queued', 'running')
            )
        """, (time.time(),)).fetchall()
        return [row["id"] for row in rows]

    def forget_media(self, clip_id):
        # Retain only the upload dedupe key, so late redelivery cannot start a new conversion.
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE clip = ?", (clip_id,))
            self.db.execute("UPDATE clips SET state = '{}' WHERE id = ?", (clip_id,))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self):
        self.db.close()
