#!/usr/bin/env python3
"""Slot spawner for the remote-chrome control room.

Manages up to SLOTS independent Chromium instances ("slots") on the shared
Selkies/labwc desktop:

- HTTP API on :9230 — GET /claim, /release?slot=N, /expand?slot=N, /collapse,
  /status
- all SLOTS browsers run permanently: the wall always shows the full grid
  (slot N in its fixed quadrant, each window at half the desktop size — with
  the 3840x2160 desktop that is a full 1920x1080 browser per tile)
- /claim only *reserves* a free slot for an agent; /release un-reserves it,
  kills the browser, wipes the profile and relaunches fresh — so the wall
  stays complete and a released slot never leaks state to the next agent
- /expand?slot=N raises one window to the full desktop (inspect in detail),
  /collapse returns to the grid; also bound to Ctrl+Alt+1..4 / Ctrl+Alt+0
  inside the session (see rc.xml)

Runs inside the labwc session (started from the autostart script, user abc).
Slots run as X11 clients (XWayland) because native Wayland clients cannot be
positioned programmatically; X11 windows can (xdotool).
"""
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SLOTS = 4
HTTP_PORT = 9230
CDP_BASE = 9240                       # slot N -> 127.0.0.1:(9240+N)
PROXY_BASE = 9250                     # slot N CDP proxy -> 0.0.0.0:(9250+N)
HOST_PORT_BASE = 9221                 # slot N -> host port (9221+N)
PROFILE_BASE = "/config/profiles"     # throwaway profiles, wiped on release
PERSIST_BASE = "/config/persistent"   # named profiles, never wiped
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
CHROME = "/usr/bin/chromium"
DISPLAY = ":0"
# reserved slots with no CDP traffic for this long are auto-released
IDLE_MINUTES = float(os.environ.get("IDLE_MINUTES", "15"))
# a FREE slot whose processes exceed this RSS (MB) is refreshed automatically
SLOT_MEM_MB = float(os.environ.get("SLOT_MEM_MB", "3000"))
# optional Chromium --host-resolver-rules, e.g. "MAP *.dev.example my-proxy"
# to reach dev stacks on a shared Docker network (see docker-compose.override.example.yml)
HOST_RESOLVER_RULES = os.environ.get("HOST_RESOLVER_RULES", "").strip()

state = set()                         # reserved slot numbers (claimed by agents)
expanded = None                       # slot currently expanded to full desktop
lock = threading.RLock()
last_activity = {n: 0.0 for n in range(1, SLOTS + 1)}   # CDP traffic timestamps
usage = {n: {} for n in range(1, SLOTS + 1)}             # per-slot cpu/mem/procs
# named persistent profile per slot (None = throwaway); copy=True means the
# slot runs a throwaway copy of that profile (discarded on release)
slot_profile = {n: None for n in range(1, SLOTS + 1)}
slot_copy = {n: False for n in range(1, SLOTS + 1)}
slot_dir = {n: None for n in range(1, SLOTS + 1)}        # user-data-dir in use


def log(msg):
    print(f"[spawner] {msg}", flush=True)


def cdp_port(n):
    return CDP_BASE + n


def profile_dir(n):
    """user-data-dir for slot n's next launch."""
    if slot_profile[n] and not slot_copy[n]:
        return f"{PERSIST_BASE}/{slot_profile[n]}"
    return f"{PROFILE_BASE}/slot{n}"


def persistent(n):
    """Slot runs a named profile (original or copy): never auto-reset."""
    return slot_profile[n] is not None


def dir_pattern(d):
    # pkill/pgrep -f regex; the boundary keeps "insta" from matching "instagram"
    return f"user-data-dir={re.escape(d)}( |$)"


def mcp_name(n):
    return f"remote-chrome-slot{n}"


def cdp_alive(n, timeout=1):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port(n)}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def write_prefs(d):
    os.makedirs(f"{d}/Default", exist_ok=True)
    # disable Chromium's client-side window buttons: there is no taskbar in
    # this session, so a minimized window would be unrecoverable by mouse
    with open(f"{d}/Default/Preferences", "w") as f:
        json.dump({"browser": {"custom_chrome_frame": False}}, f)


def clear_locks(d):
    # stale Singleton* locks (e.g. from before a container recreate, which
    # changes the hostname) make Chromium refuse the profile; exclusive use is
    # enforced by the spawner itself
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(f"{d}/{name}")
        except OSError:
            pass


