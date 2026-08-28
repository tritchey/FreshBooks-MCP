# FreshBooks MCP

An MCP server that connects Claude Code to a FreshBooks account, plus two companion
skills: `hours-report`, which estimates attended hours from Claude Code session
transcripts, and `freshbooks-timesheet`, which turns those hours into FreshBooks time
entries — with a human review step before anything is pushed.

## Pieces

- **`src/freshbooks_mcp/`** — Python MCP server (FastMCP, stdio). OAuth2 against the
  FreshBooks API; tools for projects, clients, and idempotent time-entry upserts.
- **`skills/hours-report/`** — Claude Code skill (with `scripts/session_hours.py`) that
  reconstructs attended time per day and per project from `~/.claude/projects/*/*.jsonl`,
  separating it from session wall-clock and rounding to billable increments. Usable on
  its own; `freshbooks-timesheet` depends on it.
- **`skills/freshbooks-timesheet/`** — Claude Code skill that orchestrates:
  hours-report → label→project mapping → proposed-entries table → approval → `log_time`.
- **`~/.freshbooks-mcp/`** — local state: OAuth tokens, label→project mapping, and a
  ledger of time-entry ids this tool created (so re-syncs update instead of duplicate,
  and hand-entered time is never touched).

## Setup

### 1. Create a FreshBooks app (one time)

1. Go to <https://my.freshbooks.com/#/developer> and create an application.
2. Scopes: `user:profile:read`, `user:clients:read`, `user:projects:read`,
   `user:time_entries:read`, `user:time_entries:write`.
3. Redirect URI: `https://localhost:8414/callback` (it never needs to serve anything —
   FreshBooks requires an HTTPS URI, and the auth flow copies the code from the address bar).
4. Note the **Client ID** and **Client Secret**.

### 2. Store the app credentials

Create `~/.freshbooks-mcp/credentials.json` (mode 0600):

```json
{"client_id": "<your-client-id>", "client_secret": "<your-client-secret>"}
```

(Alternatively set `FRESHBOOKS_CLIENT_ID` / `FRESHBOOKS_CLIENT_SECRET` env vars on the
server registration. `redirect_uri` defaults to `https://localhost:8414/callback`; add
it to the JSON only if your app registered something else.)

### 3. Register the server with Claude Code

```bash
claude mcp add --scope user freshbooks \
  -- uv run --directory /Users/tritchey/Projects/RedRomeLogic/FreshBooks-MCP freshbooks-mcp
```

### 4. Install the skills

Symlink both skill directories into `~/.claude/skills/` so edits in the repo are picked
up immediately. `freshbooks-timesheet` invokes `hours-report` at
`~/.claude/skills/hours-report/`, so both must be installed. From the repo root:

```bash
mkdir -p ~/.claude/skills
for s in hours-report freshbooks-timesheet; do
  ln -s "$(pwd)/skills/$s" ~/.claude/skills/$s
done
```

### 5. Authenticate (one time, and again only if tokens are lost)

In a Claude Code session: ask Claude to connect to FreshBooks. It calls `get_auth_url`;
open the URL, approve, and the browser lands on the (non-loading) redirect page — copy
the `code=` value from the address bar and give it to Claude, which calls
`submit_auth_code`. `whoami` confirms the connection. Tokens auto-refresh from then on;
refresh tokens are single-use, so the server persists each new pair atomically.

## Use

> "Update my FreshBooks timesheets for last week."

Claude runs the hours-report skill, maps project labels to FreshBooks projects (asking
once per new label), shows the proposed entries (day × project × hours × note, marked
create/update/no-change), and pushes only after approval.

## Tools

| Tool | Purpose |
|------|---------|
| `get_auth_url` / `submit_auth_code` | OAuth connect flow |
| `whoami` | Auth health check; identity + business |
| `list_projects` / `list_clients` | Account data, for building the mapping |
| `get_mapping` / `set_mapping` | hours-report label → FreshBooks project |
| `list_time_entries` | What's logged in a range, flagged if created by this tool |
| `log_time` | Idempotent upsert of per-day entries (create/update/unchanged per entry) |
| `delete_time_entry` | Delete — refuses entries this tool didn't create |

## Safety properties

- Entries not created by this tool (not in the ledger) are never updated or deleted.
- Re-running a sync for an overlapping range updates in place; no duplicates.
- Token/state files live outside the repo with 0600 permissions; nothing secret is committed.

## Development

```bash
uv sync
uv run pytest
```
