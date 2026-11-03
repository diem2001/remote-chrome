# SETUP — remote-chrome MCP servers for Claude Code

Step-by-step instructions to wire the remote-chrome browser slots into Claude
Code. One `chrome-devtools-mcp` server entry per slot; the naming scheme is
**`remote-chrome-slotN`** (N = 1…4).

## Prerequisites

- The stack is running: `docker compose up -d` in this directory. The base
  `docker-compose.yml` is self-contained; site-specific extras (e.g. attaching
  the slots to a shared reverse-proxy network) live in
  a git-ignored `docker-compose.override.yml` (see
  `docker-compose.override.example.yml`). If your override references an
  external network that doesn't exist yet, create it first
  (`docker network create <name>`) or start the stack that provides it.
- Health check — both must succeed:

  ```sh
  curl -s http://localhost:9230/status | jq .   # slot API (all four slots, cdp_alive)
  curl -s http://localhost:9222/json/version    # CDP of slot 1
  ```

## Register the MCP servers

Run from the directory tree where your Claude Code sessions live (the servers
are registered at **project scope** — they apply to every session started
under the directory you run the commands from):

```sh
claude mcp add remote-chrome-slot1 -- npx -y chrome-devtools-mcp@latest --browserUrl=http://127.0.0.1:9222
claude mcp add remote-chrome-slot2 -- npx -y chrome-devtools-mcp@latest --browserUrl=http://127.0.0.1:9223
claude mcp add remote-chrome-slot3 -- npx -y chrome-devtools-mcp@latest --browserUrl=http://127.0.0.1:9224
claude mcp add remote-chrome-slot4 -- npx -y chrome-devtools-mcp@latest --browserUrl=http://127.0.0.1:9225
```

Optional but recommended — a real Chrome on the Mac (own profile, spawned on
demand), useful for testing "user-side" behaviour:

```sh
claude mcp add chrome-devtools-local -- npx -y chrome-devtools-mcp@latest
```

Notes:

- `--browserUrl` makes the MCP server attach to the already-running slot
  browser instead of spawning its own. If the stack is down, tool calls fail —
  start it first.
- Port mapping is fixed: slot N ⇔ host port `9221+N` (9222…9225).
- Use `-s user` instead of the default project scope if the servers should be
  available outside that directory as well.

## Verify

```sh
claude mcp list        # all four remote-chrome-slotN: ✔ Connected
```

In a running Claude Code session, run `/mcp` (or start a new session) so newly
registered servers are loaded. Smoke test: ask Claude to open a page via
`remote-chrome-slot1` and watch it live on the wall (http://localhost:6080).

## Usage contract (for sessions/agents)

1. `curl -s localhost:9230/claim` → reserves a free slot, response names the
   MCP server to use, e.g. `{"slot":2,...,"mcp":"remote-chrome-slot2"}`.
2. Drive the browser exclusively through that server's tools.
3. `curl -s "localhost:9230/release?slot=2"` when done — the slot is killed,
   its profile wiped and relaunched fresh for the next agent. Safety net:
   reserved slots without CDP traffic for 15 minutes (`IDLE_MINUTES` in
   docker-compose.yml) are auto-released.

Rules: one slot per concurrently driving agent (each MCP server has a single
selected-page cursor); never open the wall/stream pages (:6080/:6081) from an
agent — that steals the human viewer's stream. Screenshots saved as files
(`take_screenshot` with `filePath`) only work for paths under the MCP client's
workspace roots or the OS temp dir (`$TMPDIR`) — details in
[CLAUDE.md](./CLAUDE.md).

## Removal / rename

```sh
claude mcp remove remote-chrome-slot1   # etc.
```

The entries live in `~/.claude.json` under
`projects."<project root you registered from>".mcpServers` if manual edits are
ever needed.