def seed_profile(n):
    d = profile_dir(n)
    if slot_profile[n] and not slot_copy[n]:
        # named profile: keep everything, create on first use
        if not os.path.exists(f"{d}/Default/Preferences"):
            write_prefs(d)
        clear_locks(d)
        return
    shutil.rmtree(d, ignore_errors=True)
    src = f"{PERSIST_BASE}/{slot_profile[n]}" if slot_copy[n] else None
    if src and os.path.isdir(src):
        shutil.copytree(src, d, symlinks=True, ignore=shutil.ignore_patterns(
            "Singleton*", "Cache", "Code Cache", "GPUCache"))
        clear_locks(d)
    else:
        write_prefs(d)


def launch(n):
    # keep one generation of the previous log: a crash is usually followed by
    # an automatic relaunch, which would otherwise truncate the evidence
    try:
        os.replace(f"/config/slot{n}.log", f"/config/slot{n}.log.1")
    except OSError:
        pass
    seed_profile(n)
    slot_dir[n] = profile_dir(n)
    env = dict(os.environ, DISPLAY=DISPLAY)
    env.pop("WAYLAND_DISPLAY", None)  # force XWayland (positionable windows)
    # nice: the stream/compositor must stay responsive even under page load;
    # renderer-process-limit: caps the process explosion of ad-heavy pages
    subprocess.Popen([
        "nice", "-n", "5",
        CHROME,
        "--ozone-platform=x11",
        # no GPU in the VM anyway: without this flag Chromium renders via the
        # SwiftShader JIT, whose generated ARM code sporadically SIGILLs on
        # the Lima vCPU -> dead GPU process, black tiles, crash pages
        "--disable-gpu",
        "--renderer-process-limit=4",
        f"--class=slot{n}",
        f"--user-data-dir={slot_dir[n]}",
        f"--remote-debugging-port={cdp_port(n)}",
        "--no-sandbox",
        "--enable-logging=stderr",   # renderer crash signals land in slotN.log
        "--test-type",
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disable-dev-shm-usage",
        "--ignore-certificate-errors",
        # page fullscreen (e.g. YouTube) fills only the slot window, not the
        # whole shared desktop (see container/fullscreen-in-window/)
        "--load-extension=/opt/fullscreen-in-window",
        *([f"--host-resolver-rules={HOST_RESOLVER_RULES}"] if HOST_RESOLVER_RULES else []),
        "--window-position=0,0",
        "--window-size=1024,768",
        "about:blank",
    ], env=env, start_new_session=True,
        stdout=open(f"/config/slot{n}.log", "w"),
        stderr=subprocess.STDOUT)
    for _ in range(60):
        if cdp_alive(n):
            return
        time.sleep(0.5)
    raise RuntimeError(f"slot{n}: CDP did not come up")


