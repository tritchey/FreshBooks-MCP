# Report page design

An hours report is a **ledger**, not a dashboard and not an essay. It gets forwarded to
people who will scan for one number and then look for a reason to trust it. Design for
that: legible figures, visible method, no salesmanship.

Keep this identity stable across reports for the same person — repeat reports should read
as a series. Load `artifact-design` first; this narrows it, it does not replace it.

## The reference identity: greenbar

The source material is machine log output, and the ancestor of exactly this document is
continuous-feed greenbar accounting paper. That is the reference — apt, and nowhere near
the AI-default cream-and-terracotta look.

**Color** — a warm-neutral paper with a green bias, alternating row tint, and one accent
per project.

```
--paper  #F1F3EC   --bar   #E0E8DC   --card  #F8F9F5
--rule   #C9D1C4   --ink   #1D231E   --ink-mid #4E574D   --ink-soft #79826F
--ghost  #CDD5C8   (wall-clock track)
series:  #2F5D50 pine · #4A5C86 slate · #8A7332 ochre
dark:    paper #131714 · bar #1B211C · card #191E1A · rule #333B33
         ink #E2E7DD · mid #A8B2A4 · soft #7E887B · ghost #2C332C
         series #71B69D · #93A9DC · #C9AE72
```

**Type** — three roles, each earning its place:

- `Archivo` 600/700, tight tracking — headings and the big figures
- `Source Serif 4` — prose, notes, caveats. The serif is what makes it read as a document
  rather than an analytics screen.
- `IBM Plex Mono` — every number, every table cell, every uppercase label. Always with
  `font-variant-numeric: tabular-nums`.

**Layout** — single column, ~1080px. Masthead with a hairline rule under it, then the
headline figure paired with three stat tiles, then the ledger, then per-project cards,
then narrative notes, then method and caveats.

## The ledger table

The centerpiece. Rules that matter:

- Alternating row tint on odd rows — the greenbar stripe, and it genuinely helps track
  across a wide row.
- Right-align every number; left-align only the day column. Tabular figures throughout.
- Per-project cells carry the project's accent color and show the **billable** figure. A
  day with no activity gets a dimmed em dash, never a zero — zero implies measurement, the
  dash correctly implies absence.
- Give raw time its own column, set in `--ink-soft` beside the billable total. Quieter than
  the billable figure, but present on every row — that contrast is the honesty of the page.
- `overflow-x: auto` on the table's own container. Never let the page body scroll sideways.
- Footer row separated by a 2px rule in full ink.

## The attended-vs-session bar

One bar per day, all on one shared scale, in its own column. A ghost track for wall-clock
with a tick marking its end, and a filled segment for attended time colored by that day's
dominant project. Nothing else communicates the leverage gap as quickly, and it is the
column people look at second, right after the total.

State the scale in the legend ("bars share one scale — Aug 24 = 11h 16m"). Clamp attended
to the track when it slightly exceeds wall-clock, and say why in the caveats.

## Tone of the copy

Plain and specific. Name real days, quote what the person actually typed, give real
numbers. No congratulation, no "productivity unlocked," no exclamation marks. The reader
is deciding whether to trust the figure, and confidence in a report like this comes from
visible limits, not from enthusiasm.

Put the caveats on the page in full. A report that hides them is worth less than one that
prints them, because the first thing a careful reader asks is what the method missed.
