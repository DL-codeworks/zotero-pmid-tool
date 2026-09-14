#!/usr/bin/env python3
"""Modern dark GUI frontend for pmid_docx_to_zotero.py.

The Zotero/DOCX conversion logic remains in pmid_docx_to_zotero.py. This GUI
runs that exact engine in a worker thread, redirects its console output into the
window, and turns blocking input() prompts into visible dark modal dialogs.

No third-party GUI dependency is required; this stays compatible with the
single-file PyInstaller build.
"""

import builtins
import contextlib
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog
import tkinter.font as tkfont

import pmid_docx_to_zotero as core

APP_TITLE = "Zotero PMID Tool"
APP_VERSION = "1.5"
REQUIREMENTS_NOTICE_VERSION = 2

# Dark palette
BG = "#111318"
PANEL = "#1a1d24"
PANEL_ALT = "#161920"
FIELD = "#0f1116"
BORDER = "#343945"
BORDER_FOCUS = "#5b8cff"
FG = "#f4f6f8"
MUTED = "#a7afba"
SUBTLE = "#7d8590"
ACCENT = "#5b8cff"
ACCENT_HOVER = "#6c98ff"
ACCENT_PRESS = "#4a79e2"
SUCCESS = "#57c78a"
DANGER = "#ef6b73"
WARNING = "#e0ad56"
DISABLED = "#2a2e37"


def gui_settings_path():
    return core.app_data_dir() / "gui_settings.json"


def load_gui_settings():
    try:
        return json.loads(gui_settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_gui_settings(settings):
    try:
        path = gui_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except Exception:
        pass


def rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """Reliable rounded rectangle using arcs + rectangles, not a smoothed polygon."""
    radius = max(1, int(radius))
    radius = min(radius, max(1, int((x2 - x1) / 2)), max(1, int((y2 - y1) / 2)))
    diameter = radius * 2
    tags = kwargs.pop("tags", None)
    ids = []
    ids.append(canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, tags=tags, **kwargs))
    ids.append(canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, tags=tags, **kwargs))
    ids.append(canvas.create_arc(x1, y1, x1 + diameter, y1 + diameter,
                                 start=90, extent=90, style="pieslice", tags=tags, **kwargs))
    ids.append(canvas.create_arc(x2 - diameter, y1, x2, y1 + diameter,
                                 start=0, extent=90, style="pieslice", tags=tags, **kwargs))
    ids.append(canvas.create_arc(x1, y2 - diameter, x1 + diameter, y2,
                                 start=180, extent=90, style="pieslice", tags=tags, **kwargs))
    ids.append(canvas.create_arc(x2 - diameter, y2 - diameter, x2, y2,
                                 start=270, extent=90, style="pieslice", tags=tags, **kwargs))
    return ids


class RoundedPanel(tk.Frame):
    """Frame whose geometry is driven by normal Tk layout, with a rounded canvas behind it."""

    def __init__(self, master, bg_color=PANEL, border_color=BORDER, radius=8,
                 padding=14, title=None, **kwargs):
        tk.Frame.__init__(self, master, bg=BG, bd=0, highlightthickness=0, **kwargs)
        self.bg_color = bg_color
        self.border_color = border_color
        self.radius = radius
        self.padding = padding
        self.canvas = tk.Canvas(self, bg=BG, bd=0, highlightthickness=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        pad_frame = tk.Frame(self, bg=bg_color, bd=0, highlightthickness=0)
        pad_frame.pack(fill="both", expand=True, padx=padding, pady=padding)
        if title:
            tk.Label(pad_frame, text=title, bg=bg_color, fg=FG,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 9))
            self.inner = tk.Frame(pad_frame, bg=bg_color, bd=0, highlightthickness=0)
            self.inner.pack(fill="both", expand=True)
        else:
            self.inner = pad_frame
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        self.canvas.delete("panel")
        # border
        rounded_rect(self.canvas, 0, 0, w - 1, h - 1, self.radius,
                     fill=self.border_color, outline=self.border_color, tags="panel")
        # inner fill, 1px inset
        rounded_rect(self.canvas, 1, 1, w - 2, h - 2, max(2, self.radius - 1),
                     fill=self.bg_color, outline=self.bg_color, tags="panel")
        self.canvas.tag_lower("panel")


