"""Claude Code Costs live window - a small desktop display that ticks up in real time.

Open it by double-clicking "Claude Code Costs.pyw", or run `python moneysaved.py live`.
Every second it reads whatever Claude Code appended to the transcripts,
then glides the numbers to their new values so the counter visibly ticks while a
session is running. Nothing is invented: the display only ever moves toward the
real total.
"""
from __future__ import annotations

import sys
import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import moneysaved as ms  # noqa: E402

POLL_MS = 1000  # how often the transcripts are re-read
FRAME_MS = 40  # animation frame
GLIDE = 0.12  # fraction of the remaining distance covered per frame
PAD_TOP = 18  # window margin above the content
PAD_BOTTOM = 6  # window margin below the bottom line
PERIODS = ("Last hour", "Today", "This week", "This month")  # statistics rows; all fully covered by a fresh install

BG = "#0f1115"
PANEL = "#161a21"
FG = "#e9ecf1"
MUTED = "#8a919c"
GREEN = "#3ddc84"
AMBER = "#f5b942"
RED = "#ff6b6b"
BAR_BG = "#232833"
SELECTED = "#2b3240"  # fill of the chosen switch option: a step lighter than the pill, not a spotlight

UI = ("Segoe UI", 10)
SMALL = ("Segoe UI", 9)
TINY = ("Segoe UI", 6)  # the collapse arrow
BIG = ("Consolas", 44, "bold")


class Odometer:
    """A value that glides toward its target, so updates read as ticking rather than jumping."""

    def __init__(self) -> None:
        self.value: float | None = None
        self.target = 0.0

    def set(self, target: float) -> None:
        self.target = target
        if self.value is None:
            self.value = target

    def step(self) -> float:
        if self.value is None:
            return 0.0
        diff = self.target - self.value
        if abs(diff) < 0.004:
            self.value = self.target
        else:
            self.value += diff * GLIDE
        return self.value


def label(parent, text="", font=UI, fg=FG, **kw) -> tk.Label:
    return tk.Label(parent, text=text, font=font, fg=fg, bg=parent["bg"], **kw)


def apply_dark_title_bar(root: tk.Tk) -> list:
    """Ask Windows for a dark title bar in the window's own colour. Harmless elsewhere or on failure."""
    results = []
    if sys.platform != "win32":
        return results
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwm = ctypes.windll.dwmapi

        def set_attr(attr: int, value: int) -> int:
            v = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        def colorref(hex_color: str) -> int:  # Windows wants 0x00BBGGRR
            r, g, b = _rgb(hex_color)
            return (b << 16) | (g << 8) | r

        results.append(("dark mode", set_attr(20, 1)))  # DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 20H1+)
        results.append(("caption colour", set_attr(35, colorref(BG))))  # DWMWA_CAPTION_COLOR (Windows 11)
        results.append(("border colour", set_attr(34, colorref(BG))))  # DWMWA_BORDER_COLOR (Windows 11)
        results.append(("caption text", set_attr(36, colorref(FG))))  # DWMWA_TEXT_COLOR (Windows 11)
    except Exception as exc:  # older Windows or no DWM: keep the default title bar
        results.append(("error", str(exc)))
    return results


