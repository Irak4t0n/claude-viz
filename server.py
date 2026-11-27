"""Claude Live Activity - local event feed.

Tails logs that Claude's tools already write on this PC and streams them to
the visualizer page over Server-Sent Events. Read-only: it never writes to
any log, never calls Claude, and listens on 127.0.0.1 only.

Run:  pythonw server.py      Page:  http://127.0.0.1:47800
"""
import csv
import glob
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CLAUDE_VIZ_PORT", "47800"))
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("USERPROFILE") or os.path.expanduser("~")
LOCAL = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")

DC_DIR = os.path.join(HOME, ".claude-server-commander")
APP_LOGS = os.path.join(LOCAL, "Claude", "Logs")
CC_PROJECTS = os.path.join(HOME, ".claude", "projects")

clients = []
clients_lock = threading.Lock()
history = deque(maxlen=40)


state = {"link": False}
VALID = {"SHELL", "FILES", "NET", "HARDWARE", "CODE", "GIT", "WEB", "SCREEN", "VAULT", "MEDIA"}


def push(ev):
    with clients_lock:
        for q in clients:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


def emit(sys_, tool, detail="", w=1.0, kind="call", nbytes=0, src="", **extra):
    ev = {"t": int(time.time() * 1000), "sys": sys_, "tool": tool,
          "detail": (detail or "")[:220], "w": w, "kind": kind,
          "bytes": nbytes, "src": src}
    ev.update(extra)
    if kind == "call":
        history.append(ev)
    push(ev)


# ---------- classification ----------
RX_HW = re.compile(r"\b(com\d+|esptool|pio|platformio|infishark|adb|fastboot|picotool|dfu-util|minicom|pyserial)\b|/dev/tty|serialport", re.I)
RX_NET = re.compile(r"\b(ssh|scp|sftp|curl|wget|ping|nmap|invoke-webrequest|invoke-restmethod|iwr|netsh|tracert|nslookup|test-netconnection|rsync)\b", re.I)
RX_GIT = re.compile(r"\b(git|gh)\b", re.I)
RX_CODE = re.compile(r"\b(python|pythonw|py|node|npm|npx|pnpm|pip|cargo|make|cmake|gcc|dotnet|pytest|idf\.py)\b", re.I)


def classify_cmd(cmd):
    if RX_HW.search(cmd):
        return "HARDWARE"
    if RX_NET.search(cmd):
        return "NET"
    if RX_GIT.search(cmd):
        return "GIT"
    if RX_CODE.search(cmd):
        return "CODE"
    return "SHELL"


FILE_TOOLS = {"read_file", "read_multiple_files", "write_file", "write_pdf", "edit_block",
              "create_directory", "list_directory", "move_file", "get_file_info",
              "start_search", "get_more_search_results", "stop_search", "list_searches"}


