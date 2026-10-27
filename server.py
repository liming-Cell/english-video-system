# -*- coding: utf-8 -*-
"""英语课前自动视频播放系统 —— 云端同步服务 + 教师端后端。

零第三方依赖：仅使用 Python 标准库（http.server / sqlite3 / socket）。
启动方式：
    python server.py                # 默认 0.0.0.0:8080
    python server.py --port 9000
"""
import argparse
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import urllib.parse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import api
import db as dbmod
import ws as wsmod

# PyInstaller 打包成单文件 exe 后，__file__ 位于临时解压目录；
# 数据目录 / config.json 必须放在 exe 旁边，保证重启后数据不丢；
# 而网页静态资源被打进包内，位于 onefile 解压目录 _MEIPASS。
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
    BUNDLE_DIR = getattr(sys, "_MEIPASS", ROOT)
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = ROOT


# ================================================================ 配置
def load_config():
    path = os.path.join(ROOT, "config.json")
    cfg = {
        "host": "0.0.0.0",
        "port": 8080,
        "public_base": "",
        "web_dir": "web",
        "data_dir": "data",
        "version": "1.0.0",
    }
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                cfg.update(json.load(fp))
        except ValueError:
            print("[warn] config.json 解析失败，使用默认配置")
    if not cfg.get("secret"):
        cfg["secret"] = dbmod.new_secret()
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)
    return cfg