def cdp_browser_close(n):
    """Ask slot n's Chromium to quit via CDP Browser.close. Unlike SIGTERM
    this is a clean shutdown that flushes the cookie store to disk — without
    it, cookies set shortly before a release are lost."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port(n)}/json/version", timeout=2) as r:
            ws_path = "/" + json.load(r)["webSocketDebuggerUrl"].split("/", 3)[3]
        s = socket.create_connection(("127.0.0.1", cdp_port(n)), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {ws_path} HTTP/1.1\r\nHost: 127.0.0.1:{cdp_port(n)}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        if b" 101 " not in s.recv(1024):
            return False
        payload = json.dumps({"id": 1, "method": "Browser.close"}).encode()
        mask = os.urandom(4)
        header = bytes([0x81, 0x80 | len(payload)]) if len(payload) < 126 else \
            bytes([0x81, 0x80 | 126]) + len(payload).to_bytes(2, "big")
        s.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        s.close()
        return True
    except Exception as e:
        log(f"slot{n}: CDP Browser.close failed: {e}")
        return False


def kill_dir(d, n=None):
    # the profile path is unique per slot and part of the browser cmdline
    pattern = dir_pattern(d)
    if n and cdp_browser_close(n):
        for _ in range(40):                  # clean shutdown: up to 10 s
            if subprocess.run(["pgrep", "-f", pattern], check=False,
                              capture_output=True).returncode != 0:
                return
            time.sleep(0.25)
    subprocess.run(["pkill", "-f", pattern], check=False)
    for _ in range(40):
        if subprocess.run(["pgrep", "-f", pattern], check=False,
                          capture_output=True).returncode != 0:
            break
        time.sleep(0.25)
    else:
        subprocess.run(["pkill", "-9", "-f", pattern], check=False)
        time.sleep(0.5)


def stop(n):
    d = slot_dir[n] or profile_dir(n)
    # clean CDP shutdown only matters for named profiles (cookies persist)
    kill_dir(d, n if d.startswith(PERSIST_BASE + "/") else None)
    # throwaway profiles (incl. copies) are wiped; named profiles never
    if d.startswith(PROFILE_BASE + "/"):
        shutil.rmtree(d, ignore_errors=True)


def ensure_running(n):
    if not cdp_alive(n):
        launch(n)


def profile_slot(name):
    """Slot currently running the original of named profile, or None."""
    for n in range(1, SLOTS + 1):
        if slot_profile[n] == name and not slot_copy[n]:
            return n
    return None


def claim(profile=None, copy=False):
    """Reserve a free slot. With profile, (re)launch it on that named
    profile (or a throwaway copy of it). Returns (slot, error)."""
    with lock:
        if profile and not copy and profile_slot(profile):
            return None, f"profile '{profile}' is open in slot{profile_slot(profile)}"
        for n in range(1, SLOTS + 1):
            if n in state:
                continue
            if profile or persistent(n):
                # leaving a free named-profile browser or switching to a
                # profile: restart the slot on the requested profile
                stop(n)
                slot_profile[n], slot_copy[n] = profile, bool(profile and copy)
                launch(n)
                retile()
            else:
                ensure_running(n)
            state.add(n)
            last_activity[n] = time.time()   # idle clock starts at claim
            what = (f" profile={profile}" + (" (copy)" if copy else "")) if profile else ""
            log(f"claimed slot{n}{what}")
            return n, None
    return None, "no free slot"


def release(n):
    """Un-reserve slot n, stop its browser and relaunch a fresh throwaway
    one. Named profiles are kept on disk; throwaway profiles/copies wiped."""
    global expanded
    with lock:
        state.discard(n)
        if expanded == n:
            expanded = None
        name = slot_profile[n]
        stop(n)
        slot_profile[n], slot_copy[n] = None, False
        launch(n)  # wall always shows the full grid, fresh profile
        kept = f" (profile '{name}' kept)" if name else " (profile wiped)"
        log(f"released slot{n}{kept}, relaunched fresh")
        retile()


def unclaim(n):
    """Only un-reserve slot n: the browser keeps running with its tabs."""
    with lock:
        state.discard(n)
        log(f"unclaimed slot{n} (browser left running)")


def xdo(*args):
    return subprocess.run(
        ["xdotool", *args], env=dict(os.environ, DISPLAY=DISPLAY),
        capture_output=True, text=True).stdout.strip()


def slot_window(n):
    for flag in ("--class", "--classname"):
        for wid in xdo("search", "--onlyvisible", flag, f"^slot{n}$").split():
            geo = geometry(wid)
            if geo and geo[2] >= 100:
                return wid
    return None


def geometry(wid):
    out = xdo("getwindowgeometry", "--shell", wid)
    try:
        vals = dict(line.split("=") for line in out.splitlines() if "=" in line)
        return (int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"]))
    except Exception:
        return None


def quadrant(n, w, h):
    # fixed cell per slot number: 1=TL, 2=TR, 3=BL, 4=BR
    col, row = (n - 1) % 2, (n - 1) // 2
    cw, ch = w // 2, h // 2
    return (col * cw, row * ch, w - cw if col else cw, h - ch if row else ch)


def apply_geometry(wid, x, y, w, h):
    # maximized windows ignore configure requests -> unmaximize first
    # (this xdotool build has no windowstate command; use wmctrl)
    subprocess.run(
        ["wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz"],
        env=dict(os.environ, DISPLAY=DISPLAY), check=False, capture_output=True)
    xdo("windowsize", wid, str(w), str(h))
    xdo("windowmove", wid, str(x), str(y))


def retile():
    try:
        geo = xdo("getdisplaygeometry").split()
        if len(geo) != 2:
            return
        width, height = int(geo[0]), int(geo[1])
        with lock:
            exp = expanded
        # expand is view-only: the wall page zooms into the slot's quadrant,
        # windows always keep their quadrant geometry (no resize on expand)
        for n in range(1, SLOTS + 1):
            wid = slot_window(n)
            if not wid:
                continue
            target = quadrant(n, width, height)
            cur = geometry(wid)
            if cur != target:
                log(f"retile: exp={exp} slot{n} {cur} -> {target}")
                apply_geometry(wid, *target)
            subprocess.run(
                ["wmctrl", "-i", "-r", wid, "-b", "remove,above"],
                env=dict(os.environ, DISPLAY=DISPLAY), check=False, capture_output=True)
            if n == exp:
                xdo("windowactivate", wid)
    except Exception as e:
        log(f"retile error: {e}")


def retile_loop():
    while True:
        time.sleep(2)
        retile()


def pump(src, dst, n):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            last_activity[n] = time.time()
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def proxy_listener(n):
    """CDP forwarder 0.0.0.0:(9250+N) -> 127.0.0.1:(9240+N).

    Replaces the former socat forwards; running it in-process lets every
    forwarded byte refresh the slot's last-activity timestamp (idle release).
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PROXY_BASE + n))
    srv.listen(16)
    while True:
        conn, _ = srv.accept()
        try:
            upstream = socket.create_connection(("127.0.0.1", cdp_port(n)), timeout=5)
        except OSError:
            conn.close()
            continue
        threading.Thread(target=pump, args=(conn, upstream, n), daemon=True).start()
        threading.Thread(target=pump, args=(upstream, conn, n), daemon=True).start()