def one_line(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def summarize(args):
    """Short label for a tool call. Never includes file contents."""
    if not isinstance(args, dict):
        return ""
    for k in ("command", "input", "path", "file_path", "source", "pattern", "query", "url", "paths"):
        if k in args and args[k]:
            v = args[k]
            if isinstance(v, list):
                v = ", ".join(map(str, v[:3]))
            return one_line(v)
    return ""


# ---------- Desktop Commander ----------
RX_DC = re.compile(r"^(\S+)\s*\|\s*(\w+)\s*\|\s*Arguments:\s*(.*)$")


def on_dc_call(line, path):
    m = RX_DC.match(line)
    if not m:
        return
    tool, raw = m.group(2), m.group(3)
    try:
        args = json.loads(raw)
    except Exception:
        args = {}
        mm = re.search(r'"(command|path|pattern|input)"\s*:\s*"((?:[^"\\]|\\.){0,300})', raw)
        if mm:
            args = {mm.group(1): mm.group(2)}
    detail = summarize(args)
    bm = RX_BEACON.search(detail) if "command" in args else None
    if bm:  # steps Claude did in the cloud, reported in one cheap no-op command
        for k, item in enumerate(bm.group(1).split(";;")[:12]):
            parts = [x.strip() for x in item.split("|")]
            if not parts[0]:
                continue
            sys_ = parts[0].upper() if parts[0].upper() in VALID else "WEB"
            emit(sys_, parts[1] if len(parts) > 1 else "cloud", parts[2] if len(parts) > 2 else "",
                 1.0, src="cloud", delay=k * 450)
        return
    extra = {}
    body = args.get("content") or args.get("new_string")
    if isinstance(body, str) and body:
        extra = {"out": body[:900], "nbytes": len(body)}
    if tool in FILE_TOOLS:
        sys_ = "WEB" if args.get("isUrl") else "FILES"
    elif "command" in args or "input" in args:
        sys_ = classify_cmd(detail)
    else:
        sys_ = "SHELL"
    emit(sys_, tool, detail, 1.0, src="desktop-commander", **extra)


RX_BEACON = re.compile(r"#\s*cviz:\s*(.+)$")
RX_PID = re.compile(r"Process started with PID (\d+)")
RX_HIST = re.compile(r'"toolName"\s*:\s*"(\w+)"')


def on_dc_done(line, path):
    m = RX_HIST.search(line[:300])
    if not m:
        return
    out = ""
    try:
        content = (json.loads(line).get("output") or {}).get("content") or []
        out = "".join(x.get("text", "") for x in content if isinstance(x, dict))
    except Exception:
        pass
    pm = RX_PID.search(out[:400] or line[:4000])
    if pm:
        register_root(int(pm.group(1)))
    emit("SHELL", m.group(1), "", 0.4, kind="done", nbytes=len(out) or len(line),
         src="desktop-commander", out=out[:900])


# ---------- Claude desktop app ----------
RX_SERVED = re.compile(r"\[remote-tools-device\] served (\S+)")
RX_RFILE = re.compile(r"\[remote-file\] (committed|staged) (.+)$")


def on_main_log(line, path):
    if "keep-awake held" in line or "keep-awake released" in line:
        state["link"] = "held" in line
        push({"kind": "link", "on": state["link"]})
        return
    rf = RX_RFILE.search(line)
    if rf:
        emit("FILES", "commit_files" if rf.group(1) == "committed" else "stage_files",
             rf.group(1) + " " + rf.group(2), 1.0, src="claude-app")
        return
    m = RX_SERVED.search(line)
    if not m:
        return
    name = m.group(1)
    low = name.lower()
    if low.startswith("computer"):
        sys_ = "SCREEN"
    elif "browser" in low:
        sys_ = "WEB"
    else:
        sys_ = "FILES"
    emit(sys_, name, "", 0.8, src="claude-app")


def on_mcp_log(line, path):
    if "tools/call" not in line:
        return
    server = os.path.basename(path)[len("mcp-server-"):-len(".log")]
    low = server.lower()
    sys_ = "VAULT" if "obsidian" in low else "MEDIA" if "resolve" in low else "GIT" if "git" in low else "CODE"
    emit(sys_, server, "tool call", 0.9, src="mcp")


_last_browser = [0.0]


def on_browser_log(line, path):
    now = time.time()
    if now - _last_browser[0] < 0.5 or not line.strip():
        return
    _last_browser[0] = now
    name = "chrome" if "chrome" in os.path.basename(path) else "in-app browser"
    emit("WEB", name, "browser activity", 0.5, src="browser")


# ---------- Claude Code transcripts ----------
CC_FILES = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit", "LS"}


def on_cc_line(line, path):
    if '"tool_use"' not in line:
        return
    try:
        obj = json.loads(line)
    except Exception:
        return
    content = (obj.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        name = str(c.get("name", "tool"))
        inp = c.get("input") or {}
        detail = summarize(inp)
        low = name.lower()
        if "desktop-commander" in low or "desktop_commander" in low:
            continue  # already seen via Desktop Commander's own log
        if name in ("Bash", "PowerShell"):
            sys_ = classify_cmd(detail)
        elif name in CC_FILES:
            sys_ = "FILES"
        elif name in ("WebSearch", "WebFetch"):
            sys_ = "WEB"
        elif "obsidian" in low:
            sys_ = "VAULT"
        elif "resolve" in low:
            sys_ = "MEDIA"
        elif "chrome" in low or "browser" in low:
            sys_ = "WEB"
        elif "computer" in low:
            sys_ = "SCREEN"
        elif "git" in low:
            sys_ = "GIT"
        else:
            sys_ = "CODE"
        emit(sys_, name.split("__")[-1], detail, 1.0, src="claude-code")


# ---------- tailing ----------
class Tail:
    def __init__(self, path, handler, from_end=True):
        self.path, self.handler = path, handler
        self.pos = None if from_end else 0
        self.buf = b""

    def poll(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if self.pos is None:
            self.pos = size
            return
        if size < self.pos:  # rotated or truncated
            self.pos, self.buf = 0, b""
        if size == self.pos:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(min(size - self.pos, 8 << 20))
        except OSError:
            return
        self.pos += len(data)
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        if len(self.buf) > (16 << 20):
            self.buf = b""
        for ln in lines:
            try:
                self.handler(ln.decode("utf-8", "replace").rstrip("\r"), self.path)
            except Exception:
                pass


def watcher():
    tails = {}

    def add(path, handler, from_end=True):
        if path not in tails:
            tails[path] = Tail(path, handler, from_end)

    add(os.path.join(DC_DIR, "claude_tool_call.log"), on_dc_call)
    add(os.path.join(DC_DIR, "tool-history.jsonl"), on_dc_done)
    add(os.path.join(APP_LOGS, "main.log"), on_main_log)
    add(os.path.join(APP_LOGS, "chrome-native-host.log"), on_browser_log)
    add(os.path.join(APP_LOGS, "in-app-browser.log"), on_browser_log)

    first, last_scan = True, 0.0
    while True:
        now = time.time()
        if now - last_scan > 3:
            last_scan = now
            for p in glob.glob(os.path.join(APP_LOGS, "mcp-server-*.log")):
                add(p, on_mcp_log, from_end=True)
            try:
                for p in glob.glob(os.path.join(CC_PROJECTS, "**", "*.jsonl"), recursive=True):
                    if p in tails:
                        continue
                    try:
                        fresh = now - os.path.getmtime(p) < 900
                    except OSError:
                        continue
                    if fresh:
                        # files that appear after startup are read from the top
                        add(p, on_cc_line, from_end=first)
            except Exception:
                pass
            first = False
        for t in list(tails.values()):
            t.poll()
        time.sleep(0.12)


# ---------- system telemetry (Windows performance counters) ----------
NOWIN = 0x08000000


def telemetry():
    if os.name != "nt":
        return
    counters = [r"\Processor(_Total)\% Processor Time", r"\Memory\% Committed Bytes In Use",
                r"\Network Interface(*)\Bytes Total/sec", r"\PhysicalDisk(_Total)\Disk Bytes/sec"]
    while True:
        try:
            p = subprocess.Popen(["typeperf", *counters, "-si", "1"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 creationflags=NOWIN, text=True, errors="replace")
            cols = None
            for line in p.stdout:
                row = next(csv.reader([line.strip()]), None)
                if not row or len(row) < 2:
                    continue
                if cols is None:
                    if row[0].startswith("(PDH"):
                        cols = [c.lower() for c in row]
                    continue
                cpu = mem = net = disk = 0.0
                for name, val in zip(cols[1:], row[1:]):
                    try:
                        v = float(val)
                    except ValueError:
                        continue
                    if "processor(" in name:
                        cpu = v
                    elif "memory" in name:
                        mem = v
                    elif "network interface" in name:
                        net += v
                    elif "physicaldisk" in name:
                        disk = v
                push({"kind": "tele", "cpu": cpu, "mem": mem, "net": net, "disk": disk})
        except Exception:
            pass
        time.sleep(5)


# ---------- process spawns under Desktop Commander ----------
family = {}
recent = deque(maxlen=600)
dc_node = [None]
PS_WATCH = (
    "$q=\"SELECT * FROM __InstanceCreationEvent WITHIN 1 WHERE TargetInstance ISA 'Win32_Process'\";"
    "Register-CimIndicationEvent -Query $q -SourceIdentifier p | Out-Null;"
    "while($true){$e=Wait-Event -SourceIdentifier p;$t=$e.SourceEventArgs.NewEvent.TargetInstance;"
    "[Console]::Out.WriteLine(\"$($t.ProcessId)|$($t.ParentProcessId)|$($t.Name)\");"
    "Remove-Event -EventIdentifier $e.EventIdentifier}"
)
QUIET = {"conhost.exe", "typeperf.exe"}


def register_root(pid):
    now = time.time()
    family[pid] = now
    for _, cpid, ppid, _n in list(recent):
        if cpid == pid:
            dc_node[0] = ppid
    for _, cpid, ppid, _n in list(recent):
        if ppid in family or ppid == dc_node[0]:
            family[cpid] = now
    for k in [k for k, v in family.items() if now - v > 900]:
        family.pop(k, None)


def proc_watch():
    if os.name != "nt":
        return
    while True:
        try:
            p = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_WATCH],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 creationflags=NOWIN, text=True, errors="replace")
            for line in p.stdout:
                parts = line.strip().split("|")
                if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                    continue
                pid, ppid, name = int(parts[0]), int(parts[1]), parts[2]
                now = time.time()
                recent.append((now, pid, ppid, name))
                if ppid == dc_node[0] or ppid in family:
                    family[pid] = now
                    if name.lower() not in QUIET:
                        emit(classify_cmd(name.lower().replace(".exe", "")), "spawn", name, 0.5,
                             kind="proc", src="process")
        except Exception:
            pass
        time.sleep(5)


# ---------- http ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    body = f.read()
            except OSError:
                body = b"index.html missing"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = queue.Queue(maxsize=500)
            try:
                for ev in list(history):
                    self.wfile.write(("data: " + json.dumps(dict(ev, h=1)) + "\n\n").encode())
                self.wfile.write(("data: " + json.dumps({"kind": "link", "on": state["link"]}) + "\n\n").encode())
                self.wfile.flush()
                with clients_lock:
                    clients.append(q)
                while True:
                    try:
                        ev = q.get(timeout=10)
                        self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with clients_lock:
                    if q in clients:
                        clients.remove(q)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        return  # already running
    srv.daemon_threads = True
    for fn in (watcher, telemetry, proc_watch):
        threading.Thread(target=fn, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
