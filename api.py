# -*- coding: utf-8 -*-
"""REST API 路由层（教师端 + 学生端设备接口）。"""
import calendar
import datetime
import json
import os
import re
import secrets
import time
import urllib.parse

import db as dbmod

MAX_JSON_BODY = 32 * 1024 * 1024  # 32MB


class HttpError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


class RawResponse(object):
    def __init__(self, body, content_type="application/octet-stream", status=200, headers=None):
        self.body = body
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}


class FileResponse(object):
    """由 server 层做 Range / 流式输出。"""

    def __init__(self, path, content_type="application/octet-stream", filename=None, no_cache=False):
        self.path = path
        self.content_type = content_type
        self.filename = filename
        # 网页资源（html/js/css）必须禁用缓存：否则升级后教师的浏览器仍用旧版脚本
        self.no_cache = no_cache


class Request(object):
    def __init__(self, handler, method, path, query, ctx):
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.ctx = ctx
        self._body = None
        self.fields = {}

    @property
    def headers(self):
        return self.handler.headers

    def content_length(self):
        try:
            return int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return 0

    def read_body(self, limit=MAX_JSON_BODY):
        if self._body is None:
            n = self.content_length()
            if n > limit:
                raise HttpError(413, "请求体过大")
            buf = bytearray()
            remaining = n
            while remaining > 0:
                chunk = self.handler.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                buf.extend(chunk)
                remaining -= len(chunk)
            self._body = bytes(buf)
        return self._body

    def json(self):
        raw = self.read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise HttpError(400, "JSON 解析失败")

    def arg(self, name, default=None):
        vals = self.query.get(name)
        return vals[0] if vals else default

    def user(self):
        """解析教师端 token（Header 或 query 参数）。"""
        token = self.headers.get("X-Auth-Token") or self.arg("t")
        payload = dbmod.parse_token(self.ctx["secret"], token)
        if not payload:
            raise HttpError(401, "未登录或登录已过期")
        row = self.ctx["db"].query_one("SELECT * FROM users WHERE id=?", (payload.get("uid"),))
        if not row:
            raise HttpError(401, "账号不存在")
        return row

    def device(self):
        key = self.headers.get("X-Device-Key") or self.arg("d")
        sec = self.headers.get("X-Device-Secret") or self.arg("k")
        row = self.ctx["db"].query_one(
            "SELECT * FROM devices WHERE device_key=? AND secret=?", (key or "", sec or "")
        )
        if not row:
            raise HttpError(401, "设备未绑定或凭证无效")
        return row


# ------------------------------------------------------------------ 路由表
ROUTES = []


def route(method, pattern):
    compiled = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, compiled, fn))
        return fn

    return deco


def dispatch(ctx, req):
    for method, pattern, fn in ROUTES:
        if method != req.method:
            continue
        m = pattern.match(req.path)
        if not m:
            continue
        result = fn(ctx, req, **m.groupdict())
        return result
    raise HttpError(404, "接口不存在：%s %s" % (req.method, req.path))


# --------------------------------------------------------------- 工具函数
def now_ts():
    return int(time.time())


def today_str():
    return datetime.date.today().strftime("%Y-%m-%d")


def weekday_monday1(d=None):
    """返回 1(周一) .. 7(周日)。"""
    d = d or datetime.date.today()
    return d.weekday() + 1


def public_base(ctx, req):
    base = ctx["config"].get("public_base") or ""
    if base:
        return base.rstrip("/")
    host = req.headers.get("Host")
    if not host:
        host = "%s:%s" % (ctx["config"].get("host", "127.0.0.1"), ctx["config"]["port"])
    return "http://" + host


def video_public(ctx, req, video):
    """把数据库中的视频记录转成对外的结构（含设备可用的下载地址）。"""
    base = public_base(ctx, req)
    item = {
        "id": video["id"],
        "title": video["title"],
        "kind": video["kind"],
        "url": video["url"],
        "size": video["file_size"],
        "duration": video["duration"],
    }
    if video["kind"] == "local":
        # 设备下载地址：带设备凭证
        item["download_url"] = base + "/media/%d?d=__DEVICE_KEY__&k=__DEVICE_SECRET__" % video["id"]
        item["play_url"] = base + "/media/%d" % video["id"]
    else:
        item["download_url"] = video["url"]
        item["play_url"] = video["url"]
    return item


