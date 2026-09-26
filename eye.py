#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vps-eye —— 让 Claude 直接在你的 VPS 上干活的 MCP 服务器
======================================================

零依赖：只用 Python 3.8+ 标准库。
提供 5 个工具：shell / read_file / write_file / list_dir / system_info。

安全设计（默认全开，不需要你额外配置）：
  · 只监听 127.0.0.1，外面必须经过 nginx(HTTPS) 才能进来
  · 每个请求都校验暗号（Authorization: Bearer <暗号>），没有暗号拒绝启动
  · /.well-known/* 一律 404，Claude 就不会去走 OAuth 登录流程
  · 每次工具调用都写审计日志（写文件只记路径，不记内容）
  · write_file 覆盖已有文件前，自动备份一份 .bak-时间戳

配置（环境变量，install.sh 会帮你写好）：
  EYE_HOST        监听地址，默认 127.0.0.1
  EYE_PORT        监听端口，默认 8787
  EYE_TOKEN_FILE  暗号文件，默认 /etc/vps-eye/token
  EYE_TOKEN       也可以直接用环境变量给暗号（优先级高于文件）
  EYE_LOG_FILE    审计日志，默认 /var/log/vps-eye/audit.log
  EYE_MAX_OUTPUT  单次返回的最大字符数，默认 60000
"""

import datetime
import hmac
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"

HOST = os.environ.get("EYE_HOST", "127.0.0.1")
PORT = int(os.environ.get("EYE_PORT", "8787"))
TOKEN_FILE = os.environ.get("EYE_TOKEN_FILE", "/etc/vps-eye/token")
LOG_FILE = os.environ.get("EYE_LOG_FILE", "/var/log/vps-eye/audit.log")
MAX_OUTPUT = int(os.environ.get("EYE_MAX_OUTPUT", "60000"))

SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "vps-eye", "version": VERSION}


# ------------------------------------------------------------------
# 暗号
# ------------------------------------------------------------------
def load_token():
    tok = os.environ.get("EYE_TOKEN", "").strip()
    if not tok and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    if len(tok) < 24:
        sys.exit(
            "[vps-eye] 没有找到足够长的暗号（至少 24 位），拒绝启动。\n"
            f"  请把暗号写进 {TOKEN_FILE}，或设置环境变量 EYE_TOKEN。\n"
            "  生成一个：openssl rand -hex 32"
        )
    return tok


TOKEN = None  # 在 main() 里加载


def check_auth(headers):
    """返回 (是否通过, 拒绝原因)。原因只描述形状，绝不包含暗号内容。"""
    auth = headers.get("Authorization", "")
    key = headers.get("X-API-Key", "").strip()
    got = ""
    # 去掉零宽字符等看不见的东西；前缀写成什么样都不要紧（Bearer / 漏写 / 中文空格），只取最后一段
    cleaned = "".join(ch for ch in auth if ch.isprintable() or ch.isspace()).strip()
    if cleaned:
        got = cleaned.split()[-1]
    elif key:
        got = key
    if got and hmac.compare_digest(got.encode(), TOKEN.encode()):
        return True, ""
    if not auth and not key:
        return False, "没带 Authorization 请求头"
    if len(got) != len(TOKEN):
        return False, f"暗号长度不对（收到 {len(got)} 位，应为 {len(TOKEN)} 位）"
    return False, "暗号长度对，但内容不一致"


def authorized(headers):
    return check_auth(headers)[0]


# ------------------------------------------------------------------
# 审计日志
# ------------------------------------------------------------------
def audit(event, **fields):
    rec = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "event": event}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        print(line, file=sys.stderr)


def clip(text, limit=None):
    """太长就保留头尾，中间标出省略了多少。"""
    limit = limit or MAX_OUTPUT
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n\n…[中间省略 {cut} 个字符，输出太长；可以用 head/tail/grep 缩小范围]…\n\n{text[-tail:]}"


# ------------------------------------------------------------------
# 工具实现
# ------------------------------------------------------------------
def tool_shell(a):
    cmd = str(a.get("cmd", ""))
    if not cmd.strip():
        return "cmd 不能为空"
    cwd = a.get("cwd") or os.path.expanduser("~")
    timeout = max(1, min(int(a.get("timeout", 120)), 1800))
    started = time.time()
    try:
        p = subprocess.run(
            ["bash", "-lc", cmd], cwd=cwd, capture_output=True,
            text=True, errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        )
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        out += f"\n[超时] 命令跑了 {timeout} 秒还没结束，已终止。长任务请放到后台（nohup … &）或 tmux 里跑。"
        code = -1
    except FileNotFoundError:
        return f"目录不存在：{cwd}"
    took = round(time.time() - started, 1)
    return clip(f"{out.rstrip()}\n[退出码 {code} · 用时 {took}s]")


def tool_read_file(a):
    path = os.path.expanduser(str(a.get("path", "")))
    offset = max(1, int(a.get("offset", 1)))
    limit = max(1, min(int(a.get("limit", 2000)), 20000))
    if not os.path.isfile(path):
        return f"文件不存在：{path}"
    lines = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i < offset:
                continue
            if i >= offset + limit:
                lines.append(f"…（后面还有，用 offset={i} 继续读）")
                break
            lines.append(f"{i:>6}\t{line.rstrip(chr(10))}")
    return clip("\n".join(lines) if lines else "（空文件或超出范围）")


def tool_write_file(a):
    path = os.path.expanduser(str(a.get("path", "")))
    content = str(a.get("content", ""))
    mode = a.get("mode", "overwrite")
    if not path:
        return "path 不能为空"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    backup = None
    if os.path.exists(path) and mode == "overwrite":
        backup = f"{path}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(path, backup)
    with open(path, "a" if mode == "append" else "w", encoding="utf-8") as f:
        f.write(content)
    msg = f"已{'追加' if mode == 'append' else '写入'} {path}（{len(content)} 字符）"
    if backup:
        msg += f"\n原文件已备份到 {backup}"
    return msg


def tool_list_dir(a):
    path = os.path.expanduser(str(a.get("path", "~")))
    if not os.path.isdir(path):
        return f"目录不存在：{path}"
    rows = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        kind = "目录" if os.path.isdir(full) else ("链接" if os.path.islink(full) else "文件")
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        rows.append(f"{kind}\t{st.st_size:>10}\t{mtime}\t{name}")
    return clip("\n".join(rows) or "（空目录）")


def tool_system_info(_a):
    def sh(c):
        try:
            return subprocess.run(["bash", "-lc", c], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    parts = [
        f"系统：{sh('. /etc/os-release 2>/dev/null && echo $PRETTY_NAME') or platform.platform()}",
        f"内核：{platform.release()}  架构：{platform.machine()}",
        f"用户：{sh('whoami')}  主机名：{platform.node()}",
        f"运行时间：{sh('uptime -p')}",
        f"CPU：{os.cpu_count()} 核  负载：{sh('cat /proc/loadavg')}",
        "内存：\n" + sh("free -h"),
        "磁盘：\n" + sh("df -h / 2>/dev/null"),
        f"Python：{platform.python_version()}  vps-eye：{VERSION}",
    ]
    return "\n".join(parts)


TOOLS = [
    {
        "name": "shell",
        "description": "在 VPS 上用 bash 执行一条命令，返回输出和退出码。长任务请用 nohup/tmux 放后台。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的命令"},
                "cwd": {"type": "string", "description": "工作目录，默认家目录"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 120，最大 1800"},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "read_file",
        "description": "读取文本文件（带行号）。大文件用 offset/limit 分段读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "从第几行开始，默认 1"},
                "limit": {"type": "integer", "description": "最多读多少行，默认 2000"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "写入文本文件。覆盖已有文件前会自动备份为 .bak-时间戳。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "默认 overwrite"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "列出目录内容（类型、大小、修改时间）。",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "system_info",
        "description": "查看 VPS 的系统、CPU、内存、磁盘等基本信息。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "shell": tool_shell,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "list_dir": tool_list_dir,
    "system_info": tool_system_info,
}


def call_tool(name, args):
    fn = HANDLERS.get(name)
    if not fn:
        return f"没有这个工具：{name}", True
    safe_args = {k: (v if k != "content" else f"<{len(str(v))} 字符>") for k, v in (args or {}).items()}
    audit("tool_call", tool=name, args=safe_args)
    try:
        return fn(args or {}), False
    except Exception as e:  # 工具出错也要把原因告诉 Claude，而不是整个请求失败
        audit("tool_error", tool=name, error=repr(e))
        return f"出错了：{type(e).__name__}: {e}", True


# ------------------------------------------------------------------
# MCP（JSON-RPC over Streamable HTTP，只用普通 JSON 响应）
# ------------------------------------------------------------------
def handle_rpc(req):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        asked = (req.get("params") or {}).get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "这是用户自己的 VPS。改动前先说明要做什么；改配置前先备份；不确定的破坏性操作先问用户。",
        }}
    if rid is None:  # 通知类消息，不需要回复
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = req.get("params") or {}
        text, is_error = call_tool(p.get("name"), p.get("arguments"))
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_error}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未知方法：{method}"}}


class Handler(BaseHTTPRequestHandler):
    server_version = "vps-eye/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 不往 stderr 刷访问日志
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _deny(self):
        audit("auth_denied", ip=self.headers.get("X-Real-IP") or self.client_address[0],
              path=self.path, reason=check_auth(self.headers)[1])
        time.sleep(1)  # 拖慢猜暗号
        self._json(401, {"error": "unauthorized"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._json(200, {"ok": True, "name": "vps-eye", "version": VERSION})
        if path.startswith("/.well-known/"):
            return self._send(404)
        if not authorized(self.headers):
            return self._deny()
        # 本服务器不提供 SSE 推送流
        return self._send(405)

    def do_DELETE(self):
        if not authorized(self.headers):
            return self._deny()
        return self._send(204)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/mcp", "/mcp/", "/"):
            return self._send(404)
        if not authorized(self.headers):
            return self._deny()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "JSON 解析失败"}})
        if isinstance(payload, list):
            replies = [r for r in (handle_rpc(x) for x in payload) if r is not None]
            return self._json(200, replies) if replies else self._send(202)
        reply = handle_rpc(payload or {})
        return self._json(200, reply) if reply is not None else self._send(202)


def main():
    global TOKEN
    TOKEN = load_token()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    audit("start", host=HOST, port=PORT, version=VERSION)
    print(f"[vps-eye {VERSION}] 在 http://{HOST}:{PORT}/mcp 等候（暗号已加载）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
