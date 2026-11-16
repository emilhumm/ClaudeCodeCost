#!/usr/bin/env python3
"""Claude Code Costs - what your Claude subscription saves compared with paying Anthropic API list prices.

Reads the Claude Code transcripts in ~/.claude/projects, de-duplicates the streamed
assistant records, prices every API call at Anthropic's pay-per-use list prices, and
compares the monthly total with the price of your plan (Pro, Max or Ultra). Everything
is kept in a local SQLite file next to this script, so history survives Claude Code
pruning old transcripts.

Usage
  python moneysaved.py                    scan transcripts, then print the report
  python moneysaved.py plan max           select the plan: pro | max | ultra
  python moneysaved.py billing annual     select the billing cycle: monthly | annual (Pro only)
  python moneysaved.py start 2026-01      first month the plan was paid for (earlier months are ignored)
  python moneysaved.py currency EUR       display currency: USD | EUR | DKK
  python moneysaved.py report [--months N] [--projects]
  python moneysaved.py status             one line for the current month (statusline friendly)
  python moneysaved.py live               open the live window (or double-click "Claude Code Costs.pyw")
  python moneysaved.py scan [--quiet]     only update the local store
  python moneysaved.py rescan [--quiet]   re-read every transcript from the start (after a fix)
  python moneysaved.py config             print the active configuration
  python moneysaved.py install-hook       run "scan" automatically when a Claude Code session ends
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
DB_PATH = HERE / "moneysaved.sqlite"

# Subscription plans: display name, then the price per month excluding tax for each billing cycle
# and currency. These are the US list prices on claude.com/pricing (checked 2026-11-16); Pro's annual
# plan is $200 up front. Max and Ultra (formerly "Max 20x") are billed monthly only. Anthropic
# publishes no EUR prices, so other currencies are converted from USD; config.json's
# "price_overrides" can hold what you actually pay.
PLANS = {
    "pro": ("Pro", {"monthly": {"USD": 20.0}, "annual": {"USD": 200.0 / 12}}),
    "max": ("Max", {"monthly": {"USD": 100.0}}),
    "ultra": ("Ultra", {"monthly": {"USD": 200.0}}),
}

DEFAULT_CONFIG = {
    "plan": "max",
    "billing": "monthly",
    "currency": "USD",
    "usd_per_eur": 1.16,
    "per_eur": {"EUR": 1.0, "DKK": 7.46},
    "price_overrides": {},
    "subscription_start": None,
    "transcripts_dir": None,
    "_notes": {
        "plan": "pro | max | ultra",
        "billing": "monthly | annual. Annual applies to Pro; Max and Ultra are billed monthly only.",
        "currency": "Display currency: USD, or any key of per_eur (EUR, DKK).",
        "usd_per_eur": "How many USD one EUR buys. API prices are USD; used to convert.",
        "per_eur": "Display-currency units per EUR. DKK is pegged to the euro at about 7.46.",
        "price_overrides": 'Optional: what you pay per month ex VAT where it differs from the list price, '
        'e.g. {"pro": {"monthly": {"EUR": 18.0}}}. The list prices are in PLANS in moneysaved.py.',
        "subscription_start": "Optional YYYY-MM. Months before this are ignored for both usage and plan cost.",
        "transcripts_dir": "Optional override. Default is ~/.claude/projects.",
    },
}

# Anthropic API list prices, USD per million tokens:
# (input, output, cache read, cache write 5-minute TTL, cache write 1-hour TTL)
# Matched by longest prefix, so "claude-opus-4-8" beats "claude-opus-4". A point release such as
# claude-opus-5-5 silently takes its family's price ("claude-opus-5") unless it has its own row.
# Checked against platform.claude.com/docs/en/about-claude/pricing on 2026-11-12.
PRICES = [
    ("claude-fable-5-2", (8.0, 40.0, 0.20, 10.0, 16.0)),
    ("claude-mythos-5-2", (8.0, 40.0, 0.20, 10.0, 16.0)),
    ("claude-fable-5-1", (10.0, 50.0, 0.25, 12.50, 20.0)),
    ("claude-mythos-5-1", (10.0, 50.0, 0.25, 12.50, 20.0)),
    ("claude-fable-5", (10.0, 50.0, 1.00, 12.50, 20.0)),
    ("claude-mythos-5", (10.0, 50.0, 1.00, 12.50, 20.0)),
    ("claude-opus-5-5", (4.0, 20.0, 0.20, 5.00, 8.0)),
    ("claude-opus-5", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-8", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-7", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-6", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-5", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-1", (15.0, 75.0, 1.50, 18.75, 30.0)),
    ("claude-opus-4", (15.0, 75.0, 1.50, 18.75, 30.0)),
    ("claude-sonnet-5-5", (2.0, 10.0, 0.10, 2.50, 4.0)),
    ("claude-sonnet-5", (2.0, 10.0, 0.20, 2.50, 4.0)),
    ("claude-sonnet-4", (3.0, 15.0, 0.30, 3.75, 6.0)),
    ("claude-haiku-5-5", (0.10, 0.50, 0.01, 0.125, 0.20)),  # prompts up to 100K tokens; see LONG_PRICES
    ("claude-haiku-4", (1.0, 5.0, 0.10, 1.25, 2.0)),
    # Generation 3 ids put the version before the family name: claude-3-7-sonnet-20250219.
    ("claude-3-7-sonnet", (3.0, 15.0, 0.30, 3.75, 6.0)),
    ("claude-3-5-sonnet", (3.0, 15.0, 0.30, 3.75, 6.0)),
    ("claude-3-5-haiku", (0.8, 4.0, 0.08, 1.00, 1.6)),
    ("claude-3-haiku", (0.25, 1.25, 0.03, 0.30, 0.5)),
]
# Fast mode (Claude Code's /fast) runs Fable 5.2, Mythos 5.2, Opus 5.5, Opus 5 and Opus 4.8 at
# premium rates; the cache multipliers stack on top. Other models ignore the setting and bill at the
# standard rates above.
FAST_PRICES = [
    ("claude-fable-5-2", (16.0, 80.0, 0.40, 20.0, 32.0)),
    ("claude-mythos-5-2", (16.0, 80.0, 0.40, 20.0, 32.0)),
    ("claude-opus-5-5", (8.0, 40.0, 0.40, 10.0, 16.0)),
    ("claude-opus-5", (10.0, 50.0, 1.00, 12.50, 20.0)),
    ("claude-opus-4-8", (10.0, 50.0, 1.00, 12.50, 20.0)),
]
# Haiku 5.5 is priced by prompt length: a call whose prompt (input, cache reads and cache writes)
# is over 100,000 tokens pays these rates for all of its tokens. Other models have one price.
LONG_PROMPT_TOKENS = 100_000
LONG_PRICES = [
    ("claude-haiku-5-5", (0.50, 2.50, 0.05, 0.625, 1.0)),
]
WEB_SEARCH_USD = 10.0 / 1000  # $10 per 1,000 searches

PREFIXES = {"EUR": "€", "USD": "$", "GBP": "£"}
SUFFIXES = {"DKK": " kr", "SEK": " kr", "NOK": " kr", "CHF": " CHF"}


# --------------------------------------------------------------------------- config

def load_config() -> dict:
    created = False
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        cfg, created = {}, True
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    legacy = {}
    for k, v in cfg.items():
        if k in ("seat", "seat_prices", "_notes"):  # Team seat settings of earlier versions are dropped;
            continue  # the notes are always the current ones
        if k == "usd_per_unit":  # older name for usd_per_eur (the unit was EUR)
            legacy[k] = v
        elif k in ("per_eur", "price_overrides") and isinstance(v, dict):
            merged[k].update(v)
        else:
            merged[k] = v
    if "usd_per_unit" in legacy and "usd_per_eur" not in cfg:
        merged["usd_per_eur"] = legacy["usd_per_unit"]
    if created or merged != cfg:
        save_config(merged)
    if created:
        print(f"Created {CONFIG_PATH.name} with defaults: {describe_plan(merged)}.")
        print("Change with:  python moneysaved.py plan pro   /   python moneysaved.py currency EUR\n")
    return merged


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def per_eur(cfg: dict, currency=None) -> float:
    """Units of `currency` (default: the display currency) per EUR."""
    cur = (currency or cfg["currency"]).upper()
    if cur == "USD":
        return float(cfg["usd_per_eur"])
    return float(cfg["per_eur"].get(cur, 1.0))


def _plan(cfg: dict):
    plan = PLANS.get(cfg.get("plan"))
    if plan is None:
        raise ValueError(f"unknown plan {cfg.get('plan')!r} in config.json; use one of: {', '.join(PLANS)}")
    return plan


def plan_name(cfg: dict) -> str:
    return _plan(cfg)[0]


def plan_billing(cfg: dict) -> str:
    """The billing cycle in effect: the configured one, or monthly for a plan without that option."""
    return cfg["billing"] if cfg["billing"] in _plan(cfg)[1] else "monthly"


def plan_price(cfg: dict) -> float:
    """The plan's price per month, excluding VAT, in the display currency. A price in
    config.json's price_overrides is what you actually pay, so it replaces the list prices in every
    currency. Uses a price in the display currency where there is one, otherwise converts the EUR
    price, otherwise the USD price."""
    billing = plan_billing(cfg)
    over = ((cfg.get("price_overrides") or {}).get(cfg["plan"]) or {}).get(billing) or {}
    known = {c.upper(): float(v) for c, v in over.items()} or dict(_plan(cfg)[1][billing])
    cur = cfg["currency"].upper()
    if cur in known:
        return float(known[cur])
    base = "EUR" if "EUR" in known else "USD"
    return known[base] / per_eur(cfg, base) * per_eur(cfg)


def describe_plan(cfg: dict) -> str:
    """'Max, monthly billing, $100.00/month ex VAT'."""
    return f"{plan_name(cfg)}, {plan_billing(cfg)} billing, {money(plan_price(cfg), cfg)}/month ex VAT"


def usd_to_local(usd: float, cfg: dict) -> float:
    return usd / float(cfg["usd_per_eur"]) * per_eur(cfg)


def money(v: float, cfg: dict, signed: bool = False) -> str:
    cur = cfg["currency"].upper()
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    if cur in SUFFIXES:
        return f"{sign}{abs(v):,.2f}{SUFFIXES[cur]}"
    return f"{sign}{PREFIXES.get(cur, cur + ' ')}{abs(v):,.2f}"


# --------------------------------------------------------------------------- pricing

def price_for(model: str, speed: str = "standard", prompt: float = 0):
    """Price tuple for a model id, matched by longest prefix; fast-mode rates when the call ran fast,
    long-prompt rates when its prompt was over LONG_PROMPT_TOKENS and the model has them."""
    if speed == "fast":
        fast = _longest_prefix(model, FAST_PRICES)
        if fast is not None:
            return fast
    if prompt > LONG_PROMPT_TOKENS:
        long = _longest_prefix(model, LONG_PRICES)
        if long is not None:
            return long
    return _longest_prefix(model, PRICES)


def _longest_prefix(model: str, table):
    best = None
    for prefix, p in table:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, p)
    return best[1] if best else None


def usd_cost(model, inp, out, cread, w5, w1, web, speed="standard", prompt=None):
    """USD for one call. `prompt` overrides the prompt length that picks the price tier; it defaults
    to the call's own input, cache read and cache write tokens."""
    if prompt is None:
        prompt = inp + cread + w5 + w1
    p = price_for(model, speed, prompt)
    if p is None:
        return None
    return (inp * p[0] + out * p[1] + cread * p[2] + w5 * p[3] + w1 * p[4]) / 1e6 + web * WEB_SEARCH_USD