def measure_usage():
    out = subprocess.run(["ps", "-eo", "rss,pcpu,args"],
                         capture_output=True, text=True).stdout
    stats = {n: [0.0, 0.0, 0] for n in range(1, SLOTS + 1)}
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        rss, pcpu, args = parts
        for n in range(1, SLOTS + 1):
            d = slot_dir[n]
            if d and (f"{d}/" in args or f"{d} " in args or args.endswith(d)):
                stats[n][0] += float(rss) / 1024
                stats[n][1] += float(pcpu)
                stats[n][2] += 1
                break
    for n, (mem, cpu, procs) in stats.items():
        usage[n] = {"mem_mb": int(mem), "cpu_percent": round(cpu, 1), "procs": procs}


def idle_watchdog():
    while True:
        time.sleep(30)
        try:
            measure_usage()
        except Exception as e:
            log(f"usage error: {e}")
        # self-heal: a crashed browser on any slot is relaunched (otherwise a
        # free slot would stay dead until the next claim — hole in the wall)
        for n in range(1, SLOTS + 1):
            if not cdp_alive(n):
                log(f"slot{n} browser is dead -> relaunch")
                try:
                    with lock:
                        launch(n)
                    retile()
                except Exception as e:
                    log(f"slot{n} relaunch failed: {e}")
        cutoff = time.time() - IDLE_MINUTES * 60
        # named-profile slots (original or copy) are never reset automatically
        with lock:
            idle = [n for n in sorted(state)
                    if last_activity[n] < cutoff and not persistent(n)]
            hogs = [n for n in range(1, SLOTS + 1)
                    if n not in state and not persistent(n)
                    and usage[n].get("mem_mb", 0) > SLOT_MEM_MB]
            warn = [n for n in range(1, SLOTS + 1)
                    if (n in state or persistent(n))
                    and usage[n].get("mem_mb", 0) > SLOT_MEM_MB]
        for n in idle:
            log(f"slot{n} idle for {IDLE_MINUTES:g}min -> auto-release")
            release(n)
        for n in hogs:
            log(f"slot{n} (free) uses {usage[n]['mem_mb']}MB > {SLOT_MEM_MB:g}MB -> refresh")
            release(n)
        for n in warn:
            log(f"WARNING: claimed/profile slot{n} uses {usage[n]['mem_mb']}MB "
                f"(cpu {usage[n]['cpu_percent']}%) — not touching it")


