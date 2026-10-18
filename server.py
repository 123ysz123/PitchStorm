#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PitchStorm LAN server
=======================
1) 静态托管 PitchStorm.html（只放行这一个文件，不暴露整个工作目录）
2) 极简文本数据库：把存档和比赛记录写成纯文本文件，无需登录

存储布局（全部是可直接用记事本打开的文本）
    data/devices/<deviceId>.json   每台设备一份存档（属性/阵型/战术/赛事进度）
    data/matches.jsonl             每行一条比赛记录，追加写入

设备识别优先级：Cookie ps_dev → 请求头 X-Device-Id → 客户端 IP
"""
import hashlib
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(ROOT, "PitchStorm.html")
DATA = os.path.join(ROOT, "data")
DEV_DIR = os.path.join(DATA, "devices")
MATCH_LOG = os.path.join(DATA, "matches.jsonl")
MAX_BODY = 256 * 1024
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

for _d in (DATA, DEV_DIR):
    os.makedirs(_d, exist_ok=True)


# ---------------------------------------------------------------- 存储小工具
def now_ms():
    return int(time.time() * 1000)


def safe_id(v):
    """只允许字母数字下划线横线，杜绝 ../ 之类的路径穿越"""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(v or ""))[:64]


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def append_line(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def tail_lines(path, limit):
    """文件很小，直接全读再取尾巴，够用且不会踩到 seek 的编码坑"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = [ln for ln in f if ln.strip()]
    except FileNotFoundError:
        return []
    return rows[-limit:]


def dev_path(did):
    return os.path.join(DEV_DIR, did + ".json")


# ---------------------------------------------------------------- HTTP 处理
class Handler(BaseHTTPRequestHandler):
    server_version = "PitchStormLAN/2.0"
    protocol_version = "HTTP/1.1"

    # ---------- 响应 ----------
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", cookie=None):
        self.send_response(code)
        if code != 204:                      # 204 不允许带 Content-Length
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
        if cookie:
            self.send_header(
                "Set-Cookie",
                "ps_dev=%s; Path=/; Max-Age=31536000; SameSite=Lax" % cookie,
            )
        self.end_headers()
        if self.command != "HEAD" and code != 204:
            self.wfile.write(body)

    def _json(self, code, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", cookie)

    # ---------- 设备识别 ----------
    def device(self):
        cookie_hdr = self.headers.get("Cookie") or ""
        did = ""
        for part in cookie_hdr.split(";"):
            part = part.strip()
            if part.startswith("ps_dev="):
                did = safe_id(part[len("ps_dev="):])
        if not did:
            did = safe_id(self.headers.get("X-Device-Id") or "")
        if not did:
            ip = self.client_address[0]
            did = "ip-" + hashlib.sha1(ip.encode("utf-8")).hexdigest()[:12]
        return did

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > MAX_BODY:
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _touch_device(self, did):
        path = dev_path(did)
        rec = read_json(path) or {}
        if not rec:
            rec = {"deviceId": did, "firstSeen": now_ms(), "matchCount": 0, "profile": None}
        rec["lastSeen"] = now_ms()
        rec["ip"] = self.client_address[0]
        rec["ua"] = (self.headers.get("User-Agent") or "")[:180]
        return rec

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        if path in ("/", "/index.html", "/PitchStorm.html", "/game"):
            try:
                with open(GAME, "rb") as f:
                    data = f.read()
            except OSError as exc:
                self._send(500, ("cannot read game file: %s" % exc).encode())
                return
            self._send(200, data, "text/html; charset=utf-8")
            return

        if path == "/health":
            self._send(200, b"ok")
            return

        if path == "/favicon.ico":
            self._send(204)
            return

        if path == "/api/profile":
            did = self.device()
            rec = read_json(dev_path(did)) or {}
            try:
                limit = max(1, min(50, int(qs.get("limit", ["6"])[0])))
            except ValueError:
                limit = 6
            self._json(200, {
                "deviceId": did,
                "updatedAt": rec.get("updatedAt", 0),
                "profile": rec.get("profile"),
                "matchCount": rec.get("matchCount", 0),
                "matches": self._recent(did, limit),
            }, cookie=did)
            return

        if path == "/api/history":
            did = self.device()
            try:
                limit = max(1, min(50, int(qs.get("limit", ["10"])[0])))
            except ValueError:
                limit = 10
            self._json(200, {"deviceId": did, "matches": self._recent(did, limit)}, cookie=did)
            return

        if path == "/api/stats":
            rows = tail_lines(MATCH_LOG, 100000)
            devs = [f for f in os.listdir(DEV_DIR) if f.endswith(".json")]
            self._json(200, {
                "devices": len(devs),
                "matches": len(rows),
                "serverTime": now_ms(),
            })
            return

        self._send(404, b"not found")

    def _recent(self, did, limit):
        out = []
        for ln in reversed(tail_lines(MATCH_LOG, 4000)):
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("deviceId") == did:
                out.append(rec)
                if len(out) >= limit:
                    break
        return out

    def do_POST(self):
        path = urlparse(self.path).path
        did = self.device()

        if path == "/api/profile":
            body = self._read_body()
            if not isinstance(body, dict):
                self._json(400, {"error": "bad json"}, cookie=did)
                return
            profile = body.get("profile")
            updated = body.get("updatedAt") or now_ms()
            try:
                updated = int(updated)
            except (TypeError, ValueError):
                updated = now_ms()
            rec = self._touch_device(did)
            old = rec.get("updatedAt", 0)
            # 最后写入者获胜，但拒绝比现有更旧的存档（多端同时在线时不会互相覆盖成旧数据）
            if profile is not None and updated >= old:
                rec["profile"] = profile
                rec["updatedAt"] = updated
            write_json(dev_path(did), rec)
            self._json(200, {"ok": True, "deviceId": did, "updatedAt": rec.get("updatedAt", 0)}, cookie=did)
            return

        if path == "/api/match":
            body = self._read_body()
            if not isinstance(body, dict):
                self._json(400, {"error": "bad json"}, cookie=did)
                return
            rec = self._touch_device(did)
            rec["matchCount"] = int(rec.get("matchCount", 0)) + 1
            write_json(dev_path(did), rec)

            row = {
                "ts": now_ms(),
                "deviceId": did,
                "ip": self.client_address[0],
                "no": rec["matchCount"],
                "home": str(body.get("home", ""))[:24],
                "away": str(body.get("away", ""))[:24],
                "hs": int(body.get("hs", 0) or 0),
                "as": int(body.get("as", 0) or 0),
                "shots": str(body.get("shots", ""))[:24],
                "on": str(body.get("on", ""))[:24],
                "pass": str(body.get("pass", ""))[:24],
                "poss": int(body.get("poss", 50) or 50),
                "diff": str(body.get("diff", ""))[:12],
                "mode": str(body.get("mode", "friendly"))[:16],
            }
            append_line(MATCH_LOG, row)
            self._json(200, {"ok": True, "no": rec["matchCount"]}, cookie=did)
            return

        self._send(404, b"not found")

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    if not os.path.exists(GAME):
        print("game file missing: %s" % GAME, file=sys.stderr)
        sys.exit(1)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("serving %s on 0.0.0.0:%d" % (GAME, PORT), flush=True)
    print("data dir: %s" % DATA, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