def log_event(ctx, kind, data):
    ctx["hub"].broadcast_teachers({"type": kind, "data": data})


# ============================================================== 认证 / 账号
@route("POST", r"/api/login")
def login(ctx, req):
    body = req.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = ctx["db"].query_one("SELECT * FROM users WHERE username=?", (username,))
    if not user or not dbmod.verify_password(password, user["salt"], user["password_hash"]):
        raise HttpError(401, "用户名或密码错误")
    token = dbmod.make_token(ctx["secret"], {"uid": user["id"], "role": user["role"]})
    return {"token": token, "user": safe_user(user)}


def safe_user(user):
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "class_id": user["class_id"],
    }


@route("GET", r"/api/me")
def me(ctx, req):
    user = req.user()
    return {"user": safe_user(user)}


# ==================================================================== 班级
@route("GET", r"/api/classes")
def list_classes(ctx, req):
    user = req.user()
    rows = ctx["db"].query(
        "SELECT c.*, (SELECT COUNT(*) FROM devices d WHERE d.class_id=c.id) AS device_count,"
        " (SELECT COUNT(*) FROM plans p WHERE p.class_id=c.id) AS plan_count,"
        " (SELECT COUNT(*) FROM videos v WHERE v.class_id=c.id) AS video_count"
        " FROM classes c ORDER BY c.id"
    )
    if user["role"] != "admin" and user["class_id"]:
        rows = [r for r in rows if r["id"] == user["class_id"]]
    for r in rows:
        r["bind_code"] = None
        r["bind_expire"] = r["bind_expire"] or 0
    return {"items": rows}


@route("POST", r"/api/classes")
def create_class(ctx, req):
    user = req.user()
    if user["role"] != "admin":
        raise HttpError(403, "仅管理员可创建班级")
    body = req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HttpError(400, "班级名称不能为空")
    try:
        cid = ctx["db"].execute(
            "INSERT INTO classes(name, created_at) VALUES(?, ?)", (name, now_ts())
        )
    except Exception:
        raise HttpError(400, "班级已存在")
    return {"id": cid, "name": name}


@route("DELETE", r"/api/classes/(?P<cid>\d+)")
def delete_class(ctx, req, cid):
    user = req.user()
    if user["role"] != "admin":
        raise HttpError(403, "仅管理员可删除班级")
    cid = int(cid)
    ctx["db"].execute("DELETE FROM plans WHERE class_id=?", (cid,))
    ctx["db"].execute("DELETE FROM homework WHERE class_id=?", (cid,))
    ctx["db"].execute("DELETE FROM devices WHERE class_id=?", (cid,))
    ctx["db"].execute("DELETE FROM classes WHERE id=?", (cid,))
    ctx["db"].execute("UPDATE users SET class_id=NULL WHERE class_id=?", (cid,))
    return {"ok": True}


@route("POST", r"/api/classes/(?P<cid>\d+)/bind-code")
def make_bind_code(ctx, req, cid):
    user = req.user()
    require_class_access(user, int(cid))
    # 客户端可能在 POST 中带请求体；必须读掉，否则 keep-alive 连接上下一个请求会被污染
    req.read_body()
    code = "".join(secrets.choice("0123456789") for _ in range(8))
    expire = now_ts() + 30 * 60
    ctx["db"].execute("UPDATE classes SET bind_code=?, bind_expire=? WHERE id=?", (code, expire, int(cid)))
    return {"code": code, "expire": expire, "ttl": 1800}


def require_class_access(user, class_id):
    if user["role"] == "admin":
        return
    if user["class_id"] and int(user["class_id"]) == int(class_id):
        return
    raise HttpError(403, "无权操作该班级")


# ==================================================================== 账号
@route("GET", r"/api/users")
def list_users(ctx, req):
    user = req.user()
    if user["role"] != "admin":
        raise HttpError(403, "仅管理员可查看账号列表")
    rows = ctx["db"].query(
        "SELECT u.id, u.username, u.role, u.class_id, c.name AS class_name"
        " FROM users u LEFT JOIN classes c ON c.id=u.class_id ORDER BY u.id"
    )
    return {"items": rows}