# --------------------------------------------------------------------------- store

def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            msg_id      TEXT NOT NULL,
            request_id  TEXT NOT NULL,
            ts          TEXT,
            month       TEXT,
            session_id  TEXT,
            project     TEXT,
            model       TEXT,
            input       INTEGER,
            output      INTEGER,
            cache_read  INTEGER,
            cache_w5    INTEGER,
            cache_w1    INTEGER,
            web_search  INTEGER,
            speed       TEXT NOT NULL DEFAULT 'standard',
            PRIMARY KEY (msg_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_month ON messages(month);
        CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
        CREATE TABLE IF NOT EXISTS files (
            path    TEXT PRIMARY KEY,
            size    INTEGER,
            mtime   REAL,
            offset  INTEGER NOT NULL DEFAULT 0,
            last_ts TEXT                        -- latest timestamp read so far
        );
        CREATE TABLE IF NOT EXISTS sessions (   -- Claude Code's own running cost per session
            session_id TEXT PRIMARY KEY,
            cc_usd     REAL NOT NULL,           -- its totalCostUSD, largest value seen
            cc_models  TEXT,                    -- its per-model token counts, JSON
            start_ts   TEXT                     -- session start, ISO UTC
        );
        CREATE TABLE IF NOT EXISTS cost_runs (  -- the same per run of its counter (see ingest_cost_state)
            session_id TEXT NOT NULL,
            start_ts   TEXT NOT NULL,           -- when the counter started, ISO UTC
            end_ts     TEXT,                    -- the last transcript line before the record, ISO UTC
            cc_usd     REAL NOT NULL,           -- largest value seen in this run
            cc_models  TEXT,
            PRIMARY KEY (session_id, start_ts)
        );
        """
    )
    cols = {row[1] for row in db.execute("PRAGMA table_info(files)")}
    if "offset" not in cols:  # store created by an earlier version: everything on disk was fully read
        db.execute("ALTER TABLE files ADD COLUMN offset INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE files SET offset = size")
        db.commit()
    if "last_ts" not in cols:  # earlier versions did not record it; `rescan` fills it in
        db.execute("ALTER TABLE files ADD COLUMN last_ts TEXT")
        db.commit()
    if "speed" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
        # Earlier versions did not record the speed; `rescan` fills it in from the transcripts.
        db.execute("ALTER TABLE messages ADD COLUMN speed TEXT NOT NULL DEFAULT 'standard'")
        db.commit()
    return db


def local_month(ts) -> str:
    if not ts:
        return "unknown"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m")
    except ValueError:
        return ts[:7]


def ingest(db: sqlite3.Connection, rec: dict, fallback_project: str, last_ts=None) -> int:
    """Store one transcript record if it is a priced API call.

    Streaming writes one line per content block, all with the same message and request id. The
    first lines carry the output count from the start of the stream (a few tokens); the last one
    carries the final count. The largest count seen is kept, so a call is priced by its final usage.
    `last_ts` is the latest timestamp on the lines before this one in its file.
    Returns 1 when the record was new or raised a stored count, else 0."""
    if rec.get("type") == "cost-state":
        return ingest_cost_state(db, rec, last_ts)
    if rec.get("type") != "assistant":
        return 0
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {}
    model = msg.get("model") or ""
    if not usage or not model or model.startswith("<"):
        return 0
    cc = usage.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens")
    w1 = cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        # Older records only carry the total. Claude Code uses the 1-hour TTL,
        # and its own cost estimate prices the total at the 1-hour rate.
        w5, w1 = 0, usage.get("cache_creation_input_tokens", 0) or 0
    cwd = rec.get("cwd") or ""
    # A drive root such as Z:\ has no last component; show the drive rather than an empty name.
    project = (Path(cwd).name or Path(cwd).anchor.rstrip("\\/") or fallback_project) if cwd else fallback_project
    cur = db.execute(
        """INSERT INTO messages (msg_id, request_id, ts, month, session_id, project, model,
                                 input, output, cache_read, cache_w5, cache_w1, web_search, speed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(msg_id, request_id) DO UPDATE SET
               input = max(input, excluded.input),
               output = max(output, excluded.output),
               cache_read = max(cache_read, excluded.cache_read),
               cache_w5 = max(cache_w5, excluded.cache_w5),
               cache_w1 = max(cache_w1, excluded.cache_w1),
               web_search = max(web_search, excluded.web_search),
               project = CASE WHEN project = '' THEN excluded.project ELSE project END,
               speed = excluded.speed
           WHERE excluded.output > output OR excluded.input > input OR excluded.cache_read > cache_read
              OR excluded.cache_w5 > cache_w5 OR excluded.cache_w1 > cache_w1
              OR excluded.web_search > web_search OR (project = '' AND excluded.project != '')
              OR excluded.speed != speed""",
        (
            msg.get("id") or rec.get("uuid") or "",
            rec.get("requestId") or "",
            rec.get("timestamp"),
            local_month(rec.get("timestamp")),
            rec.get("sessionId") or rec.get("session_id") or "",
            project,
            model,
            usage.get("input_tokens", 0) or 0,
            usage.get("output_tokens", 0) or 0,
            usage.get("cache_read_input_tokens", 0) or 0,
            w5 or 0,
            w1 or 0,
            (usage.get("server_tool_use") or {}).get("web_search_requests", 0) or 0,
            usage.get("speed") or "standard",
        ),
    )
    return cur.rowcount


def ingest_cost_state(db: sqlite3.Connection, rec: dict, last_ts=None) -> int:
    """Store Claude Code's own running cost for a session (a `cost-state` transcript record).

    It counts every request the session made, including those that never became a transcript
    message: roughly one full-context read per prompt, the Haiku calls behind web searches and
    session titles, and retried or refused attempts. Its per-model token counts are kept too, so
    `aggregate` can check the figure against the rates in PRICES (see `claude_code_usd`) and, where
    it exceeds the transcript pricing, add the difference (see README, "Requests that are not in
    the transcripts").

    The figure is a running total, but not over the whole session: it carries on across a resume
    and starts again from zero on a `/login`, each time with a new `startTime`. Claude Code writes
    the record when the session exits and again when it is resumed, so it can lag the session's
    last calls. Two copies are kept, both with the largest value seen: one per session (`sessions`,
    the only one earlier versions stored) and one per run of the counter (`cost_runs`), together
    with `last_ts`, the time of the transcript line before the record, which ends the stretch of
    calls the figure covers. Not counted as an API call: always returns 0."""
    sid = rec.get("sessionId") or ""
    usd = rec.get("totalCostUSD")
    if not sid or not isinstance(usd, (int, float)):
        return 0
    start = rec.get("startTime")  # ms since the epoch
    start_ts = run_ts = None
    if isinstance(start, (int, float)):
        started = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
        start_ts = started.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        run_ts = started.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(start) % 1000:03d}Z"
    models = json.dumps(rec.get("modelUsage") or {}, separators=(",", ":"))
    db.execute(
        """INSERT INTO sessions (session_id, cc_usd, cc_models, start_ts) VALUES (?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET
               cc_usd = excluded.cc_usd,
               cc_models = excluded.cc_models,
               start_ts = COALESCE(start_ts, excluded.start_ts)
           WHERE excluded.cc_usd > cc_usd""",
        (sid, float(usd), models, start_ts),
    )
    if run_ts:
        db.execute(
            """INSERT INTO cost_runs (session_id, start_ts, end_ts, cc_usd, cc_models) VALUES (?,?,?,?,?)
               ON CONFLICT(session_id, start_ts) DO UPDATE SET
                   end_ts = excluded.end_ts,
                   cc_usd = excluded.cc_usd,
                   cc_models = excluded.cc_models
               WHERE excluded.cc_usd > cc_usd OR (end_ts IS NULL AND excluded.end_ts IS NOT NULL)""",
            (sid, run_ts, last_ts, float(usd), models),
        )
    return 0


def scan(db: sqlite3.Connection, cfg: dict, quiet: bool = False) -> int:
    """Read whatever was appended to the transcripts since the last scan. Cheap enough to run every few seconds."""
    root = Path(cfg.get("transcripts_dir") or Path.home() / ".claude" / "projects").expanduser()
    if not root.exists():
        if not quiet:
            print(f"Transcript folder not found: {root}", file=sys.stderr)
        return 0
    known = {p: (s, m, o, t) for p, s, m, o, t in db.execute("SELECT path, size, mtime, offset, last_ts FROM files")}
    new_rows = files_read = 0
    for path in sorted(root.rglob("*.jsonl")):
        try:
            st = path.stat()
        except OSError:
            continue
        size, mtime, offset, last_ts = known.get(str(path), (None, None, 0, None))
        if size == st.st_size and mtime == st.st_mtime:
            continue
        if size is None or st.st_size < offset:
            offset = 0  # new file, or rewritten shorter than what we had read
        if offset == 0:
            last_ts = None
        files_read += 1
        fallback_project = path.relative_to(root).parts[0]
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            continue
        end = data.rfind(b"\n")  # a line still being written has no newline yet; leave it for next time
        if end >= 0:
            for raw in data[: end + 1].split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                new_rows += ingest(db, rec, fallback_project, last_ts)
                ts = rec.get("timestamp")
                if isinstance(ts, str) and (last_ts is None or ts > last_ts):
                    last_ts = ts
            offset += end + 1
        db.execute(
            "INSERT OR REPLACE INTO files (path, size, mtime, offset, last_ts) VALUES (?,?,?,?,?)",
            (str(path), st.st_size, st.st_mtime, offset, last_ts),
        )
        db.commit()
    if not quiet:
        total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        print(f"Scanned {files_read} changed transcript file(s), {new_rows} API call(s) added or updated; {total} in store.\n")
    return new_rows


def rescan(db: sqlite3.Connection, cfg: dict, quiet: bool = False) -> int:
    """Re-read every transcript from the start. Stored rows are kept and their counts only ever
    raised, so this repairs history after a parsing fix without losing calls whose transcripts
    Claude Code has since deleted."""
    db.execute("UPDATE files SET offset = 0, size = -1")
    db.commit()
    return scan(db, cfg, quiet=quiet)


# --------------------------------------------------------------------------- report

def month_range(first: str, last: str) -> list:
    y, m = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def coverage_start(db: sqlite3.Connection):
    """When the stored history begins: the earliest call as an aware local datetime, or None."""
    ts = db.execute("SELECT MIN(ts) FROM messages").fetchone()[0]
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def partial_month(db: sqlite3.Connection, cfg: dict):
    """The month the stored history begins in, when it begins after the 1st and no subscription_start
    vouches for the plan: ('2026-01', '14 Jan'), else None. Claude Code deletes transcripts after
    30 days, so the first month a fresh install sees is normally incomplete; setting a few days of
    usage against a full month's fee would mislead, so that month is shown without a verdict."""
    if cfg.get("subscription_start"):
        return None
    d = coverage_start(db)
    if d is None or d.day == 1:
        return None
    return d.strftime("%Y-%m"), f"{d.day} {d.strftime('%b')}"


EMPTY_MONTH = {"calls": 0, "tokens": 0, "usd": 0.0, "extra": 0.0}


def aggregate(db: sqlite3.Connection, cfg: dict):
    """Per month, model and project from the stored calls. Each month also carries `extra`: what
    Claude Code's own cost records count beyond the transcript calls (see `ingest_cost_state`).

    It is worked out two ways. Per run of Claude Code's counter: its largest figure against the calls
    between the run's start and that record, booked to the month of the last of those calls. Per
    session: the session's largest figure against all of its calls, booked to the month the session
    was last active in. Both can only fall short (calls a figure does not cover still count against
    it in the second, and calls after a run's last record are left out of the first), so the larger
    is used. Sessions read before runs were stored have only the second."""
    rows = db.execute(
        "SELECT month, project, model, session_id, input, output, cache_read, cache_w5, cache_w1, web_search, speed, ts "
        "FROM messages WHERE month != 'unknown'"
    ).fetchall()
    start = cfg.get("subscription_start")
    if start:
        rows = [r for r in rows if r[0] >= start]
    runs = defaultdict(list)  # session: (start_ts, end_ts) of each run of Claude Code's counter
    for sid, start_ts, end_ts in db.execute("SELECT session_id, start_ts, end_ts FROM cost_runs WHERE end_ts IS NOT NULL"):
        runs[sid].append((start_ts, end_ts))
    per_month = defaultdict(lambda: dict(EMPTY_MONTH))
    per_model = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": 0.0})
    per_project = defaultdict(lambda: {"calls": 0, "usd": 0.0})
    per_session = defaultdict(lambda: {"usd": 0.0, "month": ""})  # keyed by session, and by (session, run start)
    mix = defaultdict(lambda: [0, 0, 0.0, 0.0])  # (key, model): 5m writes, 1h writes, cost, cost at standard speed
    model_writes = defaultdict(lambda: [0, 0])  # model: 5m writes, 1h writes
    unknown = set()
    for month, project, model, session, inp, out, cread, w5, w1, web, speed, ts in rows:
        cost = usd_cost(model, inp, out, cread, w5, w1, web, speed)
        if cost is None:
            unknown.add(model)
            cost = 0.0
        std = cost if speed == "standard" else (usd_cost(model, inp, out, cread, w5, w1, web) or 0.0)
        for key in [session] + [(session, a) for a, b in runs.get(session, ()) if a <= ts <= b]:
            sm = mix[(key, model)]
            sm[0] += w5
            sm[1] += w1
            sm[2] += cost
            sm[3] += std
            ps = per_session[key]
            ps["usd"] += cost
            ps["month"] = max(ps["month"], month)
        mw = model_writes[model]
        mw[0] += w5
        mw[1] += w1
        pm = per_month[month]
        pm["calls"] += 1
        pm["tokens"] += inp + out + cread + w5 + w1
        pm["usd"] += cost
        md = per_model[model]
        md["calls"] += 1
        md["input"] += inp
        md["output"] += out
        md["cache_read"] += cread
        md["cache_write"] += w5 + w1
        md["usd"] += cost
        pp = per_project[project]
        pp["calls"] += 1
        pp["usd"] += cost
    whole = {}  # session: [(month, extra)] from its largest figure against all of its calls
    for sid, cc_usd, cc_models, start_ts in db.execute("SELECT session_id, cc_usd, cc_models, start_ts FROM sessions"):
        ours = per_session.get(sid)
        month = ours["month"] if ours else local_month(start_ts)
        whole[sid] = [(month, claude_code_usd(sid, cc_usd, cc_models, mix, model_writes) - (ours["usd"] if ours else 0.0))]
    by_run = defaultdict(list)  # session: [(month, extra)], one per run of Claude Code's counter
    for sid, start_ts, end_ts, cc_usd, cc_models in db.execute(
        "SELECT session_id, start_ts, end_ts, cc_usd, cc_models FROM cost_runs WHERE end_ts IS NOT NULL"
    ):
        key = (sid, start_ts)
        ours = per_session.get(key)
        month = ours["month"] if ours else local_month(end_ts)
        by_run[sid].append((month, claude_code_usd(key, cc_usd, cc_models, mix, model_writes) - (ours["usd"] if ours else 0.0)))
    for sid in whole.keys() | by_run.keys():
        found = max(whole.get(sid, []), by_run.get(sid, []), key=lambda xs: sum(max(x, 0.0) for _, x in xs))
        for month, extra in found:
            if extra > 0 and month != "unknown" and not (start and month < start):
                per_month[month]["extra"] += extra
    return per_month, per_model, per_project, unknown


def claude_code_usd(key, cc_usd, cc_models, mix, model_writes) -> float:
    """Claude Code's own cost for a session or one run of its counter (`key` as in `aggregate`),
    checked model by model against the rates in PRICES.

    Its price table can lag the list prices: in October 2026 it still had Sonnet 5.5 cache reads at
    $0.20 instead of $0.10. Its token counts give cache writes as one total, so list prices bound
    each model's cost between all writes at the 5-minute rate and all at the 1-hour rate. A model
    priced by prompt length (Haiku 5.5) is bounded by all requests at its lower rates and all at its
    higher ones, since the record does not say which were long. A figure inside the bounds is kept,
    since it reflects the real split. One outside them was priced differently and is repriced from
    the token counts at the lower rates, splitting the writes in the proportion of that model's calls
    under `key` in the transcripts, else of all its calls, else all at the 1-hour rate. Fast-mode
    calls in the transcripts carry their premium over to the bounds. A model with no price, or a
    record without per-model counts, keeps Claude Code's own figure."""
    usage = json.loads(cc_models or "{}")
    if not usage:
        return cc_usd
    total = 0.0
    for model, u in usage.items():
        claimed = float(u.get("costUSD", 0) or 0)
        tokens = [u.get(k, 0) or 0 for k in ("inputTokens", "outputTokens", "cacheReadInputTokens")]
        writes = u.get("cacheCreationInputTokens", 0) or 0
        web = u.get("webSearchRequests", 0) or 0
        lo = usd_cost(model, *tokens, writes, 0, web, prompt=0)
        hi = usd_cost(model, *tokens, 0, writes, web, prompt=0)
        top = usd_cost(model, *tokens, 0, writes, web, prompt=float("inf"))
        if lo is None:
            total += claimed
            continue
        w5, w1, usd, std = mix.get((key, model), (0, 0, 0.0, 0.0))
        if std > 0:
            lo, hi, top = lo * usd / std, hi * usd / std, top * usd / std
        if lo - 0.01 <= claimed <= top + 0.01:
            total += claimed
            continue
        if not w5 + w1:
            w5, w1 = model_writes.get(model, (0, 0))
        share1 = w1 / (w5 + w1) if w5 + w1 else 1.0
        total += lo + (hi - lo) * share1
    return total


def pretty_model(model: str) -> str:
    """'claude-opus-4-8' -> 'Opus 4.8', 'claude-haiku-4-5-20251001' -> 'Haiku 4.5'."""
    parts = [p for p in model.replace("claude-", "").split("-") if not (len(p) == 8 and p.isdigit())]
    if not parts:
        return model
    return f"{parts[0].capitalize()} {'.'.join(parts[1:])}".strip()


def fmt_tokens(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return str(n)


def report(db: sqlite3.Connection, cfg: dict, months=None, projects: bool = False) -> None:
    per_month, per_model, per_project, unknown = aggregate(db, cfg)
    if not per_month:
        print("No usage found yet. Run a Claude Code session, then `python moneysaved.py` again.")
        return
    price = plan_price(cfg)
    now = datetime.now().strftime("%Y-%m")
    first = cfg.get("subscription_start") or min(per_month)
    all_months = month_range(first, max(max(per_month), now))
    if months:
        all_months = all_months[-months:]
    partial = partial_month(db, cfg)
    compared = [m for m in all_months if not (partial and m == partial[0] and m != now)]

    header = f"Plan: {describe_plan(cfg)}   |   API at Anthropic USD list prices"
    if cfg["currency"].upper() != "USD":
        rates = f"1 EUR = {float(cfg['usd_per_eur']):.4f} USD"
        if cfg["currency"].upper() != "EUR":
            rates += f", {per_eur(cfg):.4f} {cfg['currency'].upper()}"
        header += f"   |   {rates}"
    print("Claude Code Costs  -  Claude subscription vs. pay-per-use API")
    print(header)
    print()
    hdr = f"{'Month':<9}{'Calls':>7}{'Tokens':>9}{'API-equiv.':>14}{'Plan':>12}{'Saved':>14}"
    print(hdr)
    print("-" * len(hdr))
    tot_calls = tot_tokens = 0
    tot_api = tot_plan = 0.0
    for m in all_months:
        d = per_month.get(m, EMPTY_MONTH)
        api_local = usd_to_local(d["usd"] + d["extra"], cfg)
        row = f"{m:<9}{d['calls']:>7}{fmt_tokens(d['tokens']):>9}{money(api_local, cfg):>14}"
        if m not in compared:  # the stored history begins part-way through this month: shown, not judged
            print(f"{row}{'-':>12}{'-':>14}  (partial: data from {partial[1]})")
            continue
        saved = api_local - price
        tot_calls += d["calls"]
        tot_tokens += d["tokens"]
        tot_api += api_local
        tot_plan += price
        tag = "  (so far)" if m == now else ""
        print(f"{row}{money(price, cfg):>12}{money(saved, cfg, signed=True):>14}{tag}")
    print("-" * len(hdr))
    tot_saved = tot_api - tot_plan
    print(
        f"{'Total':<9}{tot_calls:>7}{fmt_tokens(tot_tokens):>9}{money(tot_api, cfg):>14}"
        f"{money(tot_plan, cfg):>12}{money(tot_saved, cfg, signed=True):>14}"
    )
    print()
    n = len(compared)
    if tot_plan > 0:
        ratio = tot_api / tot_plan
        verdict = "saved" if tot_saved >= 0 else "cost extra"
        print(
            f"Over {n} month(s) the same usage at API prices would have cost {money(tot_api, cfg)} "
            f"against {money(tot_plan, cfg)} in {plan_name(cfg)} fees: {verdict} {money(abs(tot_saved), cfg)} "
            f"({ratio:.1f}x the plan price)."
        )
    print(f"Break-even: a month pays for the plan once API-equivalent usage passes {money(price, cfg)}.")
    extra_months = [(m, per_month[m]["extra"]) for m in all_months if m in per_month and per_month[m]["extra"] > 0]
    if extra_months:
        listed = ", ".join(f"{m} {money(usd_to_local(x, cfg), cfg)}" for m, x in extra_months)
        print(
            "Includes requests that Claude Code's own session records count but the transcripts do not "
            f"(see README): {listed}."
        )
    if partial:
        print(
            f"{partial[0]} is not compared with the plan: the stored history begins {partial[1]}, so that "
            f"month's usage is incomplete. If the plan was paid for it, run  python moneysaved.py start {partial[0]}"
        )
    print()

    print("By model (all time)")
    hdr2 = f"{'Model':<20}{'Calls':>7}{'Input':>9}{'Output':>9}{'Cache rd':>10}{'Cache wr':>10}{'USD':>10}{cfg['currency']:>13}"
    print(hdr2)
    print("-" * len(hdr2))
    for model, d in sorted(per_model.items(), key=lambda kv: -kv[1]["usd"]):
        print(
            f"{model:<20}{d['calls']:>7}{fmt_tokens(d['input']):>9}{fmt_tokens(d['output']):>9}"
            f"{fmt_tokens(d['cache_read']):>10}{fmt_tokens(d['cache_write']):>10}"
            f"{d['usd']:>10.2f}{money(usd_to_local(d['usd'], cfg), cfg):>13}"
        )
    print()

    if projects:
        print("By project (all time)")
        hdr3 = f"{'Project':<44}{'Calls':>7}{cfg['currency']:>14}"
        print(hdr3)
        print("-" * len(hdr3))
        for project, d in sorted(per_project.items(), key=lambda kv: -kv[1]["usd"]):
            print(f"{project[:43]:<44}{d['calls']:>7}{money(usd_to_local(d['usd'], cfg), cfg):>14}")
        print()

    if unknown:
        print("Warning: no price known for model(s) " + ", ".join(sorted(unknown)) + " - counted as 0. Add them to PRICES in moneysaved.py.")
        print()
    print("Note: API-equivalent is what the identical calls would cost at list price. It is an upper bound on savings.")


def summary(db: sqlite3.Connection, cfg: dict) -> dict:
    """The headline numbers in the display currency, used by `status` and the live window."""
    per_month, _, _, unknown = aggregate(db, cfg)
    price = plan_price(cfg)
    now = datetime.now().strftime("%Y-%m")
    if per_month:
        first = cfg.get("subscription_start") or min(per_month)
        months = month_range(first, max(max(per_month), now)) or [now]
    else:
        months = [now]
    partial = partial_month(db, cfg)
    compared = [m for m in months if not (partial and m == partial[0] and m != now)] or [now]
    tot_api = usd_to_local(sum(sum(per_month.get(m, EMPTY_MONTH)[k] for k in ("usd", "extra")) for m in compared), cfg)
    tot_plan = price * len(compared)
    cur = per_month.get(now, EMPTY_MONTH)

    def since(start) -> dict:
        """API value, calls and tokens for every call at or after `start` (an aware datetime)."""
        cutoff = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        rows = db.execute(
            "SELECT model, input, output, cache_read, cache_w5, cache_w1, web_search, speed FROM messages WHERE ts >= ?",
            (cutoff,),
        ).fetchall()
        usd = sum(usd_cost(*r) or 0.0 for r in rows)
        tokens = sum(r[1] + r[2] + r[3] + r[4] + r[5] for r in rows)
        return {"api": usd_to_local(usd, cfg), "calls": len(rows), "tokens": tokens}

    local_now = datetime.now().astimezone()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    periods = {
        "Last hour": since(local_now - timedelta(hours=1)),
        "Today": since(day_start),
        "This week": since(day_start - timedelta(days=day_start.weekday())),  # weeks start on Monday
        "This month": since(day_start.replace(day=1)),
    }

    # This month's value split by model and by project, largest first, small ones folded into "Other".
    by_model = defaultdict(lambda: [0.0, 0, 0])  # usd, calls, tokens
    by_project = defaultdict(lambda: [0.0, 0, 0])
    for model, project, *counts in db.execute(
        "SELECT model, project, input, output, cache_read, cache_w5, cache_w1, web_search, speed "
        "FROM messages WHERE month = ?",
        (now,),
    ):
        usd = usd_cost(model, *counts) or 0.0
        tokens = sum(counts[:5])
        for group in (by_model[model], by_project[project]):
            group[0] += usd
            group[1] += 1
            group[2] += tokens

    def top(groups: dict, limit: int) -> list:
        """(name, api value, calls, tokens) rows, largest first; beyond `limit` rows the rest becomes 'Other'."""
        items = sorted(((k, usd_to_local(v[0], cfg), v[1], v[2]) for k, v in groups.items()), key=lambda x: -x[1])
        if len(items) > limit:
            rest = items[limit - 1 :]
            other = ("Other", sum(i[1] for i in rest), sum(i[2] for i in rest), sum(i[3] for i in rest))
            items = items[: limit - 1] + [other]
        return items
    last_ts = db.execute("SELECT MAX(ts) FROM messages").fetchone()[0]
    # Requests Claude Code counted that never became transcript messages are known per session or run
    # of its counter, not per call, so only the month figures can include them; the hour, day and week
    # rows stay as recorded.
    month_extra = usd_to_local(cur["extra"], cfg)
    periods["This month"]["api"] += month_extra
    month_api = usd_to_local(cur["usd"], cfg) + month_extra
    return {
        "month": now,
        "plan_price": price,
        "month_api": month_api,
        "month_unrecorded": month_extra,
        "month_saved": month_api - price,
        "month_calls": cur["calls"],
        "month_tokens": cur["tokens"],
        "alltime_api": tot_api,
        "alltime_plan": tot_plan,
        "alltime_saved": tot_api - tot_plan,
        "n_months": len(compared),
        "partial_month": partial,
        "last_hour": periods["Last hour"]["api"],
        "periods": periods,
        "by_model": top(by_model, 6),
        "by_project": top(by_project, 6),
        "last_ts": last_ts,
        "unknown_models": sorted(unknown),
    }


def status(db: sqlite3.Connection, cfg: dict) -> None:
    s = summary(db, cfg)
    print(
        f"{s['month']}: {money(s['month_api'], cfg)} API-equiv vs {money(s['plan_price'], cfg)} {plan_name(cfg)}"
        f" | all time {money(s['alltime_saved'], cfg, signed=True)} over {s['n_months']} month(s)"
    )


# --------------------------------------------------------------------------- hook

def install_hook() -> None:
    settings = Path.home() / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    cmd = f'"{sys.executable}" "{Path(__file__).resolve()}" scan --quiet'
    entries = data.setdefault("hooks", {}).setdefault("SessionEnd", [])
    if any("moneysaved.py" in json.dumps(e) for e in entries):
        print("Hook already installed in", settings)
        return
    if settings.exists():
        shutil.copy2(settings, settings.with_suffix(".json.bak"))
    entries.append({"hooks": [{"type": "command", "command": cmd}]})
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Installed SessionEnd hook in {settings} (backup: settings.json.bak)")
    print("Every Claude Code session now updates the Claude Code Costs store when it ends.")


# --------------------------------------------------------------------------- cli

def main(argv: list) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(prog="moneysaved", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p_plan = sub.add_parser("plan", help="select the plan to compare with")
    p_plan.add_argument("value", type=str.lower, choices=list(PLANS))
    p_bill = sub.add_parser("billing", help="select billing cycle")
    p_bill.add_argument("value", choices=["monthly", "annual"])
    p_start = sub.add_parser("start", help="first month the plan was paid for, YYYY-MM (or 'none')")
    p_start.add_argument("value")
    p_cur = sub.add_parser("currency", help="display currency: USD, EUR, DKK (or any currency in per_eur)")
    p_cur.add_argument("value", type=str.upper)
    p_rep = sub.add_parser("report", help="print the report without rescanning")
    p_rep.add_argument("--months", type=int, help="only show the last N months")
    p_rep.add_argument("--projects", action="store_true", help="add a per-project breakdown")
    p_scan = sub.add_parser("scan", help="update the local store only")
    p_scan.add_argument("--quiet", action="store_true")
    p_rescan = sub.add_parser("rescan", help="re-read every transcript from the start (repairs the store after a fix)")
    p_rescan.add_argument("--quiet", action="store_true")
    sub.add_parser("status", help="one-line summary")
    sub.add_parser("live", help="open the live window that ticks up in real time")
    sub.add_parser("config", help="show configuration")
    sub.add_parser("install-hook", help="run scan automatically when a Claude Code session ends")
    ap.add_argument("--projects", action="store_true", help="add a per-project breakdown to the default report")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.cmd in ("plan", "billing", "currency"):
        if args.cmd == "currency" and args.value != "USD" and args.value not in cfg["per_eur"]:
            print(f"Unknown currency {args.value}: use USD, or add its rate to per_eur in config.json "
                  f"(now: {', '.join(cfg['per_eur'])})", file=sys.stderr)
            return 2
        cfg[args.cmd] = args.value
        save_config(cfg)
        print(f"{args.cmd} = {args.value}  ->  {describe_plan(cfg)}")
        if plan_billing(cfg) != cfg["billing"]:
            print(f"{plan_name(cfg)} is billed monthly only, so the monthly price is used.")
        return 0
    if args.cmd == "start":
        value = None if args.value.lower() == "none" else args.value
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m")
            except ValueError:
                print("Expected YYYY-MM, for example 2026-01", file=sys.stderr)
                return 2
        cfg["subscription_start"] = value
        save_config(cfg)
        print(f"subscription_start = {value}  (months before this are ignored)")
        return 0
    if args.cmd == "config":
        print(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")}, indent=2, ensure_ascii=False))
        print(f"\nActive plan: {describe_plan(cfg)}   config file: {CONFIG_PATH}")
        return 0
    if args.cmd == "install-hook":
        install_hook()
        return 0
    if args.cmd == "live":
        sys.path.insert(0, str(HERE))
        import moneysaved_live

        moneysaved_live.main()
        return 0

    db = open_db()
    try:
        if args.cmd == "scan":
            scan(db, cfg, quiet=args.quiet)
        elif args.cmd == "rescan":
            rescan(db, cfg, quiet=args.quiet)
        elif args.cmd == "report":
            report(db, cfg, months=args.months, projects=args.projects)
        elif args.cmd == "status":
            scan(db, cfg, quiet=True)
            status(db, cfg)
        else:
            scan(db, cfg)
            report(db, cfg, projects=args.projects)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
