# Claude Code Costs

How much the Claude Team seat saves compared with paying Anthropic's pay-per-use API prices for the
same Claude Code usage.

The script reads the transcripts Claude Code writes to `~/.claude/projects`, removes the duplicate
lines that streaming produces, prices every API call at Anthropic's list prices, and compares the
monthly total with the seat price. Results are stored in `moneysaved.sqlite` next to the script, so
history is kept even after Claude Code prunes old transcripts.

Requires Python 3.9 or newer. No third-party packages.

## Install on another PC

The tool counts the Claude Code usage of the PC it runs on, so each PC gets its own copy.

1. **Install Python** on the other PC if it is not there already. Download it from
   https://www.python.org/downloads/ and run the installer with the default options. The defaults
   include tkinter (used for the window) and the Python launcher (makes `.pyw` files open on
   double-click without a console window). Any Python 3.9 or newer works.
2. **Copy this folder** to a normal local folder on the other PC, for example
   `C:\Tools\Claude Code Costs`. Do not point both PCs at the same OneDrive folder: they would
   share one database file and interfere with each other.
3. **Delete `moneysaved.sqlite`** in the copied folder if it came along. It holds this PC's history.
   The other PC builds its own history from its own transcripts on first start.
4. **Double-click `Claude Code Costs.pyw`.** The window opens, scans the transcripts in
   `C:\Users\<you>\.claude\projects` and starts ticking. The first scan may take a few seconds if
   there is a lot of history.

`config.json` can be copied as it is; it carries the seat type, currency and prices. If Claude Code
has never been used on that PC, the window shows zero until it is.

Optional: run `python moneysaved.py install-hook` once on the new PC so every Claude Code session
updates the store when it ends, even when the window is not open.

## What is counted

Claude Code usage on the PC where the tool runs, whether started from a terminal or from the Claude
desktop app. Claude chat and Cowork are not included: their token counts exist only on Anthropic's
servers and are not exposed on a seat-based Team plan. The live window's caption says "on this PC".

## Live window

Double-click `Claude Code Costs.pyw` (or run `python moneysaved.py live`). A small window opens and stays
up. Every second it reads what Claude Code appended to the transcripts and glides the numbers
to the new totals, so the counter ticks while a session is running. It shows:

- the API value of this month's usage, with a progress bar toward the seat price and, once past
  it, the amount saved this month;
- a statistics section, collapsed by default, with three tables: by period (last hour, today, this
  week from Monday, this month), by model this month, and by project this month (largest first,
  small ones folded into "Other"), each with API value, calls and tokens; the arrow next to
  "Statistics" expands it. The "Pin on top" checkbox and the time of the last refresh sit on the
  same line.

The Standard / Premium switch at the top changes the seat being compared against, and the DKK / EUR
switch changes the display currency (DKK by default). Both save to `config.json`, so the CLI uses the same choices,
and the numbers glide to the new values straight away. Billing is monthly. "Pin on top" keeps the
window above other windows. Escape closes it.

## Commands

```
python moneysaved.py                    scan transcripts, then print the report
python moneysaved.py --projects         same, plus a per-project breakdown
python moneysaved.py seat premium       select the seat type: standard | premium
python moneysaved.py billing annual     select the billing cycle: annual | monthly
python moneysaved.py start 2026-01      first month the seat was paid for (earlier months are ignored)
python moneysaved.py currency DKK       display currency: EUR | DKK
python moneysaved.py report --months 3  report only, last three months
python moneysaved.py status             one line for the current month
python moneysaved.py live               open the live window
python moneysaved.py scan               update the store without printing a report
python moneysaved.py rescan             re-read every transcript from the start (after a pricing or parsing fix)
python moneysaved.py config             show the active configuration
python moneysaved.py install-hook       add a Claude Code SessionEnd hook that runs the scan automatically
```

## Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `seat` | `premium` | `standard` or `premium` |
| `billing` | `monthly` | `annual` or `monthly`; annual is cheaper per month |
| `currency` | `DKK` | Display currency, `DKK` or `EUR` |
| `usd_per_eur` | `1.16` | USD per EUR, used to convert API prices |
| `per_eur` | `EUR 1.0, DKK 7.46` | Display-currency units per EUR; DKK is pegged to the euro |
| `seat_prices` | see below | EUR per seat per month, excluding VAT |
| `subscription_start` | `null` | `YYYY-MM`; months before this are ignored |
| `transcripts_dir` | `null` | Override for the transcript folder |