@route("POST", r"/api/users")
def create_user(ctx, req):
    user = req.user()
    if user["role"] != "admin":
        raise HttpError(403, "仅管理员可创建账号")
    body = req.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or len(password) < 6:
        raise HttpError(400, "用户名不能为空，密码至少 6 位")
    role = body.get("role") if body.get("role") in ("admin", "teacher") else "teacher"
    salt, pwd = dbmod.hash_password(password)
    try:
        cid = ctx["db"].execute(
            "INSERT INTO users(username, salt, password_hash, role, class_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (username, salt, pwd, role, body.get("class_id"), now_ts()),
        )
    except Exception:
        raise HttpError(400, "用户名已存在")
    return {"id": cid}


@route("PUT", r"/api/users/(?P<uid>\d+)")
def update_user(ctx, req, uid):
    user = req.user()
    if user["role"] != "admin":
        raise HttpError(403, "仅管理员可修改账号")
    body = req.json()
    uid = int(uid)
    if body.get("password"):
        if len(body["password"]) < 6:
            raise HttpError(400, "密码至少 6 位")
        salt, pwd = dbmod.hash_password(body["password"])
        ctx["db"].execute("UPDATE users SET salt=?, password_hash=? WHERE id=?", (salt, pwd, uid))
    if "class_id" in body:
        ctx["db"].execute("UPDATE users SET class_id=? WHERE id=?", (body.get("class_id"), uid))
    if body.get("role") in ("admin", "teacher"):
        ctx["db"].execute("UPDATE users SET role=? WHERE id=?", (body["role"], uid))
    return {"ok": True}


@route("DELETE", r"/api/users/(?P<uid>\d+)")
def delete_user(ctx, req, uid):
    user = req.user()
    if user["role"] != "admin":
        raise HttpError(403, "仅管理员可删除账号")
    if int(uid) == int(user["id"]):
        raise HttpError(400, "不能删除自己")
    ctx["db"].execute("DELETE FROM users WHERE id=?", (int(uid),))
    return {"ok": True}


# ==================================================================== 视频
@route("GET", r"/api/videos")
def list_videos(ctx, req):
    user = req.user()
    class_id = req.arg("class_id")
    sql = (
        "SELECT v.*, c.name AS class_name FROM videos v LEFT JOIN classes c ON c.id=v.class_id"
    )
    args = []
    if class_id:
        sql += " WHERE v.class_id IS NULL OR v.class_id=?"
        args.append(int(class_id))
    elif user["role"] != "admin" and user["class_id"]:
        sql += " WHERE v.class_id IS NULL OR v.class_id=?"
        args.append(int(user["class_id"]))
    sql += " ORDER BY v.id DESC"
    rows = ctx["db"].query(sql, tuple(args))
    return {"items": rows}


EXT_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}


@route("PUT", r"/api/videos/upload")
def upload_video(ctx, req):
    """原始请求体上传（前端用 fetch body 直接传 File，避免 multipart 解析开销）。"""
    user = req.user()
    title = (req.arg("title") or "").strip()
    filename = req.headers.get("X-File-Name") or req.arg("filename") or "video.mp4"
    filename = urllib.parse.unquote(filename)
    filename = os.path.basename(filename.replace("\\", "/"))
    ext = os.path.splitext(filename)[1].lower()
    if not title:
        title = os.path.splitext(filename)[0]
    class_id = req.arg("class_id")
    class_id = int(class_id) if class_id else None
    if class_id:
        require_class_access(user, class_id)

    uid = secrets.token_hex(8) + ext
    target = os.path.join(ctx["videos_dir"], uid)
    total = req.content_length()
    if total <= 0:
        raise HttpError(400, "文件为空")
    written = 0
    with open(target, "wb") as fp:
        while written < total:
            chunk = req.handler.rfile.read(min(1024 * 256, total - written))
            if not chunk:
                break
            fp.write(chunk)
            written += len(chunk)
    if written != total:
        try:
            os.remove(target)
        except OSError:
            pass
        raise HttpError(400, "上传中断，文件不完整")

    vid = ctx["db"].execute(
        "INSERT INTO videos(title, kind, file_path, file_size, class_id, created_at, created_by)"
        " VALUES(?, 'local', ?, ?, ?, ?, ?)",
        (title, os.path.relpath(target, ctx["root"]).replace("\\", "/"), written,
         class_id, now_ts(), user["id"]),
    )
    log_event(ctx, "videos_changed", {"class_id": class_id})
    return {"id": vid, "title": title, "size": written, "kind": "local"}


