# remote-chrome — a browser "control room" for agents

A standalone Docker stack that runs up to **4 independent, non-headless
Chromium instances ("slots")** inside one container and exposes them two ways
at once:

1. **Chrome DevTools Protocol (CDP)**, one port per slot
   (`localhost:9222…9225`) — for programmatic control by tools such as the
   Claude Code `chrome-devtools-mcp` server. Multiple Claude sessions/agents
   can each drive their own browser concurrently.
2. **One web panel** on `http://localhost:6080` — a live wall of all slots,
   streamed via WebRTC ([Selkies](https://github.com/selkies-project)). The
   desktop is fixed at **2880×1800** with the four slots in fixed quadrants,
   so every tile is a genuine **1440×900 browser** (a 13" MacBook viewport); the client scales the
   stream down to your panel window. Hovering a tile shows an **⛶ overlay
   button** that zooms the view into that slot for detailed inspection
   (view-only: the browser window keeps its real 1440×900; ⊞ returns to the
   grid). A human can watch and take over mouse/keyboard in any tile at any
   time.

All four browsers run permanently — the wall always shows the full grid.
Claiming only *reserves* a slot for an agent; releasing it kills the browser,
wipes the profile and relaunches fresh, so **no state ever leaks between
agents**. The browsers are isolated from your personal Chrome profile and
from each other (separate profiles, separate CDP ports). When a login must
survive, claim the slot with a **named persistent profile** instead (see
below).

Based on [`linuxserver/chromium`](https://docs.linuxserver.io/images/docker-chromium/)
(Selkies + labwc/Wayland session), with a few overrides described below.

## Quick start

```sh
cd remote-chrome
docker compose up -d      # start (first run pulls the image)
docker compose down       # stop — the browser profile volume survives
```

| What | Where |
|---|---|
| Web panel — the wall with expand/collapse overlay | http://localhost:6080/ |
| Raw Selkies stream (embedded by the wall page) | http://localhost:6081/ |
| CDP slot 1 (always up) | http://localhost:9222 |
| CDP slots 2–4 (on claim) | http://localhost:9223 … 9225 |
| Slot API | http://localhost:9230 — `GET /claim[?profile=NAME[&copy=1]]`, `/release?slot=N`, `/unclaim?slot=N`, `/profiles`, `/expand?slot=N`, `/collapse`, `/status` |

All ports are bound to `127.0.0.1` only: the CDP ports give full control
over the browsers (including logged-in sessions) without authentication.

```sh
curl -s localhost:9230/claim            # reserve a free slot -> {"slot":2,...,"mcp":"chrome-slot2"}
curl -s "localhost:9230/claim?profile=instagram"         # slot on a named persistent profile
curl -s "localhost:9230/claim?profile=instagram&copy=1"  # parallel throwaway copy of it
curl -s localhost:9230/release?slot=2   # kill + relaunch fresh (throwaway wiped, named profile kept)
curl -s localhost:9230/unclaim?slot=2   # only un-reserve; browser keeps running with its tabs
curl -s localhost:9230/profiles         # named profiles + which slot has them open
curl -s localhost:9230/expand?slot=2    # wall view zooms into slot 2 (view-only)
curl -s localhost:9230/collapse         # back to the 2x2 grid
curl -s localhost:9230/status           # all slots, expanded state, display size
```

The wall page (served by the spawner) embeds the Selkies stream in an iframe
and draws the overlay buttons itself — expand/collapse per click, no
keyboard shortcuts needed.

The base `docker-compose.yml` is self-contained. Site-specific extras (e.g.
attaching the container to a shared Docker network to reach local dev
stacks) go into a git-ignored `docker-compose.override.yml` — see
`docker-compose.override.example.yml` and "Reaching local dev stacks" below.

## Claude Code integration (MCP)

One `chrome-devtools-mcp` server per slot, named **`remote-chrome-slotN`**
(slot N ⇔ host port `9221+N`), registered at project scope for the directory your Claude Code sessions run in.
Full registration walkthrough: **[SETUP.md](./SETUP.md)**.

Workflow for a session/agent that needs a browser:

1. `curl -s localhost:9230/claim` → returns the slot and the matching MCP
   server name (e.g. `remote-chrome-slot2`).
2. Use exactly that MCP server's tools.
3. `curl -s localhost:9230/release?slot=N` when done — the slot is wiped,
   relaunched fresh and free for the next agent. Forgotten claims release
   themselves after `IDLE_MINUTES` (default 15) without CDP traffic.

Rules of thumb: **one slot per concurrently driving agent** (each MCP server
has a single "selected page" cursor — two agents on one slot will race).
After adding servers, run `/mcp` (or start a new session) to load them.

## Architecture

```
docker: remote-chrome  (lscr.io/linuxserver/chromium)
├─ selkies      WebRTC remote desktop, web UI on :3000 (mapped to host :6080)
├─ labwc        Wayland compositor (window management)
└─ spawner.py   session entrypoint (autostart): HTTP API + wall page on :9230
                (host :6080), keeps all four Chromiums running (profile
                /config/profiles/slotN, CDP 127.0.0.1:924N), enforces the
                fixed-quadrant tiling incl. expand/collapse (xdotool/wmctrl)
                and forwards each slot's CDP port (0.0.0.0:925N -> 127.0.0.1:
                924N, mapped to host 922N) — every forwarded byte refreshes
                the slot's activity clock

volumes: kasm-config (slot profiles live under /config/profiles/, wiped on release)
```

Slots run as **X11 clients (XWayland)** on purpose: native Wayland clients
cannot be positioned programmatically, X11 windows can — that is what makes
the tiling work. The desktop resolution is pinned via
`SELKIES_IS_MANUAL_RESOLUTION_MODE` + `SELKIES_MANUAL_WIDTH/HEIGHT` in
docker-compose.yml (Selkies applies it when a viewer connects; without a
viewer the output idles at 1280×720 — the retile loop follows either way).

- **`container/autostart`** (mounted to `/config/.config/labwc/autostart`): runs the
  spawner in a restart loop. Chromium flags live in `container/spawner.py` — do **not**
  use the image's `CHROME_CLI` env for flags containing spaces; it is expanded
  unquoted and the fragments end up opened as URLs.
- **`container/rc.xml`** (mounted to `/config/.config/labwc/rc.xml`): labwc config with
  the titlebar buttons, the Alt-F4/Alt-Escape close keybinds, and — crucially —
  the lsio rule `<windowRule identifier="*"><action name="Maximize"/>` removed
  (maximized windows ignore configure requests, which breaks tiling). Slot
  windows are undecorated. Chromium's own window buttons are disabled via the
  seeded profile preference `browser.custom_chrome_frame=false`: there is no
  taskbar in the session, so a minimized window would be unrecoverable by
  mouse, and a closed Chromium stays dead until re-claimed.
- **CDP proxying** lives inside the spawner (Chromium binds its CDP ports to
  127.0.0.1 only). This doubles as usage metering: a **reserved slot with no
  CDP traffic for `IDLE_MINUTES` (default 15) is auto-released** — killed,
  wiped, relaunched fresh — so forgotten claims never block the pool.
  `/status` shows `idle_seconds` per reserved slot. wmctrl is installed at
  start via the lsio `universal-package-install` mod.
- **Resource guardrails** (the VM is 4 CPUs / 16 GB shared with all other
  stacks): the container is capped at `cpus: 3` / `mem_limit: 6g`; each slot
  Chromium runs with `nice -n 5` (the stream stays responsive under load) and
  `--renderer-process-limit=4` (caps the process explosion of ad-heavy
  pages). The spawner samples per-slot RSS/CPU every 30 s (`/status` →
  `usage`); a **free** slot exceeding `SLOT_MEM_MB` (default 3000; a single YouTube tab needs ~1.5–2 GB) is
  refreshed automatically, a **claimed** slot only logs a warning (an agent's
  browser is never yanked away).
- **`container/cont-init.d/10-chromium-policies`**: writes the managed policy
  `TranslateEnabled:false` (the translate bubble cannot be disabled via flags
  in current Chromium, and a profile pref would be lost on slot wipe).

## Named persistent profiles

`/claim?profile=NAME` (re)starts a free slot on `/config/persistent/NAME`
(created on first use, lives in the `kasm-config` volume). Cookies, logins
and sessions survive releases, container recreates and `docker compose
down` (only `down -v` wipes them).

- A profile can be open in **one slot at a time** — Chromium locks its
  profile directory, and two processes writing the same cookie/history
  databases would corrupt them. A second claim of the same profile is
  rejected (409, naming the slot). `&copy=1` starts a **throwaway copy**
  instead (logged in as well; changes are discarded on release).
- Profile slots (original or copy) are **never auto-released** (idle) and
  never memory-refreshed; `/release` closes them, `/unclaim` only drops
  the reservation.
- On release the browser is closed via CDP `Browser.close`, a clean
  shutdown that flushes the cookie store — a plain SIGTERM loses cookies
  set in the last seconds.
- Stale `Singleton*` locks (e.g. after a recreate, which changes the
  container hostname) are cleared before launch; exclusive use is enforced
  by the spawner.
- Delete a profile: `docker exec remote-chrome rm -rf /config/persistent/NAME`
  (only while it is not open).

Profile data is stored as Chromium writes it with `--password-store=basic`,
i.e. effectively unencrypted inside the volume.

## Fullscreen inside the slot

A page entering fullscreen (e.g. YouTube's fullscreen button) would make the
slot's Chromium cover the whole shared desktop. The bundled extension
`container/fullscreen-in-window/` (loaded into every slot via
`--load-extension`) emulates the Fullscreen API inside the slot window
instead: element fullscreen is put into the top layer and stretched over
the viewport, whole-page fullscreen (`<html>`) only reports the state.

## Display scaling

`SELKIES_SCALING_DPI: "96"` locks the session DPI. Without it a HiDPI viewer
(e.g. a Retina Mac) pushes a scale of 1.75–2.0, which shrinks the logical
desktop (e.g. to 1645×1028) and every slot's CSS viewport to ~822 px.

## Reaching local dev stacks (optional)

Dev domains that resolve to 127.0.0.1 on the host are useless inside the
container. Attach the container to the Docker network of your reverse proxy
and let the slot browsers map the domains directly to the proxy container via
`HOST_RESOLVER_RULES` (passed to Chromium as `--host-resolver-rules`), e.g.
in `docker-compose.override.yml`:

```yaml
environment:
  HOST_RESOLVER_RULES: "MAP *.dev.example my-reverse-proxy"
```

See `docker-compose.override.example.yml`. `--ignore-certificate-errors`
takes care of self-signed certificates. This only affects Chromium — `curl`
inside the container will not resolve these names.

## Operations

- **Apply changes to files under `container/`**:
  `docker compose up -d --force-recreate` (mounted read-only; a restart
  re-reads them). Claims are reset; slot 1 comes back automatically.
- **Window lost?** `python3 tools/restore-window.py` (needs
  `pip3 install websocket-client` once) un-minimizes the slot-1 window via
  CDP. Simpler: release + re-claim the slot. Last resort:
  `docker compose restart`.
- A container restart resets all claims (slot profiles are wiped on the next
  claim anyway). `down -v` additionally wipes the volume — nothing needs to be
  restored manually; slot profiles are seeded fresh on every claim.
- GL/GPU errors in `docker logs` are expected (no GPU in the container,
  Chromium renders in software) and harmless.
- **Video playback SIGILL on Apple-Silicon hosts with SME** (M4/M5+): the
  guest kernel advertises SME/SME2, Apple Virtualization does not execute it
  correctly, and Chromium's libyuv (VideoFrameCompositor) uses it on the
  first video frame — every video crashes the renderer with `Aw, Snap!
  SIGILL`, regardless of codec. Fix at the VM level (once per guest):
  `arm64.nosme` in `GRUB_CMDLINE_LINUX` + `update-grub` + graceful VM
  restart. Diagnose with `sysctl kernel.print-fatal-signals=1` in the VM,
  then `dmesg` shows `chromium: VideoFrameCompo: ... fatal signal 4`.
- **Spawner log**: `docker exec remote-chrome tail -f /config/spawner.log` —
  every claim/release/retile decision is logged there (not in `docker logs`).
- Without a connected viewer the desktop idles at 1280×720; the fixed
  2880×1800 applies when a viewer connects. The retile loop re-fits the
  slots within ~2s either way — sizes in between are not a bug.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | the stack: image, ports, volumes, networks |
| `container/spawner.py` | slot manager: claim/release/status API + adaptive tiling |
| `container/autostart` | session entrypoint: runs the spawner (mounted over the lsio default) |
| `container/rc.xml` | labwc config: no titlebar buttons, no close keybinds, no force-maximize |
| `container/cont-init.d/10-chromium-policies` | managed policy: translate bubble off |
| `container/fullscreen-in-window/` | extension: page fullscreen stays inside the slot window |
| `docker-compose.override.example.yml` | template for site-specific extras (network, `HOST_RESOLVER_RULES`) |
| `tools/restore-window.py` | emergency: un-minimize a browser window via CDP |