class RoundedButton(tk.Canvas):
    def __init__(self, master, text, command=None, accent=False, width=None, height=34,
                 radius=6, font=None, **kwargs):
        self._font = font or tkfont.Font(family="Segoe UI", size=9, weight="bold" if accent else "normal")
        if width is None:
            width = max(74, self._font.measure(text) + 26)
        tk.Canvas.__init__(self, master, width=width, height=height, bg=master.cget("bg"),
                           bd=0, highlightthickness=0, cursor="hand2", **kwargs)
        self.text = text
        self.command = command
        self.accent = accent
        self.radius = radius
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            self.configure_cursor()
            self._draw()
        if "text" in kwargs:
            self.text = kwargs.pop("text")
            self._draw()
        if kwargs:
            return tk.Canvas.configure(self, **kwargs)
        return None

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        return tk.Canvas.cget(self, key)

    def configure_cursor(self):
        tk.Canvas.configure(self, cursor="hand2" if self._state != "disabled" else "arrow")

    def _colors(self):
        if self._state == "disabled":
            return DISABLED, "#777f89", DISABLED
        if self.accent:
            fill = ACCENT_PRESS if self._pressed else (ACCENT_HOVER if self._hover else ACCENT)
            return fill, "#ffffff", fill
        fill = "#313640" if self._pressed else ("#373d48" if self._hover else "#292e37")
        return fill, FG, BORDER

    def _draw(self):
        self.delete("all")
        w = max(2, int(float(tk.Canvas.cget(self, "width"))))
        h = max(2, int(float(tk.Canvas.cget(self, "height"))))
        fill, fg, border = self._colors()
        rounded_rect(self, 0, 0, w - 1, h - 1, self.radius,
                     fill=border, outline=border)
        rounded_rect(self, 1, 1, w - 2, h - 2, max(2, self.radius - 1),
                     fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self._font)

    def _on_enter(self, _event):
        if self._state != "disabled":
            self._hover = True
            self._draw()

    def _on_leave(self, _event):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event):
        if self._state != "disabled":
            self._pressed = True
            self._draw()

    def _on_release(self, event):
        if self._state == "disabled":
            return
        was_pressed = self._pressed
        self._pressed = False
        self._draw()
        if was_pressed and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            if self.command:
                self.command()


class RoundedEntry(tk.Canvas):
    def __init__(self, master, textvariable=None, height=36, radius=6, **kwargs):
        tk.Canvas.__init__(self, master, height=height, bg=master.cget("bg"), bd=0,
                           highlightthickness=0, **kwargs)
        self.radius = radius
        self.height_px = height
        self._state = "normal"
        self._focus = False
        self.entry = tk.Entry(
            self, textvariable=textvariable, bg=FIELD, fg=FG, insertbackground=FG,
            disabledbackground="#20242b", disabledforeground=SUBTLE,
            relief="flat", bd=0, highlightthickness=0,
            font=("Segoe UI", 9)
        )
        self.window_id = self.create_window(12, height / 2, window=self.entry, anchor="w")
        self.bind("<Configure>", self._redraw)
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self._redraw()

    def _focus_in(self, _event=None):
        self._focus = True
        self._redraw()

    def _focus_out(self, _event=None):
        self._focus = False
        self._redraw()

    def _redraw(self, _event=None):
        self.delete("field")
        w = max(60, self.winfo_width())
        h = self.height_px
        border = BORDER_FOCUS if self._focus and self._state != "disabled" else BORDER
        fill = "#20242b" if self._state == "disabled" else FIELD
        rounded_rect(self, 0, 0, w - 1, h - 1, self.radius,
                     fill=border, outline=border, tags="field")
        rounded_rect(self, 1, 1, w - 2, h - 2, max(2, self.radius - 1),
                     fill=fill, outline=fill, tags="field")
        self.tag_lower("field")
        self.itemconfigure(self.window_id, width=max(20, w - 24))
        self.entry.configure(bg=fill)

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            self.entry.configure(state=self._state)
            self._redraw()
        if kwargs:
            return tk.Canvas.configure(self, **kwargs)
        return None

    config = configure

    def focus_set(self):
        self.entry.focus_set()

    def bind_entry(self, sequence, func):
        self.entry.bind(sequence, func)

    def get(self):
        return self.entry.get()


class ModernCheckbox(tk.Frame):
    def __init__(self, master, text, variable, command=None, **kwargs):
        tk.Frame.__init__(self, master, bg=master.cget("bg"), bd=0, **kwargs)
        self.variable = variable
        self.command = command
        self._state = "normal"
        self.box = tk.Canvas(self, width=18, height=18, bg=self.cget("bg"), bd=0,
                             highlightthickness=0, cursor="hand2")
        self.box.pack(side="left")
        self.label = tk.Label(self, text=text, bg=self.cget("bg"), fg=FG,
                              font=("Segoe UI", 9), cursor="hand2")
        self.label.pack(side="left", padx=(7, 0))
        self.box.bind("<Button-1>", self._toggle)
        self.label.bind("<Button-1>", self._toggle)
        try:
            self.variable.trace_add("write", lambda *_args: self._draw())
        except AttributeError:
            self.variable.trace("w", lambda *_args: self._draw())
        self._draw()

    def _toggle(self, _event=None):
        if self._state == "disabled":
            return
        self.variable.set(not bool(self.variable.get()))
        if self.command:
            self.command()

    def _draw(self):
        self.box.delete("all")
        selected = bool(self.variable.get())
        fill = ACCENT if selected and self._state != "disabled" else FIELD
        border = ACCENT if selected and self._state != "disabled" else BORDER
        rounded_rect(self.box, 1, 1, 17, 17, 4, fill=border, outline=border)
        rounded_rect(self.box, 2, 2, 16, 16, 3, fill=fill, outline=fill)
        if selected:
            color = "#ffffff" if self._state != "disabled" else SUBTLE
            self.box.create_line(5, 9, 8, 12, 13, 6, fill=color, width=2,
                                 capstyle="round", joinstyle="round")
        self.label.configure(fg=FG if self._state != "disabled" else SUBTLE)
        cursor = "hand2" if self._state != "disabled" else "arrow"
        self.box.configure(cursor=cursor)
        self.label.configure(cursor=cursor)

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            self._draw()
        if kwargs:
            return tk.Frame.configure(self, **kwargs)
        return None

    config = configure