@route("POST", r"/api/videos/link")
def add_link_video(ctx, req):
    user = req.user()
    body = req.json()
    title = (body.get("title") or "").strip()
    url = (body.get("url") or "").strip()
    if not title or not url:
        raise HttpError(400, "标题和链接不能为空")
    if not re.match(r"^https?://", url):
        raise HttpError(400, "链接必须以 http:// 或 https:// 开头")
    class_id = body.get("class_id")
    class_id = int(class_id) if class_id else None
    if class_id:
        require_class_access(user, class_id)
    vid = ctx["db"].execute(
        "INSERT INTO videos(title, kind, url, duration, class_id, created_at, created_by)"
        " VALUES(?, 'url', ?, ?, ?, ?, ?)",
        (title, url, int(body.get("duration") or 0), class_id, now_ts(), user["id"]),
    )
    return {"id": vid}


@route("PUT", r"/api/videos/(?P<vid>\d+)")
def update_video(ctx, req, vid):
    req.user()
    body = req.json()
    sets, args = [], []
    for key in ("title", "duration", "class_id"):
        if key in body:
            sets.append("%s=?" % key)
            args.append(body[key])
    if not sets:
        return {"ok": True}
    args.append(int(vid))
    ctx["db"].execute("UPDATE videos SET %s WHERE id=?" % ",".join(sets), tuple(args))
    return {"ok": True}


@route("DELETE", r"/api/videos/(?P<vid>\d+)")
def delete_video(ctx, req, vid):
    req.user()
    vid = int(vid)
    row = ctx["db"].query_one("SELECT * FROM videos WHERE id=?", (vid,))
    if not row:
        raise HttpError(404, "视频不存在")
    used = ctx["db"].query_one("SELECT COUNT(*) AS c FROM plans WHERE video_id=?", (vid,))
    if used and used["c"]:
        raise HttpError(400, "该视频已被 %d 条播放计划引用，无法删除" % used["c"])
    if row["file_path"]:
        try:
            os.remove(os.path.join(ctx["root"], row["file_path"]))
        except OSError:
            pass
    ctx["db"].execute("DELETE FROM videos WHERE id=?", (vid,))
    return {"ok": True}


# ==================================================================== 计划
def plan_rows(ctx, class_id=None, weekday=None):
    sql = (
        "SELECT p.*, v.title AS video_title, v.kind AS video_kind, v.url AS video_url,"
        " v.file_size AS video_size, v.duration AS video_duration, c.name AS class_name"
        " FROM plans p JOIN videos v ON v.id=p.video_id LEFT JOIN classes c ON c.id=p.class_id"
    )
    where, args = [], []
    if class_id:
        where.append("p.class_id=?")
        args.append(int(class_id))
    if weekday:
        where.append("p.weekday=?")
        args.append(int(weekday))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.weekday, p.start_time, p.id"
    return ctx["db"].query(sql, tuple(args))


@route("GET", r"/api/plans")
def get_plans(ctx, req):
    user = req.user()
    class_id = req.arg("class_id") or (user["class_id"] if user["role"] != "admin" else None)
    return {"items": plan_rows(ctx, class_id, req.arg("weekday"))}


def _validate_plan_body(ctx, body, user):
    class_id = body.get("class_id") or user["class_id"]
    if not class_id:
        raise HttpError(400, "缺少班级")
    class_id = int(class_id)
    require_class_access(user, class_id)
    weekday = int(body.get("weekday") or 0)
    if weekday < 1 or weekday > 5:
        raise HttpError(400, "weekday 必须为 1-5（周一至周五）")
    start_time = (body.get("start_time") or "").strip()
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", start_time):
        raise HttpError(400, "时间格式必须为 HH:MM")
    video_id = body.get("video_id")
    if not video_id and body.get("video_title"):
        v = ctx["db"].query_one("SELECT id FROM videos WHERE title=?", (body["video_title"],))
        if not v:
            raise HttpError(400, "找不到视频：%s" % body["video_title"])
        video_id = v["id"]
    video_id = int(video_id or 0)
    if not ctx["db"].query_one("SELECT id FROM videos WHERE id=?", (video_id,)):
        raise HttpError(400, "视频不存在")
    reminder = (body.get("reminder") or "").strip()[:200]
    return (class_id, weekday, start_time, video_id, int(body.get("duration") or 0),
            body.get("note") or "", reminder)


