# What the numbers mean

Three quantities, computed from transcript timestamps. Know the difference before you
put any of them in front of a client.

## Attended time ("your time")

The headline. For each message the person **actually typed**:

- **Read-and-compose** — the gap since the previous assistant reply, capped at 5 minutes
  (low estimate) or 10 (high). The cap is the whole trick: median gap before sending is
  usually 2–3 minutes, but p90 runs past an hour. That tail is someone walking away, and
  the cap is what removes it.
- **Watch window** — 1 minute (low) or 2 (high) after they hit enter, for reading the
  response they just triggered.

Overlapping windows are merged before summing, so rapid back-and-forth is not
double-counted. Intervals that cross midnight are split across both days.

Report the range and its midpoint. The low and high are not error bars in a statistical
sense — they are two defensible readings of the same evidence, and the honest answer is
"somewhere in here."

## Billable time

The midpoint, rounded **up** to the next 15 minutes (`--round`), **per project per day**.
Each project is a separate invoice line, so each rounds separately: a day touching three
repos rounds three times and can gain up to 45 minutes.

Two rules for reporting it:

- **Never show billable without raw.** State what rounding added as its own figure. A
  rounded number whose derivation is hidden invites exactly the challenge you don't want.
- **A project-day with no activity stays zero.** Rounding lifts a real 20 seconds to 15
  minutes; it must never conjure time out of a day with nothing on it.

Rounding up is a billing convention, not a measurement. It sits on top of an estimate that
is already a floor (see limits below), which is what makes it defensible here — but say so
rather than letting the two effects quietly cancel in the client's favor.

## Session wall-clock

Union of every transcript event across the selected projects, with gaps over 20 minutes
treated as breaks. This includes sub-agents grinding away while nobody was watching, so
on heavy days it can be 5–10x the attended figure. Useful as context and as the
denominator for leverage. **Never** bill from it.

## Messages typed

Only what the person typed. Excluded: `<task-notification>` injections from background
agents, slash-command echoes, hook output, system reminders, and shell stdout. `<bash-input>`
counts — that is them typing a command. `[Request interrupted by user]` counts — that is
them hitting escape, a real act of steering.

On an agent-heavy day the excluded messages can outnumber the real ones several times
over, which is why raw message counts from any other source will disagree with this one.

## Known limits — state these, don't hide them

- **Sessions only.** Xcode, Android Studio, a device, the Play Console, PR review on the
  board — none of it is here. Every figure is a floor. The days where attended time
  approaches wall-clock are precisely the days with the most invisible work.
- **Long unattended runs are ambiguous.** Someone may have been watching a two-hour agent
  run attentively, or may have been at lunch. The model assumes lunch. On days that ran
  mostly autonomously, the high estimate is the fairer one.
- **Commits are attributed by author date**, all authors, including merge commits and work
  landed by agents. That is a measure of output, not of hours.
- **Resumed and forked sessions** duplicate lines across transcript files. Deduplication is
  by timestamp, which is right in practice but would merge two genuinely distinct events
  landing in the same second.
- **Timezone** is the machine's current local zone, applied across the whole range. A range
  spanning a DST change gets that boundary slightly wrong.

## Changing the parameters

`--cap-low`, `--cap-high` and `--break-after` are exposed for a reason: a person who thinks
in long focused blocks is served by a higher cap than someone who fires off one-liners.
If you change a default from a previous report for the same person, say so in the report —
an unexplained shift in method reads as an unexplained shift in hours.