class ActivityBar(tk.Canvas):
    """Indeterminate activity bar that freezes after one step while waiting for input."""

    def __init__(self, master, height=8, **kwargs):
        tk.Canvas.__init__(self, master, height=height, bg=master.cget("bg"), bd=0,
                           highlightthickness=0, **kwargs)
        self.height_px = height
        self.position = 0.02
        self.direction = 1
        self.running = False
        self.after_id = None
        self.mode = "idle"
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(20, self.winfo_width())
        h = self.height_px
        rounded_rect(self, 0, 0, w - 1, h - 1, 4, fill=FIELD, outline=FIELD)
        if self.mode == "finished":
            rounded_rect(self, 0, 0, w - 1, h - 1, 4, fill=SUCCESS, outline=SUCCESS)
            return
        if self.mode == "error":
            rounded_rect(self, 0, 0, w - 1, h - 1, 4, fill=DANGER, outline=DANGER)
            return
        if self.mode in ("working", "waiting"):
            segment = max(42, int(w * 0.18))
            x = int((w - segment) * self.position)
            rounded_rect(self, x, 0, min(w - 1, x + segment), h - 1, 4,
                         fill=ACCENT, outline=ACCENT)

    def start(self):
        self.mode = "working"
        self.running = True
        if self.after_id is None:
            self._tick()

    def _tick(self):
        self.after_id = None
        if not self.running:
            return
        self.position += 0.018 * self.direction
        if self.position >= 1.0:
            self.position = 1.0
            self.direction = -1
        elif self.position <= 0.0:
            self.position = 0.0
            self.direction = 1
        self._draw()
        self.after_id = self.after(28, self._tick)

    def pause_for_input(self):
        self.running = False
        if self.after_id is not None:
            try:
                self.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        # One discrete step, then freeze exactly where it lands.
        self.position += 0.08 * self.direction
        if self.position >= 1.0:
            self.position = 1.0
            self.direction = -1
        elif self.position <= 0.0:
            self.position = 0.0
            self.direction = 1
        self.mode = "waiting"
        self._draw()

    def stop_idle(self):
        self.running = False
        if self.after_id is not None:
            try:
                self.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        self.mode = "idle"
        self.position = 0.02
        self.direction = 1
        self._draw()

    def finish(self, ok=True):
        self.running = False
        if self.after_id is not None:
            try:
                self.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        self.mode = "finished" if ok else "error"
        self._draw()


class QueueWriter(object):
    def __init__(self, output_queue):
        self.output_queue = output_queue
        self._buffer = ""

    def write(self, text):
        if not text:
            return 0
        self._buffer += str(text)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.output_queue.put(("log", line + "\n"))
        return len(text)

    def flush(self):
        if self._buffer:
            self.output_queue.put(("log", self._buffer))
            self._buffer = ""

    def isatty(self):
        return False


class InputRequest(object):
    def __init__(self, prompt):
        self.prompt = prompt
        self.event = threading.Event()
        self.answer = ""