@route("POST", r"/api/plans")
def create_plan(ctx, req):
    user = req.user()
    body = req.json()
    class_id, weekday, start_time, video_id, duration, note, reminder = _validate_plan_body(ctx, body, user)
    pid = ctx["db"].execute(
        "INSERT INTO plans(class_id, weekday, start_time, video_id, duration, enabled, note, reminder,"
        " updated_at, updated_by) VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (class_id, weekday, start_time, video_id, duration, note, reminder, now_ts(), user["id"]),
    )
    notify_plan_change(ctx, class_id)
    return {"id": pid}


@route("POST", r"/api/plans/batch")
def batch_create(ctx, req):
    """批量导入：items 中可用 video_id 或 video_title 指定视频。"""
    user = req.user()
    body = req.json()
    items = body.get("items") or []
    if not items:
        raise HttpError(400, "items 不能为空")
    created, errors, class_ids = [], [], set()
    for idx, raw in enumerate(items):
        try:
            class_id, weekday, start_time, video_id, duration, note, reminder = _validate_plan_body(ctx, raw, user)
            pid = ctx["db"].execute(
                "INSERT INTO plans(class_id, weekday, start_time, video_id, duration, enabled, note, reminder,"
                " updated_at, updated_by) VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (class_id, weekday, start_time, video_id, duration, note, reminder, now_ts(), user["id"]),
            )
            created.append(pid)
            class_ids.add(class_id)
        except HttpError as exc:
            errors.append({"index": idx, "message": exc.message})
    for cid in class_ids:
        notify_plan_change(ctx, cid)
    return {"created": len(created), "errors": errors}


@route("PUT", r"/api/plans/(?P<pid>\d+)")
def update_plan(ctx, req, pid):
    user = req.user()
    body = req.json()
    pid = int(pid)
    row = ctx["db"].query_one("SELECT * FROM plans WHERE id=?", (pid,))
    if not row:
        raise HttpError(404, "计划不存在")
    require_class_access(user, row["class_id"])
    merged = dict(row)
    merged.update(body)
    merged["class_id"] = row["class_id"]
    class_id, weekday, start_time, video_id, duration, note, reminder = _validate_plan_body(ctx, merged, user)
    enabled = 1 if body.get("enabled", row["enabled"]) else 0
    ctx["db"].execute(
        "UPDATE plans SET weekday=?, start_time=?, video_id=?, duration=?, enabled=?, note=?, reminder=?,"
        " updated_at=?, updated_by=? WHERE id=?",
        (weekday, start_time, video_id, duration, enabled, note, reminder, now_ts(), user["id"], pid),
    )
    notify_plan_change(ctx, class_id)
    return {"ok": True}


@route("DELETE", r"/api/plans/(?P<pid>\d+)")
def delete_plan(ctx, req, pid):
    user = req.user()
    row = ctx["db"].query_one("SELECT * FROM plans WHERE id=?", (int(pid),))
    if not row:
        return {"ok": True}
    require_class_access(user, row["class_id"])
    ctx["db"].execute("DELETE FROM plans WHERE id=?", (int(pid),))
    notify_plan_change(ctx, row["class_id"])
    return {"ok": True}


@route("POST", r"/api/plans/clear")
def clear_plans(ctx, req):
    user = req.user()
    body = req.json()
    class_id = int(body.get("class_id") or user["class_id"] or 0)
    if not class_id:
        raise HttpError(400, "缺少班级")
    require_class_access(user, class_id)
    weekday = body.get("weekday")
    if weekday:
        ctx["db"].execute("DELETE FROM plans WHERE class_id=? AND weekday=?", (class_id, int(weekday)))
    else:
        ctx["db"].execute("DELETE FROM plans WHERE class_id=?", (class_id,))
    notify_plan_change(ctx, class_id)
    return {"ok": True}


def notify_plan_change(ctx, class_id):
    ctx["hub"].broadcast_devices_of_class(class_id, {"type": "schedule_changed"})
    log_event(ctx, "plans_changed", {"class_id": class_id})


# ==================================================================== 作业通知
@route("GET", r"/api/homework")
def list_homework(ctx, req):
    user = req.user()
    class_id = req.arg("class_id") or (user["class_id"] if user["role"] != "admin" else None)
    sql = "SELECT h.*, c.name AS class_name FROM homework h LEFT JOIN classes c ON c.id=h.class_id"
    args = []
    if class_id:
        sql += " WHERE h.class_id=?"
        args.append(int(class_id))
    sql += " ORDER BY h.id DESC LIMIT 200"
    return {"items": ctx["db"].query(sql, tuple(args))}