Default seat prices are in EUR excluding VAT: standard 18.00 annual / 21.04 monthly, premium 90.00
annual / 105.23 monthly.
Anthropic's public pricing page only shows USD (standard 20 / 25, premium 100 / 125) and says
currency varies by region, so check your own invoice and edit `config.json` if your figures differ.

## How the API-equivalent cost is computed

Per API call: input tokens, output tokens, cache reads, and cache writes (5-minute and 1-hour TTL
billed at their own rates), plus web searches. Prices are USD per million tokens and live in the
`PRICES` table at the top of `moneysaved.py`. Models are matched by longest prefix, so a new dated
model id still gets its family's price. The flip side is that a point release such as Opus 5.5 also
gets its family's price (Opus 5) until it has its own row, so add one when a new version appears.
Unknown models are counted as zero and reported. Calls that ran in fast mode (Claude Code's `/fast`,
Fable 5.2, Mythos 5.2, Opus 5.5, Opus 5 and Opus 4.8 only) are priced at the fast-mode rates, with
the cache multipliers on top. Haiku 5.5 is priced by prompt length: a call whose prompt (input, cache reads and cache writes)
is over 100,000 tokens pays $0.50 / $2.50 per million instead of $0.10 / $0.50, on all its tokens.

Cache reads dominate Claude Code usage, so the cache rates matter more than the headline
input/output rates. Claude Code uses the 1-hour cache TTL.

Streaming writes several transcript lines per API call, all with the same message and request id.
The first lines carry the output count from the start of the stream, the last one the final count,
so the store keeps the largest count seen for each call. After a change to the pricing or parsing,
`python moneysaved.py rescan` re-reads every transcript still on disk and repairs the stored rows;
calls whose transcripts Claude Code has already deleted are kept as they are.

### Requests that are not in the transcripts

Claude Code also makes requests that never become a message in the transcript: roughly one
full-context read per prompt (most likely to keep the prompt cache warm), the Haiku calls behind web
searches and session titles, and retried or refused attempts. Its own running count, which does
include those, is written to the transcript as a `cost-state` record: a dollar figure plus token
counts per model.

The count is not one total per session. It carries on when a session is resumed, but starts again
from zero after a `/login`, and the record is written when the session exits or is resumed, so it
can lag the session's last calls. The tool therefore stores each run of the counter with the time
its record was written, compares it with the transcript calls made between the run's start and that
time, and adds what it finds above them to the month of the last of those calls. Calls after a run's
last record are left out of the comparison rather than counted against it. Earlier versions compared
the session's largest record with all of its calls, which comes out too low when a session has had
more than one run. That comparison is still made,
and the larger of the two is used, so sessions whose transcripts were deleted before runs were
stored keep their figure. After updating from such a version, run `python moneysaved.py rescan`.

Claude Code's price table can lag the list prices: in October 2026 it still charged Sonnet 5.5
cache reads at $0.20 per million instead of $0.10. So its figure is checked model by model. The
record gives cache writes as one total, which puts the list-price cost between "all at the 5-minute
rate" and "all at the 1-hour rate". A figure inside that range is used as it is, since it reflects
the real split; one outside it is repriced from the token counts at list prices, splitting the
writes as that model's transcript calls were split. For Haiku 5.5 the record does not say which
requests had long prompts, so the range runs from all at the lower rates to all at the higher ones,
and a figure outside it is repriced at the lower rates. The report lists the amount per month, and the live window shows it as
"Not in transcripts" in the by-model table. It is known per run, not per call, so the hour, day
and week rows cannot include it; the month figures do.

## Why the window shows no all-time figure

Claude Code deletes transcripts that have not been modified for 30 days, so a freshly installed copy
of this tool can only see the last 30 days. The window therefore limits itself to today, this week
and this month, which a fresh install always sees in full. The command-line report still shows every
month found in the store, and leaves a month out of the seat comparison when its data begins
part-way through it (see Caveats).

## Caveats

- The API-equivalent figure is what the identical calls would cost at list price. On pay-per-use
  you would probably use cheaper models and shorter sessions, so treat it as an upper bound.
- Transcripts that Claude Code has already deleted cannot be recovered. Run the scan regularly or
  install the hook.
- Months with no usage still count a full seat fee. Set `subscription_start` so months before the
  seat existed are not counted.
- The month in which the stored history begins is shown but not compared with the seat when its
  data starts after the 1st, which is the normal case for a fresh install (Claude Code keeps 30 days
  of transcripts). The report marks it "partial". If the seat was paid for that month, set
  `subscription_start` to it and it is counted in full.

## License

MIT. See [LICENSE](LICENSE).
