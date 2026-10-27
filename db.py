# -*- coding: utf-8 -*-
"""数据层：SQLite 存储 + 口令散列 + 令牌签发（全部使用标准库）。"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS classes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    bind_code     TEXT,
    bind_expire   INTEGER DEFAULT 0,
    created_at    INTEGER
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    salt          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'teacher',
    class_id      INTEGER,
    created_at    INTEGER
);
CREATE TABLE IF NOT EXISTS videos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    url           TEXT,
    file_path     TEXT,
    file_size     INTEGER DEFAULT 0,
    duration      INTEGER DEFAULT 0,
    class_id      INTEGER,
    created_at    INTEGER,
    created_by    INTEGER
);
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id      INTEGER NOT NULL,
    weekday       INTEGER NOT NULL,
    start_time    TEXT NOT NULL,
    video_id      INTEGER NOT NULL,
    duration      INTEGER DEFAULT 0,
    enabled       INTEGER DEFAULT 1,
    note          TEXT,
    reminder      TEXT,
    updated_at    INTEGER,
    updated_by    INTEGER
);
CREATE TABLE IF NOT EXISTS homework (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id      INTEGER NOT NULL,
    content       TEXT NOT NULL,
    created_by    TEXT,
    created_at    INTEGER
);
CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    class_id        INTEGER NOT NULL,
    device_key      TEXT NOT NULL UNIQUE,
    secret          TEXT NOT NULL,
    online          INTEGER DEFAULT 0,
    last_seen       INTEGER DEFAULT 0,
    client_version  TEXT,
    current_plan_id INTEGER,
    cache_state     TEXT,
    exit_password   TEXT DEFAULT '8888',
    created_at      INTEGER
);
CREATE TABLE IF NOT EXISTS play_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     INTEGER NOT NULL,
    plan_id       INTEGER,
    video_id      INTEGER,
    play_date     TEXT NOT NULL,
    start_time    TEXT,
    status        TEXT,
    detail        TEXT,
    created_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_plans_class ON plans(class_id, weekday);
CREATE INDEX IF NOT EXISTS idx_logs_date ON play_logs(play_date, device_id);
"""


# ----------------------------------------------------------------- 口令 / 令牌
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), 100000)
    return salt, dk.hex()


def verify_password(password, salt, expected_hash):
    _, calc = hash_password(password, salt)
    return hmac.compare_digest(calc, expected_hash)


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def make_token(secret, payload, ttl=7 * 24 * 3600):
    payload = dict(payload)
    payload["exp"] = int(time.time()) + ttl
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return body + "." + sig


def parse_token(secret, token):
    if not token or "." not in token:
        return None
    body, sig = token.split(".", 1)
    expect = _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (ValueError, KeyError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def new_secret():
    return secrets.token_hex(24)


# ------------------------------------------------------------------------ DB
class DB(object):
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()
        self.seed()

    def init_schema(self):
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self):
        """对老数据库做增量升级（新增列不存在时才 ALTER）。"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(plans)").fetchall()]
        if "reminder" not in cols:
            self.conn.execute("ALTER TABLE plans ADD COLUMN reminder TEXT")

    def seed(self):
        """首次启动写入默认班级与超级管理员。"""
        with self.lock:
            cur = self.conn.execute("SELECT COUNT(*) AS c FROM classes")
            if cur.fetchone()["c"] == 0:
                self.conn.execute(
                    "INSERT INTO classes(name, created_at) VALUES(?, ?)", ("默认班级", int(time.time()))
                )
            cur = self.conn.execute("SELECT COUNT(*) AS c FROM users")
            if cur.fetchone()["c"] == 0:
                salt, pwd = hash_password("admin123")
                self.conn.execute(
                    "INSERT INTO users(username, salt, password_hash, role, class_id, created_at)"
                    " VALUES(?, ?, ?, 'admin', NULL, ?)",
                    ("admin", salt, pwd, int(time.time())),
                )
            self.conn.commit()

    # ------------------------------------------------------------ helpers
    def query(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def execute_many(self, sql, seq):
        with self.lock:
            cur = self.conn.executemany(sql, seq)
            self.conn.commit()
            return cur.rowcount