@route("POST", r"/api/homework")
def create_homework(ctx, req):
    user = req.user()
    body = req.json()
    class_id = int(body.get("class_id") or user["class_id"] or 0)
    if not class_id:
        raise HttpError(400, "缺少班级")
    require_class_access(user, class_id)
    content = (body.get("content") or "").strip()
    if not content:
        raise HttpError(400, "作业内容不能为空")
    hid = ctx["db"].execute(
        "INSERT INTO homework(class_id, content, created_by, created_at) VALUES(?, ?, ?, ?)",
        (class_id, content[:500], user["username"], now_ts()),
    )
    notify_homework_change(ctx, class_id)
    return {"id": hid}


@route("DELETE", r"/api/homework/(?P<hid>\d+)")
def delete_homework(ctx, req, hid):
    user = req.user()
    row = ctx["db"].query_one("SELECT * FROM homework WHERE id=?", (int(hid),))
    if not row:
        return {"ok": True}
    require_class_access(user, row["class_id"])
    ctx["db"].execute("DELETE FROM homework WHERE id=?", (int(hid),))
    notify_homework_change(ctx, row["class_id"])
    return {"ok": True}


def notify_homework_change(ctx, class_id):
    ctx["hub"].broadcast_devices_of_class(class_id, {"type": "homework_changed"})
    log_event(ctx, "homework_changed", {"class_id": class_id})


# ==================================================================== 设备
@route("GET", r"/api/devices")
def list_devices(ctx, req):
    user = req.user()
    class_id = req.arg("class_id") or (user["class_id"] if user["role"] != "admin" else None)
    sql = (
        "SELECT d.*, c.name AS class_name FROM devices d LEFT JOIN classes c ON c.id=d.class_id"
    )
    args = []
    if class_id:
        sql += " WHERE d.class_id=?"
        args.append(int(class_id))
    sql += " ORDER BY d.id"
    rows = ctx["db"].query(sql, tuple(args))
    now = now_ts()
    for r in rows:
        r["online"] = 1 if (r["last_seen"] and now - r["last_seen"] < 90) else 0
        try:
            r["cache_state"] = json.loads(r["cache_state"] or "{}")
        except ValueError:
            r["cache_state"] = {}
    return {"items": rows}


@route("POST", r"/api/devices/(?P<did>\d+)/password")
def set_device_password(ctx, req, did):
    user = req.user()
    did = int(did)
    row = ctx["db"].query_one("SELECT * FROM devices WHERE id=?", (did,))
    if not row:
        raise HttpError(404, "设备不存在")
    require_class_access(user, row["class_id"])
    body = req.json()
    password = (body.get("password") or "").strip()
    if len(password) < 4:
        raise HttpError(400, "退出密码至少 4 位")
    ctx["db"].execute("UPDATE devices SET exit_password=? WHERE id=?", (password, did))
    ctx["hub"].broadcast_device(row["device_key"], {"type": "config_changed"})
    return {"ok": True}


@route("POST", r"/api/devices/(?P<did>\d+)/rename")
def rename_device(ctx, req, did):
    user = req.user()
    did = int(did)
    row = ctx["db"].query_one("SELECT * FROM devices WHERE id=?", (did,))
    if not row:
        raise HttpError(404, "设备不存在")
    require_class_access(user, row["class_id"])
    body = req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HttpError(400, "名称不能为空")
    ctx["db"].execute("UPDATE devices SET name=? WHERE id=?", (name, did))
    return {"ok": True}


@route("DELETE", r"/api/devices/(?P<did>\d+)")
def delete_device(ctx, req, did):
    user = req.user()
    did = int(did)
    row = ctx["db"].query_one("SELECT * FROM devices WHERE id=?", (did,))
    if not row:
        return {"ok": True}
    require_class_access(user, row["class_id"])
    ctx["db"].execute("DELETE FROM devices WHERE id=?", (did,))
    log_event(ctx, "devices_changed", {"class_id": row["class_id"]})
    return {"ok": True}


