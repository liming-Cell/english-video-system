# -*- coding: utf-8 -*-
"""极简 RFC6455 WebSocket 服务端实现（仅依赖 Python 标准库）。

设计说明：
    http.server 的 BaseHTTPRequestHandler 在写完 101 响应后，把底层 socket
    交给 WebSocket 对象，后续所有帧收发都直接走裸 socket。
    WebSocket 客户端在收到 101 握手响应之前不会发送任何帧，因此不存在
    "HTTP 解析缓冲区里残留字节" 的问题，可以安全地绕开 rfile 直接 recv。
"""
import base64
import hashlib
import json
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def make_accept(key):
    """按 RFC6455 计算 Sec-WebSocket-Accept。"""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WebSocket(object):
    def __init__(self, sock, addr=None):
        self.sock = sock
        self.closed = False
        self._send_lock = threading.Lock()
        try:
            self.addr = addr or sock.getpeername()
        except OSError:
            self.addr = addr

    # ------------------------------------------------------------ recv
    def _recv_exact(self, n):
        chunks = []
        remaining = n
        while remaining > 0:
            try:
                chunk = self.sock.recv(remaining)
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv(self):
        """返回 (opcode, payload) 或 None（连接已关闭）。"""
        while True:
            head = self._recv_exact(2)
            if head is None:
                return None
            b0, b1 = head[0], head[1]
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F

            if length == 126:
                ext = self._recv_exact(2)
                if ext is None:
                    return None
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(8)
                if ext is None:
                    return None
                length = struct.unpack(">Q", ext)[0]

            mask = b""
            if masked:
                mask = self._recv_exact(4)
                if mask is None:
                    return None

            data = self._recv_exact(length) if length else b""
            if data is None:
                return None

            if masked and mask:
                data = bytes(d ^ mask[i % 4] for i, d in enumerate(data))

            if opcode == OP_CLOSE:
                return (OP_CLOSE, data)
            if opcode == OP_PING:
                self._send(OP_PONG, data)
                continue
            if opcode == OP_PONG:
                continue
            return (opcode, data)

    # ------------------------------------------------------------ send
    def _send(self, opcode, payload):
        if self.closed:
            return False
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < (1 << 16):
            header.append(126)
            header.extend(struct.pack(">H", n))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", n))
        packet = bytes(header) + payload
        with self._send_lock:
            try:
                self.sock.sendall(packet)
                return True
            except OSError:
                self.closed = True
                return False

    def send_text(self, text):
        return self._send(OP_TEXT, text.encode("utf-8"))

    def send_json(self, obj):
        return self.send_text(json.dumps(obj, ensure_ascii=False))

    def close(self):
        if not self.closed:
            self._send(OP_CLOSE, b"")
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def upgrade(handler):
    """把当前 HTTP 连接升级为 WebSocket。返回 WebSocket 或 None。"""
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        return None
    accept = make_accept(key)
    handler.wfile.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        ).encode("ascii")
    )
    try:
        handler.wfile.flush()
    except OSError:
        return None
    # 该连接不再走 HTTP 解析流程
    handler.close_connection = True
    sock = handler.connection
    try:
        sock.settimeout(None)
    except OSError:
        pass
    return WebSocket(sock, handler.client_address)