# ================================================================ WS 中枢
class Hub(object):
    """WebSocket 连接池：教师端（监控）与学生端（设备）订阅广播。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []

    def add(self, conn, kind, device_key=None, class_id=None, user_id=None):
        entry = {
            "conn": conn,
            "kind": kind,
            "device_key": device_key,
            "class_id": class_id,
            "user_id": user_id,
        }
        with self.lock:
            self.clients.append(entry)
        return entry

    def remove(self, conn):
        with self.lock:
            self.clients = [c for c in self.clients if c["conn"] is not conn]

    def _snapshot(self):
        with self.lock:
            return list(self.clients)

    def _send(self, entries, payload, drop):
        text = json.dumps(payload, ensure_ascii=False)
        for e in entries:
            if not e["conn"].send_text(text):
                drop.append(e)

    def broadcast_teachers(self, payload):
        drop = []
        self._send([e for e in self._snapshot() if e["kind"] == "teacher"], payload, drop)
        for e in drop:
            self.remove(e["conn"])

    def broadcast_devices_of_class(self, class_id, payload):
        drop = []
        self._send(
            [e for e in self._snapshot() if e["kind"] == "device" and e["class_id"] == class_id],
            payload, drop,
        )
        for e in drop:
            self.remove(e["conn"])

    def broadcast_device(self, device_key, payload):
        drop = []
        self._send(
            [e for e in self._snapshot() if e["kind"] == "device" and e["device_key"] == device_key],
            payload, drop,
        )
        for e in drop:
            self.remove(e["conn"])

    def keepalive(self):
        """定期 ping，清理死连接。"""
        while True:
            time.sleep(30)
            drop = []
            for e in self._snapshot():
                if not e["conn"].send_text(json.dumps({"type": "ping"})):
                    drop.append(e)
            for e in drop:
                self.remove(e["conn"])


# ================================================================ HTTP 层
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "EnglishVideoServer/1.0"

    # ---- 基础输出 -------------------------------------------------
    def log_message(self, fmt, *args):
        if os.environ.get("EVS_VERBOSE"):
            sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body, content_type, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Device-Key, X-Device-Secret, X-File-Name")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _send_file(self, resp):
        path = resp.path
        try:
            total = os.path.getsize(path)
        except OSError:
            self._send_json({"error": "文件不存在"}, 404)
            return
        start, end = 0, total - 1
        status = 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else total - 1
                else:  # bytes=-N 后缀形式
                    start = max(0, total - int(b))
                    end = total - 1
            except ValueError:
                start, end = 0, total - 1
            if start >= total:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % total)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, total - 1)
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store" if resp.no_cache else "public, max-age=3600")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.end_headers()
        if self.command == "HEAD":
            return
        remaining = length
        with open(path, "rb") as fp:
            fp.seek(start)
            while remaining > 0:
                chunk = fp.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _static(self, rel):
        web_dir = self.ctx["web_dir"]
        rel = rel.lstrip("/")
        if rel in ("", "/"):
            rel = "index.html"
        target = os.path.normpath(os.path.join(web_dir, rel))
        if not target.startswith(os.path.normpath(web_dir)):
            self._send_json({"error": "非法路径"}, 403)
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.exists(target):
            self._send_json({"error": "资源不存在: " + rel}, 404)
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send_file(api.FileResponse(target, ctype, no_cache=True))

    # ---- 请求分发 -------------------------------------------------
    def _handle(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        req = api.Request(self, self.command, path, query, self.ctx)

        if path == "/ws" or path == "/ws/":
            self._websocket(query)
            return

        try:
            if path.startswith("/media/"):
                result = api.dispatch(self.ctx, req)
            elif path.startswith("/api/"):
                result = api.dispatch(self.ctx, req)
            else:
                self._static(path)
                return
        except api.HttpError as exc:
            self._send_json({"error": exc.message}, exc.status)
            return
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001  —— 兜底，避免线程崩溃
            import traceback
            traceback.print_exc()
            self._send_json({"error": "服务器内部错误：%s" % exc}, 500)
            return

        if result is None:
            return
        if isinstance(result, api.FileResponse):
            self._send_file(result)
        elif isinstance(result, api.RawResponse):
            self._send(result.status, result.body, result.content_type, result.headers)
        else:
            self._send_json(result)

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    # ---- WebSocket -------------------------------------------------
    def _websocket(self, query):
        if (self.headers.get("Upgrade") or "").lower() != "websocket":
            self._send_json({"error": "需要 WebSocket 升级请求"}, 400)
            return
        ctx = self.ctx
        kind, class_id, device_key, user_id = None, None, None, None

        token = (query.get("t") or [None])[0]
        if token:
            payload = dbmod.parse_token(ctx["secret"], token)
            if not payload:
                self._send_json({"error": "令牌无效"}, 401)
                return
            user = ctx["db"].query_one("SELECT * FROM users WHERE id=?", (payload.get("uid"),))
            if not user:
                self._send_json({"error": "账号不存在"}, 401)
                return
            kind = "teacher"
            class_id = user["class_id"]
            user_id = user["id"]

        if kind is None:
            key = (query.get("d") or [None])[0]
            sec = (query.get("k") or [None])[0]
            dev = ctx["db"].query_one(
                "SELECT * FROM devices WHERE device_key=? AND secret=?", (key or "", sec or "")
            )
            if dev:
                kind = "device"
                class_id = dev["class_id"]
                device_key = dev["device_key"]

        if kind is None:
            self._send_json({"error": "未授权"}, 401)
            return

        conn = wsmod.upgrade(self)
        if conn is None:
            return
        entry = ctx["hub"].add(conn, kind, device_key=device_key, class_id=class_id, user_id=user_id)
        conn.send_json({"type": "welcome", "role": kind, "server_time": int(time.time())})
        if kind == "device":
            ctx["db"].execute("UPDATE devices SET last_seen=?, online=1 WHERE device_key=?",
                              (int(time.time()), device_key))
        try:
            while True:
                frame = conn.recv()
                if frame is None:
                    break
                opcode, data = frame
                if opcode == wsmod.OP_CLOSE:
                    break
                if opcode in (wsmod.OP_TEXT, wsmod.OP_BIN):
                    try:
                        msg = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if msg.get("type") == "hello":
                        conn.send_json({"type": "ready", "server_time": int(time.time())})
        finally:
            ctx["hub"].remove(entry["conn"])
            if kind == "device" and device_key:
                ctx["db"].execute("UPDATE devices SET online=0 WHERE device_key=?", (device_key,))
            conn.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="英语课前自动视频播放系统 - 云端服务")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    args = parser.parse_args()

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port
    elif os.environ.get("PORT"):
        # 云平台（Render / Heroku 等）会通过 PORT 环境变量指定监听端口
        try:
            cfg["port"] = int(os.environ["PORT"])
        except ValueError:
            print("[warn] PORT 环境变量不是合法整数，忽略")
    if args.host:
        cfg["host"] = args.host

    # 云平台可把数据目录挂到持久化磁盘上（EVS_DATA_DIR）；未设置时用项目内 data 目录
    data_dir = os.environ.get("EVS_DATA_DIR") or os.path.join(ROOT, cfg["data_dir"])
    videos_dir = os.path.join(data_dir, "videos")
    if os.path.isabs(cfg["web_dir"]):
        web_dir = cfg["web_dir"]
    else:
        web_dir = os.path.join(BUNDLE_DIR, cfg["web_dir"])
    os.makedirs(videos_dir, exist_ok=True)

    database = dbmod.DB(os.path.join(data_dir, "app.db"))
    hub = Hub()
    threading.Thread(target=hub.keepalive, daemon=True).start()

    Handler.ctx = {
        "config": cfg,
        "secret": cfg["secret"],
        "db": database,
        "hub": hub,
        "root": ROOT,
        "web_dir": web_dir,
        "videos_dir": videos_dir,
    }

    srv = Server((cfg["host"], cfg["port"]), Handler)
    ip = lan_ip()
    print("=" * 62)
    print(" 英语课前自动视频播放系统 · 云端服务已启动")
    print("=" * 62)
    print(" 教师端（本机） : http://127.0.0.1:%d" % cfg["port"])
    print(" 教师端（局域网）: http://%s:%d" % (ip, cfg["port"]))
    print(" 默认管理员     : admin / admin123")
    print(" 数据目录       : %s" % data_dir)
    print(" 提示：公网部署时请在 config.json 的 public_base 填写对外地址")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