WALL_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>remote-chrome wall</title>
<style>
  html, body { margin:0; height:100%; background:#000; overflow:hidden; }
  iframe { position:absolute; inset:0; width:100%; height:100%; border:0;
           transform-origin:0 0; }
  #overlay { position:absolute; inset:0; pointer-events:none; z-index:10; }
  .btn { position:absolute; pointer-events:auto; cursor:pointer; user-select:none;
         text-align:center; font-family:system-ui,sans-serif; color:#fff;
         background:rgba(0,0,0,.45); border:1px solid rgba(255,255,255,.35);
         border-radius:8px; opacity:.55; transition:opacity .15s; }
  .btn:hover { opacity:1; background:rgba(0,0,0,.8); }
  /* tile dividers: black hairline + teal glow -> contrast on light AND dark pages */
  .divider { position:absolute; pointer-events:none;
             background:rgba(0,0,0,.9);
             box-shadow:0 0 0 1px rgba(94,234,212,.35); }
  .frame { position:absolute; pointer-events:none; box-sizing:border-box;
           border:1px solid rgba(255,255,255,.12); }
  .badge { position:absolute; pointer-events:none; text-align:center;
           background:rgba(15,15,15,.65); border:1px solid rgba(255,255,255,.15);
           border-radius:4px; color:rgba(255,255,255,.85);
           font-family:ui-monospace,"SF Mono",monospace; font-weight:600;
           opacity:.8; }
</style>
</head>
<body>
<iframe id="stream" allow="autoplay; clipboard-read; clipboard-write"></iframe>
<div id="overlay"></div>
<script>
const stream = document.getElementById('stream');
stream.src = new URLSearchParams(location.search).get('stream')
          || location.protocol + '//' + location.hostname + ':6081/';
const overlay = document.getElementById('overlay');
let expanded = null;
let aspect = 2880 / 1800;   // updated from /status (desktop geometry)

function contentRect() {
    // largest desktop-aspect rect centered in the window = where the stream is drawn
    const W = innerWidth, H = innerHeight;
    const cw = Math.min(W, H * aspect), ch = cw / aspect;
    return { x: (W - cw) / 2, y: (H - ch) / 2, w: cw, h: ch };
}

function applyZoom() {
    // view-only expand: zoom the stream into the slot's quadrant; the
    // browser window itself keeps its real size
    if (!expanded) { stream.style.transform = ''; return; }
    const W = innerWidth, H = innerHeight, r = contentRect();
    const col = (expanded - 1) % 2, row = (expanded - 1) >> 1;
    const qx = r.x + col * r.w / 2, qy = r.y + row * r.h / 2;
    const qw = r.w / 2, qh = r.h / 2;
    const s = Math.min(W / qw, H / qh);
    const tx = (W - qw * s) / 2 - qx * s, ty = (H - qh * s) / 2 - qy * s;
    stream.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + s + ')';
}

function makeBtn(text, title, x, y, b, onclick) {
    const el = document.createElement('div');
    el.className = 'btn';
    el.textContent = text;
    el.title = title;
    el.style.left = x + 'px';
    el.style.top = y + 'px';
    el.style.width = el.style.height = el.style.lineHeight = b + 'px';
    el.style.fontSize = Math.round(b * 0.5) + 'px';
    el.onclick = onclick;
    overlay.appendChild(el);
}

function el(cls, x, y, w, h) {
    const e = document.createElement('div');
    e.className = cls;
    e.style.left = x + 'px';
    e.style.top = y + 'px';
    e.style.width = w + 'px';
    e.style.height = h + 'px';
    overlay.appendChild(e);
    return e;
}

function badge(n, x, y, k) {
    const s = Math.round(20 * k);
    const e = el('badge', x, y, s, s);
    e.style.lineHeight = s + 'px';
    e.style.fontSize = Math.round(11 * k) + 'px';
    e.textContent = n;
}

function render() {
    applyZoom();
    overlay.innerHTML = '';
    const r = contentRect();
    // size relative to the stream area -> constant physical size at any
    // browser zoom level (fixed px shrink to specks when zoomed out)
    const b = Math.max(34, Math.round(Math.min(r.w, r.h) * 0.055));
    const m = Math.round(b / 4);
    const k = b / 34;                       // scale factor for badges
    el('frame', r.x, r.y, r.w - 2, r.h - 2).style.borderWidth = '1px';
    if (!expanded) {
        el('divider', r.x + r.w / 2 - 1, r.y, 2, r.h);   // vertical
        el('divider', r.x, r.y + r.h / 2 - 1, r.w, 2);   // horizontal
        for (let n = 1; n <= 4; n++) {
            const col = (n - 1) % 2, row = (n - 1) >> 1;
            badge(n, r.x + col * r.w / 2 + 8, r.y + row * r.h / 2 + 8, k);
        }
    } else {
        badge(expanded, r.x + 8, r.y + 8, k);
    }
    // reconnect the stream (the Selkies client does not resume a dropped
    // websocket on its own -> frozen picture until the iframe reloads)
    makeBtn('\\u27f3', 'Stream neu verbinden',
            r.x + r.w - b - m, r.y + r.h - b - m, b,
            () => { stream.src = stream.src; });
    if (expanded) {
        makeBtn('\\u229e', 'Zur\\u00fcck zum Grid',
                r.x + r.w - b - m, r.y + m, b,
                () => fetch('/collapse').then(refresh));
        return;
    }
    for (let n = 1; n <= 4; n++) {
        const col = (n - 1) % 2, row = (n - 1) >> 1;
        makeBtn('\\u26f6', 'Slot ' + n + ' vergr\\u00f6\\u00dfern',
                r.x + (col + 1) * r.w / 2 - b - m, r.y + row * r.h / 2 + m, b,
                () => fetch('/expand?slot=' + n).then(refresh));
    }
}

function refresh() {
    fetch('/status').then(r => r.json()).then(d => {
        expanded = d.expanded;
        const g = String(d.display || '').split(' ').map(Number);
        if (g.length === 2 && g[0] > 0 && g[1] > 0) aspect = g[0] / g[1];
        render();
    }).catch(() => {});
}

addEventListener('resize', render);
setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            body = WALL_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/claim":
            params = self.params(query)
            profile = params.get("profile") or None
            copy = params.get("copy", "") in ("1", "true", "yes")
            if profile and not PROFILE_NAME_RE.match(profile):
                return self.send(400, {"error":
                                       "profile must match [a-z0-9][a-z0-9_-]{0,40}"})
            if copy and not profile:
                return self.send(400, {"error": "copy=1 needs profile=NAME"})
            try:
                n, err = claim(profile, copy)
            except Exception as e:
                return self.send(500, {"error": str(e)})
            if n is None:
                return self.send(409, {"error": err})
            return self.send(200, {"slot": n,
                                   "cdp": f"http://127.0.0.1:{HOST_PORT_BASE + n}",
                                   "mcp": mcp_name(n),
                                   "profile": slot_profile[n],
                                   "copy": slot_copy[n]})
        if path == "/release":
            n = self.slot_param(query)
            if not n:
                return self.send(400, {"error": "slot must be 1..%d" % SLOTS})
            release(n)
            return self.send(200, {"slot": n, "released": True})
        if path == "/unclaim":
            n = self.slot_param(query)
            if not n:
                return self.send(400, {"error": "slot must be 1..%d" % SLOTS})
            unclaim(n)
            return self.send(200, {"slot": n, "unclaimed": True})
        if path == "/profiles":
            try:
                names = sorted(d for d in os.listdir(PERSIST_BASE)
                               if os.path.isdir(f"{PERSIST_BASE}/{d}"))
            except OSError:
                names = []
            with lock:
                return self.send(200, {"profiles": {
                    name: {"open_in_slot": profile_slot(name)} for name in names}})
        if path == "/expand":
            n = self.slot_param(query)
            if not n:
                return self.send(400, {"error": "slot must be 1..%d" % SLOTS})
            global expanded
            with lock:
                expanded = n
            retile()
            return self.send(200, {"expanded": n})
        if path == "/collapse":
            with lock:
                expanded = None
            retile()
            return self.send(200, {"expanded": None})
        if path == "/status":
            with lock:
                slots = {n: {"claimed": n in state,
                             "cdp_alive": cdp_alive(n),
                             "mcp": mcp_name(n),
                             "cdp": f"http://127.0.0.1:{HOST_PORT_BASE + n}",
                             "idle_seconds": (int(time.time() - last_activity[n])
                                              if n in state else None),
                             "profile": slot_profile[n],
                             "copy": slot_copy[n],
                             "usage": usage[n]}
                         for n in range(1, SLOTS + 1)}
                exp = expanded
            return self.send(200, {"slots": slots, "expanded": exp,
                                   "display": xdo("getdisplaygeometry")})
        self.send(404, {"error":
                        "unknown path (/claim[?profile=NAME[&copy=1]] /release?slot=N "
                        "/unclaim?slot=N /profiles /expand?slot=N /collapse /status)"})

    def params(self, query):
        return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}

    def slot_param(self, query):
        params = self.params(query)
        try:
            n = int(params.get("slot", "0"))
        except ValueError:
            return None
        return n if 1 <= n <= SLOTS else None

    do_POST = do_GET


def main():
    threading.Thread(target=retile_loop, daemon=True).start()
    for n in range(1, SLOTS + 1):
        threading.Thread(target=proxy_listener, args=(n,), daemon=True).start()
    threading.Thread(target=idle_watchdog, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    log(f"listening on :{HTTP_PORT}, {SLOTS} slots")
    # the wall always shows all slots
    for n in range(1, SLOTS + 1):
        try:
            ensure_running(n)
        except Exception as e:
            log(f"slot{n} launch failed: {e}")
    retile()
    server.serve_forever()


if __name__ == "__main__":
    main()