def _rgb(hex_color: str):
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def _blend(a, b, t: float):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _coverage(px: int, py: int, x1, y1, x2, y2, r, n: int = 4) -> float:
    """Fraction of the pixel at (px, py) covered by the rounded rectangle x1,y1..x2,y2 with corner radius r."""
    if px + 1 <= x1 or px >= x2 or py + 1 <= y1 or py >= y2:
        return 0.0
    # Pixels away from the corners are fully inside; only corner pixels need sampling.
    if (x1 + r <= px and px + 1 <= x2 - r and y1 <= py and py + 1 <= y2) or (
        y1 + r <= py and py + 1 <= y2 - r and x1 <= px and px + 1 <= x2
    ):
        return 1.0
    inside = 0
    for i in range(n):
        x = px + (i + 0.5) / n
        for j in range(n):
            y = py + (j + 0.5) / n
            if not (x1 <= x <= x2 and y1 <= y <= y2):
                continue
            cx = x1 + r if x < x1 + r else (x2 - r if x > x2 - r else None)
            cy = y1 + r if y < y1 + r else (y2 - r if y > y2 - r else None)
            if cx is None or cy is None or (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                inside += 1
    return inside / (n * n)


def render_pill(w: int, h: int, bg: str, pill: str, inner, inner_fill: str) -> tk.PhotoImage:
    """Anti-aliased pill (and optional inner pill) as a PhotoImage. Tk's canvas cannot smooth edges itself."""
    bg_rgb, pill_rgb, inner_rgb = _rgb(bg), _rgb(pill), _rgb(inner_fill)
    rows = []
    for py in range(h):
        row = []
        for px in range(w):
            col = _blend(bg_rgb, pill_rgb, _coverage(px, py, 0, 0, w, h, h / 2))
            if inner:
                x1, y1, x2, y2 = inner
                col = _blend(col, inner_rgb, _coverage(px, py, x1, y1, x2, y2, (y2 - y1) / 2))
            row.append("#%02x%02x%02x" % col)
        rows.append("{" + " ".join(row) + "}")
    img = tk.PhotoImage(width=w, height=h)
    img.put(" ".join(rows))
    return img


def render_box(size: int, r: float, bg: str, border: str, fill: str) -> tk.PhotoImage:
    """Anti-aliased rounded square with a 1 px border; pass fill == bg for an outline-only box."""
    bg_rgb, border_rgb, fill_rgb = _rgb(bg), _rgb(border), _rgb(fill)
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            col = _blend(bg_rgb, border_rgb, _coverage(px, py, 0, 0, size, size, r))
            col = _blend(col, fill_rgb, _coverage(px, py, 1, 1, size - 1, size - 1, max(r - 1, 0)))
            row.append("#%02x%02x%02x" % col)
        rows.append("{" + " ".join(row) + "}")
    img = tk.PhotoImage(width=size, height=size)
    img.put(" ".join(rows))
    return img


class RoundCheck(tk.Frame):
    """A checkbox with rounded corners, matching the switches. Clicking the box or its text toggles it."""

    SIZE = 15
    RADIUS = 4

    def __init__(self, parent, text: str, variable: tk.BooleanVar, command) -> None:
        super().__init__(parent, bg=parent["bg"], cursor="hand2")
        self.var = variable
        self.command = command
        bg = parent["bg"]
        self.img = render_box(self.SIZE, self.RADIUS, bg, MUTED, bg)  # outline only; checked adds a mark
        self.box = tk.Canvas(self, width=self.SIZE, height=self.SIZE, bg=bg, highlightthickness=0, bd=0)
        self.box.pack(side="left")
        self.lbl = label(self, text, SMALL, MUTED)
        self.lbl.pack(side="left", padx=(6, 0))
        for w in (self, self.box, self.lbl):
            w.bind("<Button-1>", self._toggle)
            w.bind("<Enter>", lambda _e: self.lbl.config(fg=FG))
            w.bind("<Leave>", lambda _e: self.lbl.config(fg=MUTED))
        self._draw()

    def _toggle(self, _event=None) -> None:
        self.var.set(not self.var.get())
        self._draw()
        self.command()

    def _draw(self) -> None:
        self.box.delete("all")
        self.box.create_image(0, 0, anchor="nw", image=self.img)
        if self.var.get():
            self.box.create_text(self.SIZE / 2, self.SIZE / 2, text="✓", font=(SMALL[0], 8, "bold"), fill=FG)


class MiniTable:
    """A heading, column headers and rows of (name, API value, calls, tokens) drawn into a shared grid
    from (row0, col0) downwards, so its columns line up with the other tables in that grid."""

    COLUMNS = ("API value", "Calls", "Tokens")
    PADX = (0, 8)  # gap to the right of every column; shared with the period table so the columns align

    def __init__(self, grid: tk.Frame, heading: str, row0: int, col0: int, pady_top: int = 0) -> None:
        self.grid = grid
        self.row0 = row0
        self.col0 = col0
        label(grid, heading, SMALL, MUTED).grid(row=row0, column=col0, columnspan=4, sticky="w", pady=(pady_top, 2))
        for j, col in enumerate(self.COLUMNS, start=1):
            label(grid, col, SMALL, MUTED).grid(row=row0 + 1, column=col0 + j, sticky="e", padx=self.PADX)
        self.rows = []

    def update(self, items) -> bool:
        """Show `items` as rows. Returns True when the number of visible rows changed (the window must refit)."""
        changed = False
        while len(self.rows) < len(items):
            i = self.row0 + 2 + len(self.rows)
            name = label(self.grid, "", SMALL, MUTED)
            name.grid(row=i, column=self.col0, sticky="w", padx=self.PADX, pady=(2, 0))
            cells = [label(self.grid, "", SMALL, FG) for _ in self.COLUMNS]
            for j, cell in enumerate(cells, start=1):
                cell.grid(row=i, column=self.col0 + j, sticky="e", padx=self.PADX, pady=(2, 0))
            self.rows.append((name, *cells))
            changed = True
        for k, widgets in enumerate(self.rows):
            shown = bool(widgets[0].winfo_manager())
            if k < len(items):
                for w, text in zip(widgets, items[k]):
                    w.config(text=text)
                if not shown:
                    for w in widgets:
                        w.grid()
                    changed = True
            elif shown:
                for w in widgets:
                    w.grid_remove()
                changed = True
        return changed


class Segmented(tk.Canvas):
    """A pill-shaped row of options of which exactly one is selected, with smooth rounded corners."""

    PAD_X = 14  # horizontal padding inside each option
    PAD_Y = 4  # vertical padding inside each option
    GAP = 3  # gap between the outer pill and the options

    def __init__(self, parent, options, command, **kw) -> None:
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0, cursor="hand2", **kw)
        self.options = options
        self.command = command
        self.value = None
        self.font = tkfont.Font(family=SMALL[0], size=SMALL[1])
        self.widths = [self.font.measure(text) + 2 * self.PAD_X for _, text in options]
        self.h = self.font.metrics("linespace") + 2 * self.PAD_Y + 2 * self.GAP
        self.w = sum(self.widths) + 2 * self.GAP
        self.config(width=self.w, height=self.h)
        # One pre-rendered image per selectable option; rendering is done once, not on every refresh.
        self.images = {}
        x = self.GAP
        for (v, _text), w in zip(options, self.widths):
            inner = (x, self.GAP, x + w, self.h - self.GAP)
            self.images[v] = render_pill(self.w, self.h, parent["bg"], PANEL, inner, SELECTED)
            x += w
        self.bind("<Button-1>", self._on_click)
        self.set(options[0][0])

    def set(self, value) -> None:
        if value == self.value:
            return
        self.value = value
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self.images.get(value, next(iter(self.images.values()))))
        x = self.GAP
        for (v, text), w in zip(self.options, self.widths):
            self.create_text(
                x + w / 2, self.h / 2, text=text, font=self.font, fill=FG if v == value else MUTED
            )
            x += w

    def _on_click(self, event) -> None:
        x = self.GAP
        for (v, _text), w in zip(self.options, self.widths):
            if x <= event.x < x + w:
                if v != self.value:
                    self.set(v)
                    self.command(v)
                return
            x += w

    def _click(self, value) -> None:  # kept for tests and programmatic use
        if value != self.value:
            self.set(value)
            self.command(value)


class LiveWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Claude Code Costs")
        root.configure(bg=BG)
        root.bind("<Escape>", lambda _e: root.destroy())
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.cfg = ms.load_config()
        self.db = ms.open_db()
        self.month_api = Odometer()
        self.summary: dict | None = None
        self.error: str | None = None
        self.warning: str | None = None  # e.g. a model with no price; shown in amber when there is no error
        self.bar_fraction = 0.0
        self.bar_color = AMBER

        self._build()
        self._fit()
        self.title_bar = apply_dark_title_bar(root)
        self.poll()
        self.animate()

    def _fit(self) -> None:
        """Size the window to its content so there is no slack below the bottom line."""
        self.root.update_idletasks()
        height = self.outer.winfo_reqheight() + PAD_TOP + PAD_BOTTOM
        # Fit the widest content (the statistics tables count even while collapsed, so expanding
        # never changes the width): 24 px margin each side plus a little breathing room.
        width = max(self.outer.winfo_reqwidth(), self.stats_table.winfo_reqwidth()) + 48 + 8
        self.root.minsize(width, height)
        self.root.geometry(f"{width}x{height}")

    def _show_error(self) -> None:
        """Show the error (red) or, failing that, the warning (amber) under the content; hide when neither."""
        text = self.error or self.warning
        shown = bool(self.error_lbl.winfo_manager())
        if text:
            width = self.outer.winfo_width() or 480
            self.error_lbl.config(text=text, fg=RED if self.error else AMBER, wraplength=width, justify="left")
            if not shown:
                self.error_lbl.pack(fill="x", pady=(8, 0))
                self._fit()
        elif shown:
            self.error_lbl.pack_forget()
            self._fit()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        outer = self.outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=24, pady=(PAD_TOP, PAD_BOTTOM))

        # The options name themselves (Pro / Max / Ultra, USD / EUR / DKK), so the switches carry no
        # captions; that leaves room for the plan's price without widening the window.
        switches = tk.Frame(outer, bg=BG)
        switches.pack(fill="x")
        self.plan_switch = Segmented(
            switches, [(key, name) for key, (name, _prices) in ms.PLANS.items()], self._set_plan
        )
        self.plan_switch.pack(side="left")
        tk.Frame(switches, bg=BG, width=18).pack(side="left")
        self.currency_switch = Segmented(
            switches, [("USD", "USD"), ("EUR", "EUR"), ("DKK", "DKK")], self._set_currency
        )
        self.currency_switch.pack(side="left")
        self.plan_price_lbl = label(switches, "", SMALL, MUTED)
        self.plan_price_lbl.pack(side="right", padx=(12, 0))

        self.caption_lbl = label(
            outer, "API value of this month's Claude Code usage on this PC", SMALL, MUTED, anchor="w"
        )
        self.caption_lbl.pack(fill="x", pady=(14, 0))
        self.big_lbl = label(outer, "—", BIG, FG, anchor="w")
        self.big_lbl.pack(fill="x")

        self.bar = tk.Canvas(outer, height=8, bg=BG, highlightthickness=0)
        self.bar.pack(fill="x", pady=(2, 6))
        self.bar.bind("<Configure>", lambda _e: self._draw_bar())
        self.bar_lbl = label(outer, "", SMALL, AMBER, anchor="w")
        self.bar_lbl.pack(fill="x")

        tk.Frame(outer, bg=BAR_BG, height=1).pack(fill="x", pady=14)

        # Statistics: only periods that a fresh install can always see in full (Claude Code keeps
        # transcripts for 30 days), so the numbers here are complete regardless of install date.
        stats = tk.Frame(outer, bg=BG)
        stats.pack(fill="x")
        self.stats_expanded = False  # starts collapsed; the arrow or the caption expands it
        cap_row = tk.Frame(stats, bg=BG)
        cap_row.pack(fill="x")
        self.stats_caption = label(cap_row, "Statistics", SMALL, MUTED, cursor="hand2")
        self.stats_caption.pack(side="left")
        self.stats_caption.bind("<Button-1>", lambda _e: self._toggle_stats())
        self.stats_btn = label(cap_row, "▼", TINY, MUTED, cursor="hand2")
        self.stats_btn.pack(side="left", padx=(8, 0))
        self.stats_btn.bind("<Button-1>", lambda _e: self._toggle_stats())
        self.stats_btn.bind("<Enter>", lambda _e: self.stats_btn.config(fg=FG))
        self.stats_btn.bind("<Leave>", lambda _e: self.stats_btn.config(fg=MUTED))
        self.pin_var = tk.BooleanVar(value=False)
        self.pin = RoundCheck(cap_row, "Pin on top", self.pin_var, self._toggle_pin)
        self.pin.pack(side="right")
        self.updated_lbl = label(cap_row, "", SMALL, MUTED)
        self.updated_lbl.pack(side="right", padx=(0, 12))

        # Left grid: the period table with the model table beneath it in the same columns, so
        # API value / Calls / Tokens line up. Right grid: the project table, top-aligned with the left.
        self.stats_table = tk.Frame(stats, bg=BG)  # packed only while expanded
        left = tk.Frame(self.stats_table, bg=BG)
        left.pack(side="left", anchor="n")
        right = tk.Frame(self.stats_table, bg=BG)
        right.pack(side="left", anchor="n", padx=(28, 0))

        label(left, "By period", SMALL, MUTED).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 2))
        for j, heading in enumerate(MiniTable.COLUMNS, start=1):
            label(left, heading, SMALL, MUTED).grid(row=1, column=j, sticky="e", padx=MiniTable.PADX)
        self.period_rows = {}  # period name -> ([value, calls, tokens labels], Odometer for the value)
        for i, name in enumerate(PERIODS, start=2):
            label(left, name, SMALL, MUTED).grid(row=i, column=0, sticky="w", padx=MiniTable.PADX, pady=(2, 0))
            cells = [label(left, "", SMALL, FG) for _ in MiniTable.COLUMNS]
            for j, cell in enumerate(cells, start=1):
                cell.grid(row=i, column=j, sticky="e", padx=MiniTable.PADX, pady=(2, 0))
            self.period_rows[name] = (cells, Odometer())
        self.model_table = MiniTable(left, "By model, this month", row0=len(PERIODS) + 2, col0=0, pady_top=14)
        self.project_table = MiniTable(right, "By project, this month", row0=0, col0=0)

        self.error_lbl = label(outer, "", SMALL, RED, anchor="w")  # only packed while there is an error

    def _toggle_pin(self) -> None:
        self.root.attributes("-topmost", bool(self.pin_var.get()))

    def _toggle_stats(self) -> None:
        self.stats_expanded = not self.stats_expanded
        if self.stats_expanded:
            self.stats_table.pack(fill="x", pady=(8, 0))
        else:
            self.stats_table.pack_forget()
        self.stats_btn.config(text="▲" if self.stats_expanded else "▼")
        self._fit()

    def _set_plan(self, value: str) -> None:
        self._set_option("plan", value)

    def _set_currency(self, value: str) -> None:
        self._set_option("currency", value)

    def _set_option(self, key: str, value: str) -> None:
        """Save the choice so the CLI sees it too, then recompute so the numbers glide to the new values."""
        try:
            self.cfg[key] = value
            ms.save_config(self.cfg)
            self.summary = ms.summary(self.db, self.cfg)
            self.error = None
            self._apply_summary()
            self._fit()  # the price beside the switches changes length with the plan and currency
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        self._show_error()

    def _draw_bar(self) -> None:
        w = max(self.bar.winfo_width(), 1)
        h = 8
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w, h, fill=BAR_BG, outline="")
        fill_w = int(w * max(0.0, min(1.0, self.bar_fraction)))
        if fill_w > 0:
            self.bar.create_rectangle(0, 0, fill_w, h, fill=self.bar_color, outline="")

    # ------------------------------------------------------------------ data

    def poll(self) -> None:
        try:
            self.cfg = ms.load_config()
            ms.scan(self.db, self.cfg, quiet=True)
            self.summary = ms.summary(self.db, self.cfg)
            self.error = None
            self._apply_summary()
        except Exception as exc:  # keep ticking even if one read fails
            self.error = f"{type(exc).__name__}: {exc}"
        self._show_error()
        self.root.after(POLL_MS, self.poll)

    def _apply_summary(self) -> None:
        s, cfg = self.summary, self.cfg
        self.month_api.set(s["month_api"])
        self.plan_switch.set(cfg["plan"])  # keeps the switches in step with changes made from the CLI
        self.currency_switch.set(cfg["currency"].upper())
        annual = " (annual)" if ms.plan_billing(cfg) == "annual" else ""
        self.plan_price_lbl.config(text=f"{ms.money(s['plan_price'], cfg)}/month{annual} ex VAT")
        for name, (cells, odometer) in self.period_rows.items():
            p = s["periods"][name]
            odometer.set(p["api"])
            cells[1].config(text=f"{p['calls']:,}")
            cells[2].config(text=ms.fmt_tokens(p["tokens"]))
        model_rows = [(ms.pretty_model(m), ms.money(v, cfg), f"{c:,}", ms.fmt_tokens(t)) for m, v, c, t in s["by_model"]]
        if s["month_unrecorded"] > 0:  # requests Claude Code counted that never became transcript messages
            model_rows.append(("Not in transcripts", ms.money(s["month_unrecorded"], cfg), "-", "-"))
        changed = self.model_table.update(model_rows)
        self.warning = (
            "No price for model(s) " + ", ".join(s["unknown_models"])
            + ": counted as 0. Add them to PRICES in moneysaved.py."
            if s["unknown_models"]
            else None
        )
        changed |= self.project_table.update(
            [(p[:24], ms.money(v, cfg), f"{c:,}", ms.fmt_tokens(t)) for p, v, c, t in s["by_project"]]
        )
        if changed:  # row count changed: refit height if expanded, and width in any case
            self._fit()
        self.updated_lbl.config(text="updated " + datetime.now().strftime("%H:%M:%S"))

    # ------------------------------------------------------------------ animation

    def animate(self) -> None:
        if self.summary is not None:
            s, cfg = self.summary, self.cfg
            price = s["plan_price"]
            api = self.month_api.step()
            self.big_lbl.config(text=ms.money(api, cfg))

            saved = api - price
            if price > 0 and saved >= 0:
                self.bar_fraction, self.bar_color = 1.0, GREEN
                self.bar_lbl.config(
                    text=f"{ms.plan_name(cfg)} paid off  ·  saved {ms.money(saved, cfg, signed=True)} so far this month",
                    fg=GREEN,
                )
            elif price > 0:
                self.bar_fraction, self.bar_color = api / price, AMBER
                self.bar_lbl.config(
                    text=f"{api / price * 100:.0f}% of the {ms.money(price, cfg)} {ms.plan_name(cfg)} plan  ·  "
                    f"{ms.money(-saved, cfg)} to break-even",
                    fg=AMBER,
                )
            else:
                self.bar_fraction, self.bar_color = 0.0, MUTED
                self.bar_lbl.config(text="Plan price is 0 in config.json", fg=MUTED)
            self._draw_bar()

            if self.stats_expanded:
                for cells, odometer in self.period_rows.values():
                    cells[0].config(text=ms.money(odometer.step(), cfg))
        self.root.after(FRAME_MS, self.animate)

    def close(self) -> None:
        try:
            self.db.close()
        finally:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    LiveWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