# ============================================================ 设备端（学生端）
@route("POST", r"/api/device/register")
def device_register(ctx, req):
    body = req.json()
    code = (body.get("code") or "").strip()
    name = (body.get("name") or "").strip() or "未命名设备"
    version = (body.get("version") or "").strip()
    cls = ctx["db"].query_one("SELECT * FROM classes WHERE bind_code=?", (code,))
    if not cls or not cls["bind_code"] or (cls["bind_expire"] or 0) < now_ts():
        raise HttpError(400, "绑定码无效或已过期，请在教师端重新生成")
    key = dbmod.new_secret()
    secret = dbmod.new_secret()
    did = ctx["db"].execute(
        "INSERT INTO devices(name, class_id, device_key, secret, client_version, last_seen, created_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?)",
        (name, cls["id"], key, secret, version, now_ts(), now_ts()),
    )
    log_event(ctx, "devices_changed", {"class_id": cls["id"]})
    return {
        "device_id": did,
        "device_key": key,
        "device_secret": secret,
        "class_id": cls["id"],
        "class_name": cls["name"],
        "server_time": now_ts(),
        "exit_password": "8888",
    }


@route("GET", r"/api/device/schedule")
def device_schedule(ctx, req):
    dev = req.device()
    ctx["db"].execute("UPDATE devices SET last_seen=?, online=1 WHERE id=?", (now_ts(), dev["id"]))
    plans = plan_rows(ctx, dev["class_id"])
    out = []
    for p in plans:
        if not p["enabled"]:
            continue
        video = ctx["db"].query_one("SELECT * FROM videos WHERE id=?", (p["video_id"],))
        if not video:
            continue
        item = video_public(ctx, req, video)
        out.append({
            "id": p["id"],
            "weekday": p["weekday"],
            "start_time": p["start_time"],
            "duration": p["duration"] or video["duration"] or 0,
            "note": p["note"] or "",
            "reminder": p.get("reminder") or "",
            "video": item,
        })
    hw = ctx["db"].query(
        "SELECT id, content, created_by, created_at FROM homework WHERE class_id=?"
        " ORDER BY id DESC LIMIT 20",
        (dev["class_id"],),
    )
    return {
        "class_id": dev["class_id"],
        "server_time": now_ts(),
        "exit_password": dev["exit_password"] or "8888",
        "items": out,
        "homework": hw,
    }


@route("POST", r"/api/device/heartbeat")
def device_heartbeat(ctx, req):
    dev = req.device()
    body = req.json()
    ctx["db"].execute(
        "UPDATE devices SET last_seen=?, online=1, client_version=?, current_plan_id=?, cache_state=? WHERE id=?",
        (
            now_ts(),
            (body.get("version") or dev["client_version"] or "")[:32],
            body.get("current_plan_id"),
            json.dumps(body.get("cache_state") or {}, ensure_ascii=False)[:4000],
            dev["id"],
        ),
    )
    log_event(ctx, "device_update", {
        "id": dev["id"], "name": dev["name"], "online": 1,
        "last_seen": now_ts(), "current_plan_id": body.get("current_plan_id"),
        "cache_state": body.get("cache_state") or {},
    })
    return {"ok": True, "server_time": now_ts(), "exit_password": dev["exit_password"] or "8888"}


@route("POST", r"/api/device/report")
def device_report(ctx, req):
    dev = req.device()
    body = req.json()
    ctx["db"].execute(
        "INSERT INTO play_logs(device_id, plan_id, video_id, play_date, start_time, status, detail, created_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dev["id"], body.get("plan_id"), body.get("video_id"),
            body.get("play_date") or today_str(), body.get("start_time") or "",
            body.get("status") or "unknown", (body.get("detail") or "")[:500], now_ts(),
        ),
    )
    log_event(ctx, "play_log", {
        "device_id": dev["id"], "device_name": dev["name"], "class_id": dev["class_id"],
        "plan_id": body.get("plan_id"), "video_id": body.get("video_id"),
        "status": body.get("status"), "play_date": body.get("play_date") or today_str(),
        "start_time": body.get("start_time") or "",
    })
    return {"ok": True}


@route("POST", r"/api/device/verify-password")
def device_verify_password(ctx, req):
    dev = req.device()
    body = req.json()
    ok = (body.get("password") or "") == (dev["exit_password"] or "8888")
    return {"ok": ok}


