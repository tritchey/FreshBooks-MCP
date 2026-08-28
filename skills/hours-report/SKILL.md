---
name: hours-report
description: Produce a per-day breakdown of time spent on a project from Claude Code session transcripts, estimating actual interaction time rather than wall-clock. Use when asked "how much time did I spend on X", "hours worked broken down by day", "time report", "billable hours", "how long have I been on this project", or when a client- or invoice-facing summary of effort is needed.
---

# Hours report

Reconstructs how much time a person actually spent working with Claude on a project,
from `~/.claude/projects/*/*.jsonl`. The point of difference from naive time tracking:
sessions run for hours while agents work unattended, so **wall-clock badly overstates
effort**. This skill reports attended time, with wall-clock alongside as context.

## 1. Scope the request

Settle three things before running anything. Infer what you can; ask only if a wrong
guess would change the output materially.

- **Date range.** "Since August 11" → `--since 2026-08-11`. Watch the year: transcript
  timestamps are UTC ISO-8601, the script converts to local time.
- **Which projects.** List `~/.claude/projects/` first. One logical project often spans
  several directories — a parent repo plus per-platform subdirectories (`…-Acme`,
  `…-Acme-widgets-ios`, `…-Acme-widgets-android`). Include all of them and
  break them out as separate columns; the split is usually the interesting part.
- **Whether commits matter.** If the directories map to git repos, pass `--repo`.
  Commits per attended hour is the most legible productivity figure in the report.

## 2. Run the script

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/hours-report/scripts/session_hours.py" \
  --since 2026-08-11 \
  --label ios=-Acme-widgets-ios \
  --label android=-Acme-widgets-android \
  --label planning='-Clients-Acme$' \
  --repo ios=~/Projects/Clients/Acme/widgets-ios
```

- `--match SUBSTR` grabs every matching directory and auto-names it; `--label NAME=SUBSTR`
  names one explicitly. A trailing `$` anchors to the end of the directory name — that is
  how you select a parent project without swallowing its subdirectory projects.
- `--json` gives the same data structured, for building the report page.
- `--repo [LABEL=]PATH` adds commit counts. A repo worked on from inside another project's
  sessions (a worktree, a sibling library) gets its own row with zero time and real
  commits — say so in the report rather than letting it look like free work.
- `--round MIN` (default **15**) rounds each project's daily figure up to that increment,
  giving the **billable** column. `--round 0` reports raw time only.

**Billable vs. raw.** Rounding applies per project per day, off the midpoint of the
low-high range — each project is its own invoice line, so each rounds separately, and a
day touching three projects rounds three times. Report billable as the headline, but
always show raw alongside and state what rounding added. A billable figure whose
derivation is invisible is the one a client challenges.

Read `references/method.md` before explaining or defending any number.

## 3. Sanity-check before writing

Three checks, every time:

- **Spot-read the days.** Pull the messages the person typed on the biggest and smallest
  days so the narrative describes real work, not invented work. Grep the transcripts for
  `"type":"user"` or re-run with `--json`, then quote actual phrasing.
- **Look for a prior estimate.** If they have asked this before, find that answer in the
  transcripts and match its method, or explicitly say what changed and why. Numbers that
  silently move between reports destroy trust in all of them.
- **Question outliers.** A day where attended time approaches wall-clock usually means work
  happened outside the session (a console, a device, an IDE) — that day is *undercounted*.
  A day at 10% attended means a long autonomous run. Both belong in the writeup.

## 4. Write the report

Lead with the billable total, with the raw estimate and its range right behind it —
`12h 30m billable · 10h 18m raw (7h 39m – 12h 57m)`. Never a bare point estimate, never a
range with no recommended figure, and never the billable number on its own.

Include:

- **The daily table** — per-project billable columns, the day's raw figure, wall-clock,
  typed messages, commits.
- **Attended vs. session, visually.** The gap between the two is the story; a bar per day
  on one shared scale shows it faster than any sentence.
- **Three or four narrative beats**, each anchored to a date and a real number: the busiest
  day, the least attended day, the most hands-on day, the anomaly. Quote what they typed.
- **The method, stated plainly** — caps, break threshold, rounding increment, what counts
  as a message. Show what rounding added as its own figure, not folded into the total.
- **Caveats.** Always include the big one: this sees Claude Code sessions only. Time in an
  IDE, on a device, in a web console, or reviewing PRs is invisible, so the figure is a
  floor. Name the specific days most affected.

Do not pad the estimate to look better and do not round toward a billable number. If the
person plans to invoice from this, the caveats are the most important part of the page.

## 5. Publish it

These reports get forwarded — to a client, a partner, an accountant. Publish as an artifact
and hand over the link. Load `artifact-design`, then follow
`references/report-design.md` for the visual identity, so repeat reports for the same
person look like a series rather than a fresh invention each time.

Also give the headline number and the table in the terminal. Do not make them open a link
to learn the answer to what they asked.