class ZoteroCitationGUI(object):
    def __init__(self, root):
        self.root = root
        self.root.title("{} {}".format(APP_TITLE, APP_VERSION))
        self.root.geometry("940x900")
        self.root.minsize(820, 760)
        self.root.configure(bg=BG)

        self.output_queue = queue.Queue()
        self.input_queue = queue.Queue()
        self.running = False
        self.worker = None
        self.current_input = None
        self.prompt_dialog = None
        self.run_started_at = 0.0
        self.last_output_docx = None
        self.last_report = None
        self.last_ris = None
        self.recent_log_lines = []

        self.settings = load_gui_settings()
        self.docx_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.make_ris_var = tk.BooleanVar(value=False)
        self.make_report_var = tk.BooleanVar(value=False)
        self.use_collection_var = tk.BooleanVar(value=False)
        self.collection_name_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose a Word document to begin.")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll_queues)
        if self.settings.get("requirements_notice_version") != REQUIREMENTS_NOTICE_VERSION:
            self.root.after(250, self._show_first_run_requirements)

    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG, padx=20, pady=18)
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(7, weight=1)

        # Header
        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text=APP_TITLE, bg=BG, fg=FG,
                 font=("Segoe UI", 19, "bold")).pack(side="left")
        tk.Label(header, text="v{}".format(APP_VERSION), bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(9, 0), pady=(8, 0))
        tk.Label(outer,
                 text="Turn PMID/DOI placeholders into live Zotero citations in Word.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).grid(
                     row=1, column=0, sticky="w", pady=(3, 14))

        # Inputs are intentionally normal grid-managed panels. They cannot collapse/disappear.
        input_panel = RoundedPanel(outer, title="Document", radius=8, padding=13)
        input_panel.grid(row=2, column=0, sticky="ew")
        input_panel.inner.grid_columnconfigure(0, weight=1)
        self.docx_entry = RoundedEntry(input_panel.inner, textvariable=self.docx_var)
        self.docx_entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = RoundedButton(input_panel.inner, "Browse…", self._browse,
                                           width=92, height=36, radius=6)
        self.browse_button.grid(row=0, column=1, padx=(9, 0))

        output_panel = RoundedPanel(outer, title="Output", radius=8, padding=13)
        output_panel.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        output_panel.inner.grid_columnconfigure(0, weight=1)
        tk.Label(output_panel.inner, text="Output folder", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).grid(row=0, column=0, columnspan=2, sticky="w")
        self.output_dir_entry = RoundedEntry(output_panel.inner, textvariable=self.output_dir_var)
        self.output_dir_entry.grid(row=1, column=0, sticky="ew", pady=(5, 10))
        self.output_browse_button = RoundedButton(output_panel.inner, "Browse…",
                                                  self._browse_output_dir,
                                                  width=92, height=36, radius=6)
        self.output_browse_button.grid(row=1, column=1, padx=(9, 0), pady=(5, 10))
        tk.Label(output_panel.inner, text="Extra output files", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 4))
        extra_row = tk.Frame(output_panel.inner, bg=PANEL)
        extra_row.grid(row=3, column=0, columnspan=2, sticky="w")
        self.ris_check = ModernCheckbox(extra_row, "PubMed RIS backup/export", self.make_ris_var)
        self.ris_check.pack(side="left")
        self.report_check = ModernCheckbox(extra_row, "Conversion report", self.make_report_var)
        self.report_check.pack(side="left", padx=(24, 0))

        # Optional Zotero collection destination. This is configured inline so
        # the normal collection choice does not interrupt conversion with a modal.
        collection_panel = RoundedPanel(outer, title="Zotero collection", radius=8, padding=13)
        collection_panel.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        collection_panel.inner.grid_columnconfigure(0, weight=1)
        self.collection_check = ModernCheckbox(
            collection_panel.inner,
            "Put references in a collection/folder",
            self.use_collection_var,
            command=self._toggle_collection_controls,
        )
        self.collection_check.grid(row=0, column=0, sticky="w")
        tk.Label(
            collection_panel.inner, text="Collection name or path", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 8)
        ).grid(row=1, column=0, sticky="w", pady=(9, 0))
        self.collection_entry = RoundedEntry(
            collection_panel.inner, textvariable=self.collection_name_var
        )
        self.collection_entry.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        tk.Label(
            collection_panel.inner,
            text="Uses a unique existing match automatically. If no collection matches, a new top-level collection is created with this name.",
            bg=PANEL, fg=SUBTLE, font=("Segoe UI", 8), justify="left", wraplength=820
        ).grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._toggle_collection_controls()

        # Actions + activity indicator
        action_row = tk.Frame(outer, bg=BG)
        action_row.grid(row=5, column=0, sticky="ew", pady=(12, 8))
        self.run_button = RoundedButton(action_row, "Convert Citations", self._start_run,
                                        accent=True, width=138, height=36, radius=7)
        self.run_button.pack(side="left")
        self.open_output_button = RoundedButton(action_row, "Open Output", self._open_output,
                                                width=104, height=36, radius=7)
        self.open_output_button.pack(side="left", padx=(8, 0))
        self.open_output_button.configure(state="disabled")
        self.open_folder_button = RoundedButton(action_row, "Open Folder", self._open_folder,
                                                width=104, height=36, radius=7)
        self.open_folder_button.pack(side="left", padx=(8, 0))
        self.open_folder_button.configure(state="disabled")
        self.activity = ActivityBar(action_row, width=200, height=8)
        self.activity.pack(side="right", padx=(12, 0), pady=(14, 0))

        # Status
        status_row = tk.Frame(outer, bg=BG)
        status_row.grid(row=6, column=0, sticky="ew", pady=(0, 9))
        self.status_dot = tk.Canvas(status_row, width=10, height=10, bg=BG, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(1, 7), pady=(2, 0))
        self.status_dot_id = self.status_dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")
        tk.Label(status_row, textvariable=self.status_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left", anchor="w")

        # Log/progress output. Use a standard frame for maximum cross-platform reliability.
        log_panel = RoundedPanel(outer, title="Progress", radius=8, padding=10)
        log_panel.grid(row=7, column=0, sticky="nsew")
        log_panel.inner.grid_rowconfigure(1, weight=1)
        log_panel.inner.grid_columnconfigure(0, weight=1)
        tk.Label(
            log_panel.inner,
            text="If nothing is happening, check Zotero for a permissions popup.",
            bg=PANEL, fg=WARNING, font=("Segoe UI", 8)
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        text_wrap = tk.Frame(log_panel.inner, bg=FIELD, bd=0)
        text_wrap.grid(row=1, column=0, sticky="nsew")
        self.log = tk.Text(
            text_wrap, wrap="word", state="disabled", font=("Cascadia Mono", 9),
            bg=FIELD, fg=FG, insertbackground=FG, selectbackground=ACCENT,
            selectforeground="#ffffff", relief="flat", bd=0, padx=10, pady=9
        )
        scrollbar = tk.Scrollbar(text_wrap, orient="vertical", command=self.log.yview,
                                 bg="#2b3038", troughcolor=FIELD, activebackground="#39404b",
                                 bd=0, highlightthickness=0)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        tk.Label(outer,
                 text="The original DOCX is never overwritten. Zotero must be open while converting.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).grid(
                     row=8, column=0, sticky="w", pady=(9, 0))

    def _toggle_collection_controls(self):
        if not hasattr(self, "collection_entry"):
            return
        if self.running:
            state = "disabled"
        else:
            state = "normal" if bool(self.use_collection_var.get()) else "disabled"
        self.collection_entry.configure(state=state)

    def _browse(self):
        filename = filedialog.askopenfilename(
            title="Choose Word manuscript",
            filetypes=[("Word documents", "*.docx"), ("All files", "*.*")]
        )
        if filename:
            path = Path(filename)
            self.docx_var.set(str(path))
            self.output_dir_var.set(str(path.parent))
            self.status_var.set("Ready to convert.")
            self._set_status_dot(MUTED)

    def _browse_output_dir(self):
        initial = self.output_dir_var.get().strip()
        if not initial and self.docx_var.get().strip():
            try:
                initial = str(Path(self.docx_var.get().strip().strip('"')).parent)
            except Exception:
                initial = ""
        folder = filedialog.askdirectory(title="Choose output folder", initialdir=initial or None)
        if folder:
            self.output_dir_var.set(folder)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")
        for line in str(text).splitlines():
            if line.strip():
                self.recent_log_lines.append(line.rstrip())
        self.recent_log_lines = self.recent_log_lines[-80:]

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.recent_log_lines = []

    def _show_dark_message(self, title, message, kind="info", button_text="OK", on_close=None):
        if self.prompt_dialog is not None:
            return
        dialog = tk.Toplevel(self.root)
        self.prompt_dialog = dialog
        dialog.title(title)
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        shell = tk.Frame(dialog, bg=BG, padx=14, pady=14)
        shell.pack(fill="both", expand=True)
        card = RoundedPanel(shell, radius=9, padding=18)
        card.pack(fill="both", expand=True)
        title_color = DANGER if kind == "error" else (WARNING if kind == "warning" else FG)
        tk.Label(card.inner, text=title, bg=PANEL, fg=title_color,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(card.inner, text=message, bg=PANEL, fg=MUTED, justify="left",
                 wraplength=590, font=("Segoe UI", 9)).pack(anchor="w", fill="x", pady=(9, 16))

        def close_dialog():
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()
            self.prompt_dialog = None
            if on_close:
                on_close()

        btn = RoundedButton(card.inner, button_text, close_dialog, accent=True,
                            width=max(82, 18 + tkfont.Font(family="Segoe UI", size=9, weight="bold").measure(button_text)),
                            height=34, radius=6)
        btn.pack(anchor="e")
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        self._center_dialog(dialog)

    def _start_run(self):
        if self.running:
            return
        raw = self.docx_var.get().strip().strip('"')
        path = Path(raw).expanduser() if raw else None
        if path is None or not path.exists() or path.suffix.lower() != ".docx":
            self._show_dark_message(APP_TITLE, "Choose an existing .docx file first.", kind="error")
            return

        raw_output = self.output_dir_var.get().strip().strip('"')
        output_dir = Path(raw_output).expanduser() if raw_output else path.parent
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_dark_message(APP_TITLE,
                                    "Could not create/access the output folder:\n{}".format(exc),
                                    kind="error")
            return
        if not output_dir.is_dir():
            self._show_dark_message(APP_TITLE, "Output location is not a folder.", kind="error")
            return

        collection_name = ""
        if bool(self.use_collection_var.get()):
            collection_name = self.collection_name_var.get().strip()
            if not collection_name:
                self._show_dark_message(
                    APP_TITLE,
                    "Enter a collection name/path, or uncheck the collection option.",
                    kind="error"
                )
                return

        self.docx_var.set(str(path))
        self.output_dir_var.set(str(output_dir))
        self._clear_log()
        self._hide_input()
        self.last_output_docx = None
        self.last_report = None
        self.last_ris = None
        self.open_output_button.configure(state="disabled")
        self.open_folder_button.configure(state="disabled")

        self.running = True
        self.run_started_at = time.time()
        self._set_controls_enabled(False)
        self.activity.start()
        self.status_var.set("Processing…")
        self._set_status_dot(ACCENT)

        self.worker = threading.Thread(
            target=self._worker_run,
            args=(
                path, str(output_dir), bool(self.make_ris_var.get()),
                bool(self.make_report_var.get()), collection_name
            ),
            daemon=True
        )
        self.worker.start()

    def _set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.run_button.configure(state=state)
        self.browse_button.configure(state=state)
        self.output_browse_button.configure(state=state)
        self.docx_entry.configure(state=state)
        self.output_dir_entry.configure(state=state)
        self.ris_check.configure(state=state)
        self.report_check.configure(state=state)
        self.collection_check.configure(state=state)
        self._toggle_collection_controls()

    def _worker_run(self, path, output_dir, make_ris, make_report, collection_name):
        old_argv = list(sys.argv)
        old_input = builtins.input
        writer = QueueWriter(self.output_queue)

        def gui_input(prompt=""):
            request = InputRequest(str(prompt or ""))
            self.input_queue.put(request)
            request.event.wait()
            return request.answer

        result = {"ok": True, "message": ""}
        try:
            argv = ["pmid_docx_to_zotero.py", str(path), "--output-dir", output_dir]
            if not make_ris:
                argv.append("--no-ris")
            if not make_report:
                argv.append("--no-report")
            # Always pass --collection from the GUI. An empty value means
            # "My Library only" and suppresses the CLI collection prompt.
            argv.extend(["--collection", collection_name])
            sys.argv = argv
            builtins.input = gui_input
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    core.main()
                except SystemExit as exc:
                    code = exc.code
                    if code not in (None, 0):
                        result["ok"] = False
                        result["message"] = str(code)
        except Exception as exc:
            result["ok"] = False
            result["message"] = "{}: {}".format(type(exc).__name__, exc)
        finally:
            writer.flush()
            builtins.input = old_input
            sys.argv = old_argv
        self.output_queue.put(("finished", result, str(path)))

    def _poll_queues(self):
        try:
            while True:
                item = self.output_queue.get_nowait()
                if item[0] == "log":
                    self._append_log(item[1])
                elif item[0] == "finished":
                    self._finish_run(item[1], Path(item[2]))
        except queue.Empty:
            pass

        if self.current_input is None:
            try:
                request = self.input_queue.get_nowait()
            except queue.Empty:
                request = None
            if request is not None:
                self._show_input(request)
        self.root.after(80, self._poll_queues)

    def _show_input(self, request):
        self.current_input = request
        self.activity.pause_for_input()
        self.status_var.set("Waiting for your input.")
        self._set_status_dot(WARNING)
        self._open_prompt_dialog(request)
        try:
            self.root.bell()
        except Exception:
            pass

    def _prompt_spec(self, prompt):
        lower = prompt.lower()
        if prompt.strip() == "Choice:":
            return {
                "title": "Reference collection",
                "message": "Do you want to put references in a specific collection/folder?",
                "buttons": [
                    ("No — My Library", ""),
                    ("Existing collection", "e"),
                    ("New collection", "n")
                ],
                "default": ""
            }
        if "ignore and finish" in lower and "re-check" in lower:
            return {
                "title": "Unresolved references",
                "message": "Some references are still unresolved. Choose what the converter should do.",
                "buttons": [
                    ("Ignore and finish", "i"),
                    ("Re-check Zotero", "r"),
                    ("Quit", "q")
                ],
                "default": "i"
            }
        if "choose collection number" in lower:
            choices = []
            for line in self.recent_log_lines[-30:]:
                if re.match(r"^\s*\d+\.\s+", line):
                    choices.append(line.strip())
            extra = ""
            if choices:
                extra = "\n\n" + "\n".join(choices[-12:])
            return {
                "title": "Choose collection",
                "message": "More than one collection matched. Enter the collection number." + extra,
                "entry": True,
                "placeholder": "Collection number",
                "cancel": ("Cancel", ""),
                "default": ""
            }
        if "existing collection name/path" in lower:
            return {
                "title": "Find collection",
                "message": "Type part of the existing Zotero collection name or path. If there is exactly one match, it will be accepted automatically.",
                "entry": True,
                "placeholder": "Collection name or path",
                "cancel": ("Cancel", "/"),
                "default": "/"
            }
        if "new collection name" in lower:
            return {
                "title": "New collection",
                "message": "Enter the name for the new Zotero collection.",
                "entry": True,
                "placeholder": "Collection name",
                "default": ""
            }
        return {
            "title": "Input needed",
            "message": prompt.strip() or "The conversion needs a response before it can continue.",
            "entry": True,
            "placeholder": "Response",
            "default": ""
        }

    def _open_prompt_dialog(self, request):
        if self.prompt_dialog is not None:
            try:
                self.prompt_dialog.destroy()
            except Exception:
                pass
        spec = self._prompt_spec(request.prompt)
        dialog = tk.Toplevel(self.root)
        self.prompt_dialog = dialog
        dialog.title(spec.get("title", "Input needed"))
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        shell = tk.Frame(dialog, bg=BG, padx=14, pady=14)
        shell.pack(fill="both", expand=True)
        card = RoundedPanel(shell, radius=9, padding=18)
        card.pack(fill="both", expand=True)
        tk.Label(card.inner, text=spec.get("title", "Input needed"), bg=PANEL, fg=FG,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(card.inner, text=spec.get("message", ""), bg=PANEL, fg=MUTED,
                 justify="left", wraplength=620, font=("Segoe UI", 9)).pack(
                     anchor="w", fill="x", pady=(8, 15))

        buttons = spec.get("buttons")
        if buttons:
            row = tk.Frame(card.inner, bg=PANEL)
            row.pack(fill="x")
            for index, pair in enumerate(buttons):
                label, answer = pair
                btn = RoundedButton(
                    row, label, command=lambda value=answer: self._answer_input(value),
                    accent=(index == 0), height=34, radius=6
                )
                btn.pack(side="left", padx=(0, 8))
        else:
            entry_var = tk.StringVar()
            entry = RoundedEntry(card.inner, textvariable=entry_var)
            entry.pack(fill="x")
            entry.focus_set()
            row = tk.Frame(card.inner, bg=PANEL)
            row.pack(fill="x", pady=(12, 0))
            RoundedButton(row, "Continue", lambda: self._answer_input(entry_var.get()),
                          accent=True, height=34, radius=6).pack(side="left")
            if spec.get("cancel"):
                label, answer = spec["cancel"]
                RoundedButton(row, label, lambda value=answer: self._answer_input(value),
                              height=34, radius=6).pack(side="left", padx=(8, 0))
            entry.bind_entry("<Return>", lambda _event: self._answer_input(entry_var.get()))

        default_answer = spec.get("default", "")
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._answer_input(default_answer))
        self._center_dialog(dialog)

    def _answer_input(self, answer):
        request = self.current_input
        if request is None:
            return
        request.answer = str(answer)
        self.current_input = None
        if self.prompt_dialog is not None:
            try:
                self.prompt_dialog.grab_release()
            except Exception:
                pass
            try:
                self.prompt_dialog.destroy()
            except Exception:
                pass
            self.prompt_dialog = None
        request.event.set()
        self.status_var.set("Processing…")
        self._set_status_dot(ACCENT)
        self.activity.start()

    def _hide_input(self):
        if self.prompt_dialog is not None:
            try:
                self.prompt_dialog.grab_release()
            except Exception:
                pass
            try:
                self.prompt_dialog.destroy()
            except Exception:
                pass
            self.prompt_dialog = None
        self.current_input = None

    def _finish_run(self, result, input_path):
        self.running = False
        self._set_controls_enabled(True)
        self._hide_input()
        self._discover_outputs(input_path)
        ok = bool(result.get("ok"))
        self.activity.finish(ok=ok)

        if self.last_output_docx and self.last_output_docx.exists():
            self.open_output_button.configure(state="normal")
            self.open_folder_button.configure(state="normal")

        if ok and self.last_output_docx:
            self.status_var.set("Finished successfully.")
            self._set_status_dot(SUCCESS)
            self._show_dark_message(
                APP_TITLE,
                "Finished.\n\nCreated:\n{}".format(self.last_output_docx),
                kind="info"
            )
        elif ok:
            self.status_var.set("Finished.")
            self._set_status_dot(SUCCESS)
        else:
            self.status_var.set("Stopped with an error.")
            self._set_status_dot(DANGER)
            message = result.get("message") or "The conversion stopped with an error. See the progress log."
            self._show_dark_message(APP_TITLE, message, kind="error")

    def _discover_outputs(self, input_path):
        raw_output = self.output_dir_var.get().strip().strip('"')
        folder = Path(raw_output) if raw_output else input_path.parent
        stem = input_path.stem

        def newest(pattern):
            candidates = []
            for path in folder.glob(pattern):
                try:
                    if path.stat().st_mtime >= self.run_started_at - 2:
                        candidates.append(path)
                except OSError:
                    pass
            return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

        self.last_output_docx = newest(stem + "_zotero_citations_*.docx")
        self.last_report = newest(stem + "_zotero_citations_*_REPORT.txt") if self.make_report_var.get() else None
        self.last_ris = newest(stem + "_pubmed_citations_*.ris") if self.make_ris_var.get() else None

    def _open_output(self):
        if self.last_output_docx and self.last_output_docx.exists():
            try:
                os.startfile(str(self.last_output_docx))
            except AttributeError:
                self._show_dark_message(APP_TITLE, str(self.last_output_docx))
            except OSError as exc:
                self._show_dark_message(APP_TITLE, "Could not open output:\n{}".format(exc), kind="error")

    def _open_folder(self):
        if self.last_output_docx:
            path = self.last_output_docx
        elif self.output_dir_var.get().strip():
            path = Path(self.output_dir_var.get().strip().strip('"'))
        else:
            path = Path(self.docx_var.get()).parent if self.docx_var.get() else None
        if not path:
            return
        folder = path.parent if path.suffix else path
        try:
            os.startfile(str(folder))
        except AttributeError:
            self._show_dark_message(APP_TITLE, str(folder))
        except OSError as exc:
            self._show_dark_message(APP_TITLE, "Could not open folder:\n{}".format(exc), kind="error")

    def _show_first_run_requirements(self):
        if self.prompt_dialog is not None:
            return
        dialog = tk.Toplevel(self.root)
        self.prompt_dialog = dialog
        dialog.title("Before you start")
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        shell = tk.Frame(dialog, bg=BG, padx=14, pady=14)
        shell.pack(fill="both", expand=True)
        card = RoundedPanel(shell, radius=9, padding=18)
        card.pack(fill="both", expand=True)

        tk.Label(
            card.inner, text="Zotero PMID Tool requirements",
            bg=PANEL, fg=FG, font=("Segoe UI", 14, "bold")
        ).pack(anchor="w")

        text = (
            "1. In Zotero, go to Settings → Advanced and enable:\n"
            "   ‘Allow other applications on this computer to communicate with Zotero.’\n\n"
            "2. On your first run of this tool Zotero will ask you for permission to "
            "communicate with this app with a popup. Choose Always Allow.\n\n"
            "This tool will create a new copy of your file. The original DOCX is never overwritten."
        )
        tk.Label(
            card.inner, text=text, bg=PANEL, fg=MUTED, justify="left",
            wraplength=610, font=("Segoe UI", 9)
        ).pack(anchor="w", fill="x", pady=(12, 16))

        dont_show_var = tk.BooleanVar(value=False)
        dont_show = ModernCheckbox(card.inner, "Don't show this again", dont_show_var)
        dont_show.pack(anchor="w", pady=(0, 16))

        def close_dialog(save_choice=False):
            if save_choice and bool(dont_show_var.get()):
                self.settings["requirements_notice_version"] = REQUIREMENTS_NOTICE_VERSION
                save_gui_settings(self.settings)
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()
            self.prompt_dialog = None

        RoundedButton(
            card.inner, "Continue", lambda: close_dialog(save_choice=True),
            accent=True, width=92, height=34, radius=6
        ).pack(anchor="e")

        # Closing the window dismisses the notice for this launch only.
        dialog.protocol("WM_DELETE_WINDOW", lambda: close_dialog(save_choice=False))
        self._center_dialog(dialog)

    def _center_dialog(self, dialog):
        dialog.update_idletasks()
        try:
            x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_height()) // 2
            dialog.geometry("+{}+{}".format(max(0, x), max(0, y)))
        except Exception:
            pass

    def _set_status_dot(self, color):
        try:
            self.status_dot.itemconfigure(self.status_dot_id, fill=color)
        except Exception:
            pass

    def _on_close(self):
        if self.running:
            self._show_confirm_close()
            return
        self.root.destroy()

    def _show_confirm_close(self):
        if self.prompt_dialog is not None:
            return
        dialog = tk.Toplevel(self.root)
        self.prompt_dialog = dialog
        dialog.title("Conversion running")
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        shell = tk.Frame(dialog, bg=BG, padx=14, pady=14)
        shell.pack(fill="both", expand=True)
        card = RoundedPanel(shell, radius=9, padding=18)
        card.pack(fill="both", expand=True)
        tk.Label(card.inner, text="A conversion is still running", bg=PANEL, fg=WARNING,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(card.inner,
                 text="Closing now will stop the application. Do you want to close anyway?",
                 bg=PANEL, fg=MUTED, justify="left", wraplength=520,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(9, 15))
        row = tk.Frame(card.inner, bg=PANEL)
        row.pack(fill="x")

        def cancel():
            try:
                dialog.grab_release()
            except Exception:
                pass
            dialog.destroy()
            self.prompt_dialog = None

        def close_now():
            try:
                dialog.grab_release()
            except Exception:
                pass
            self.root.destroy()

        RoundedButton(row, "Keep running", cancel, accent=True, height=34, radius=6).pack(side="left")
        RoundedButton(row, "Close anyway", close_now, height=34, radius=6).pack(side="left", padx=(8, 0))
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        self._center_dialog(dialog)


def main():
    root = tk.Tk()
    ZoteroCitationGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