# =============================================================== 监控 / 概览
@route("GET", r"/api/overview")
def overview(ctx, req):
    user = req.user()
    class_id = req.arg("class_id") or (user["class_id"] if user["role"] != "admin" else None)
    date = req.arg("date") or today_str()
    wd = datetime.datetime.strptime(date, "%Y-%m-%d").weekday() + 1

    devices = ctx["db"].query(
        "SELECT d.*, c.name AS class_name FROM devices d LEFT JOIN classes c ON c.id=d.class_id"
        + (" WHERE d.class_id=?" if class_id else "")
        + " ORDER BY d.id",
        (int(class_id),) if class_id else (),
    )
    now = now_ts()
    logs = ctx["db"].query(
        "SELECT * FROM play_logs WHERE play_date=?" + (" AND device_id IN (%s)" % ",".join(["?"] * len(devices)) if devices else ""),
        tuple([date] + [d["id"] for d in devices]) if devices else (date,),
    )
    plans = ctx["db"].query(
        "SELECT p.*, v.title AS video_title FROM plans p JOIN videos v ON v.id=p.video_id"
        " WHERE p.weekday=?" + (" AND p.class_id=?" if class_id else ""),
        (wd, int(class_id)) if class_id else (wd,),
    )
    logs_by_device = {}
    for lg in logs:
        logs_by_device.setdefault(lg["device_id"], []).append(lg)

    items = []
    for d in devices:
        dev_logs = logs_by_device.get(d["id"], [])
        done = len([x for x in dev_logs if x["status"] == "success"])
        items.append({
            "id": d["id"],
            "name": d["name"],
            "class_id": d["class_id"],
            "class_name": d["class_name"],
            "online": 1 if (d["last_seen"] and now - d["last_seen"] < 90) else 0,
            "last_seen": d["last_seen"],
            "client_version": d["client_version"],
            "exit_password": d["exit_password"],
            "today_total": len(plans),
            "today_done": done,
            "logs": dev_logs,
        })
    return {
        "date": date,
        "weekday": wd,
        "device_total": len(devices),
        "device_online": len([x for x in items if x["online"]]),
        "plan_total": len(plans),
        "plans": [
            {"id": p["id"], "start_time": p["start_time"], "video_title": p["video_title"],
             "class_id": p["class_id"]}
            for p in plans
        ],
        "devices": items,
    }


@route("GET", r"/api/logs")
def list_logs(ctx, req):
    user = req.user()
    date = req.arg("date") or today_str()
    class_id = req.arg("class_id") or (user["class_id"] if user["role"] != "admin" else None)
    sql = (
        "SELECT l.*, d.name AS device_name, v.title AS video_title, p.start_time AS plan_time"
        " FROM play_logs l JOIN devices d ON d.id=l.device_id"
        " LEFT JOIN videos v ON v.id=l.video_id LEFT JOIN plans p ON p.id=l.plan_id"
        " WHERE l.play_date=?"
    )
    args = [date]
    if class_id:
        sql += " AND d.class_id=?"
        args.append(int(class_id))
    sql += " ORDER BY l.created_at DESC LIMIT 500"
    return {"items": ctx["db"].query(sql, tuple(args))}


@route("GET", r"/api/server-info")
def server_info(ctx, req):
    req.user()
    return {
        "server_time": now_ts(),
        "weekday": weekday_monday1(),
        "version": ctx["config"].get("version", "1.0.0"),
        "base": public_base(ctx, req),
    }


# ==================================================================== 媒体
@route("GET", r"/media/(?P<vid>\d+)")
def media(ctx, req, vid):
    # 校验：教师令牌 或 设备令牌 或 public=true
    ok = False
    token = req.headers.get("X-Auth-Token") or req.arg("t")
    if token and dbmod.parse_token(ctx["secret"], token):
        ok = True
    if not ok:
        key = req.arg("d")
        sec = req.arg("k")
        if key and sec and ctx["db"].query_one(
            "SELECT id FROM devices WHERE device_key=? AND secret=?", (key, sec)
        ):
            ok = True
    if not ok and req.arg("public") != "true":
        raise HttpError(401, "无权访问该媒体")
    row = ctx["db"].query_one("SELECT * FROM videos WHERE id=?", (int(vid),))
    if not row:
        raise HttpError(404, "视频不存在")
    if row["kind"] != "local" or not row["file_path"]:
        raise HttpError(400, "在线视频请直接访问其原始链接")
    path = os.path.join(ctx["root"], row["file_path"])
    if not os.path.exists(path):
        raise HttpError(404, "文件已丢失")
    ext = os.path.splitext(path)[1].lower()
    ctype = EXT_TYPES.get(ext, "application/octet-stream")
    return FileResponse(path, ctype, filename=row["title"] + ext)
