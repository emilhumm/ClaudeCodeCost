"""Double-click to open the Claude Code Costs live window without a console."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import moneysaved_live

    moneysaved_live.main()
except Exception:  # no console to print to, so show the error in a dialog
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Claude Code Costs could not start", traceback.format_exc())
