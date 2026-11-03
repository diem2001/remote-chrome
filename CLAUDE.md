# CLAUDE.md — remote-chrome (browser control room)

Containerized browser "control room" (image `lscr.io/linuxserver/chromium`,
Selkies/WebRTC + labwc/Wayland): up to **4 independent Chromium slots**, each
with its own profile and CDP port, tiled live on one web panel
(http://localhost:6080). Full docs: [README.md](./README.md).

## Slots and MCP servers

All four slot browsers run permanently (fixed 2×2 wall on a 2880×1800
desktop, each tile a real 1440×900 browser, a 13" MacBook viewport). Claiming *reserves* a slot;
releasing wipes + relaunches it fresh. The wall page (host :6080, served by
the spawner) embeds the Selkies stream (:6081) and provides expand/collapse
overlay buttons.

| Slot | CDP (host) | MCP server |
|---|---|---|
| 1 | :9222 | `remote-chrome-slot1` |
| 2 | :9223 | `remote-chrome-slot2` |
| 3 | :9224 | `remote-chrome-slot3` |
| 4 | :9225 | `remote-chrome-slot4` |

MCP registration commands: see [SETUP.md](./SETUP.md).

`chrome-devtools-local` (also project scope) drives a real Chrome on the Mac
instead — pick deliberately.

**Claim/release protocol** (spawner API on :9230):

```sh
curl -s localhost:9230/claim            # -> slot number + matching MCP name
curl -s "localhost:9230/claim?profile=NAME"         # slot on named persistent profile
curl -s "localhost:9230/claim?profile=NAME&copy=1"  # throwaway copy of that profile
curl -s localhost:9230/release?slot=N   # kill + relaunch fresh (throwaway wiped, named profile kept)
curl -s localhost:9230/unclaim?slot=N   # only un-reserve; browser keeps running with its tabs
curl -s localhost:9230/profiles         # named profiles + which slot has them open
curl -s localhost:9230/expand?slot=N    # wall view zooms into slot N (view-only, window keeps 1440x900)
curl -s localhost:9230/collapse         # back to the grid
curl -s localhost:9230/status
```

(The human can expand/collapse via the ⛶/⊞ overlay buttons on the wall page.)

- Claim a slot before using its MCP tools; release it when done. Slot state is
  shared machine-wide — other Claude sessions/agents use the same pool.
- **Idle auto-release**: a reserved slot with no CDP traffic for 15 minutes
  (`IDLE_MINUTES` env) is released automatically (killed + wiped + fresh).
  Don't park work in a slot and come back an hour later.
- **Resource monitoring**: `/status` shows per-slot `usage` (mem_mb,
  cpu_percent, procs). Free slots above `SLOT_MEM_MB` (3000) auto-refresh;
  claimed slots only log a warning — keep your slot lean (close heavy tabs).
  The container is capped at 3 CPUs / 6 GB total.
- **One slot per concurrently driving agent.** Each MCP server has one
  "selected page" cursor; parallel subagents on the same slot will race.
- Released slots are wiped — never park anything valuable in a throwaway profile.
- **Named profiles** (`/config/persistent/NAME` in the volume) keep cookies,
  logins and sessions across releases and container recreates. A profile can
  be open in only one slot at a time (Chromium locks the profile dir); use
  `copy=1` for a parallel throwaway copy (changes discarded on release).
  Profile slots (original or copy) are never auto-released or memory-reset.
  On release the browser is closed via CDP `Browser.close` so cookies are
  flushed to disk (SIGTERM loses recently set cookies). Delete a profile
  manually: `docker exec remote-chrome rm -rf /config/persistent/NAME`
  (only while it is not open).
- **Fullscreen**: the `fullscreen-in-window` extension (loaded into every
  slot) keeps page fullscreen (e.g. YouTube) inside the slot window instead
  of covering the whole shared desktop.
- **Screenshots as files**: `take_screenshot` with `filePath` only accepts paths
  under the announced workspace roots (the directory Claude Code runs in) or `os.tmpdir()` (`$TMPDIR`,
  i.e. `/var/folders/…/T`) — the session scratchpad and `/tmp` are rejected
  ("not within any of the configured workspace roots"). Convention: save to
  `remote-chrome/screenshots/` (git-ignored). Inline screenshots (no `filePath`)
  are unaffected.

## Operating rules

- Start/stop with `docker compose up -d` / `down` in this directory. Never
  `docker rm -f`. Health: `curl -s localhost:9230/status`.
- everything under `container/` is mounted
  read-only; apply edits with `docker compose up -d --force-recreate`
  (resets claims; slot 1 auto-returns).
- Chromium flags with spaces belong in `container/spawner.py`, never in `CHROME_CLI`
  (unquoted expansion splits them).
- The session is Wayland, but **slots run as X11 clients (XWayland)** so the
  spawner can tile them (`DISPLAY=:0`, xdotool works as user `abc`).
- Do not kill a slot's Chromium process directly — release/claim it instead.
  Do not re-add the lsio force-maximize rule to rc.xml (breaks tiling).
- A human may be watching the panel while agents drive; announce disruptive
  actions (container restarts kill all slots).
- **Never open the wall page (:6080) or the raw stream (:6081) in a browser
  yourself** — Selkies serves one viewer well; another client freezes or
  steals the human's stream. Verify visually via slot CDP screenshots, or
  geometry via `xdotool` in the container.
- Optional `HOST_RESOLVER_RULES` (env, e.g. set in a git-ignored
  `docker-compose.override.yml`, see the `.example.yml`) maps dev domains to a
  reverse-proxy container on a shared Docker network. It only affects the slot
  browsers, NOT `curl` inside the container.
