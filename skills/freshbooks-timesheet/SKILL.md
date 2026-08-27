---
name: freshbooks-timesheet
description: Push billable hours computed from Claude Code sessions into FreshBooks time entries. Use when asked to "update my timesheet", "log my hours in FreshBooks", "push time to FreshBooks", "sync my timesheets", or to bill tracked hours for a client or date range. Requires the freshbooks MCP server and the hours-report skill.
---

# FreshBooks timesheet sync

Turns the hours-report skill's per-day billable figures into FreshBooks time entries via
the `freshbooks` MCP tools. The contract: **nothing is pushed until the person has seen
and approved the exact entries.** FreshBooks time entries feed invoices; a silent wrong
number here becomes a billing dispute later.

## 1. Compute the hours

Follow the **hours-report** skill (`~/.claude/skills/hours-report/`) to scope the request
and run its script — same date-range and project-selection rules, but with `--json`:

```bash
python3 ~/.claude/skills/hours-report/scripts/session_hours.py \
  --since 2026-08-18 --label myclient=-SomeProject --json
```

Use each day's `billable_min` (already rounded per that skill's method) as the minutes to
log. Keep `attention_mid_min` and `wall_min` on hand for the review table so the person
can sanity-check what the rounding did.

If auth might be stale, call `whoami` first; if it fails, run the auth flow from the
server README (`get_auth_url` → approve in browser → `submit_auth_code`).

## 2. Map project labels to FreshBooks projects

Call `get_mapping`. For any hours-report label with no mapping, call `list_projects`,
propose the closest title match, and **confirm with the person before saving** with
`set_mapping` — a wrong mapping bills the wrong client. Mappings persist in
`~/.freshbooks-mcp/mapping.json`, so this is usually a first-run-only step.

## 3. Diff against what's already logged

Call `list_time_entries` for the date range. Entries marked `owned_by_ledger` were
created by this workflow and will be updated in place; anything else is hand-entered and
will never be touched (the server enforces this). If hand-entered time already covers a
day you are about to log, flag the overlap in the review table instead of silently
double-billing the day.

## 4. Propose, then wait

Show a table before pushing anything:

| Date | Project | Hours | Action | Note |
|------|---------|-------|--------|------|

- **Hours** from `billable_min`; also state the raw midpoint total so the rounding is visible.
- **Action** is create / update / no change, from the step-3 diff.
- **Note** becomes the FreshBooks entry note and can end up on an invoice. Write it from
  what actually happened that day — spot-read the transcripts as the hours-report skill
  directs and describe the real work in client-appropriate language ("Implemented CSV
  export and fixed pagination in the admin list"), never boilerplate like "development work".

Then **stop and ask for approval**. Apply any edits (drop a day, reword a note, adjust
hours) before pushing. Never treat the original "update my timesheet" request as
approval of the specific numbers.

## 5. Push and report

Call `log_time` with the approved entries. Report the per-entry results (created /
updated / unchanged / failed, with FreshBooks entry ids), the total hours logged, and
any failures with their errors. If an entry failed, fix and retry just that entry rather
than resubmitting the whole batch.

## Cautions

- The hours are a **floor** — Claude Code sessions only. Repeat the hours-report skill's
  caveat when presenting totals, and name days likely undercounted.
- One entry per project per day, `started_at` 09:00 local. Duration is what FreshBooks
  invoices care about; the start time is cosmetic.
- Re-running for an overlapping range is safe: owned entries update, others are ignored.
- Deleting: `delete_time_entry` only works on entries this workflow created. For anything
  else, point the person at the FreshBooks UI.
