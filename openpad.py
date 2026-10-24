#!/usr/bin/env python3
"""OpenPad — a small, local-first Windows Notepad-inspired text editor for Linux.

Copyright (c) 2026 OpenPad contributors. MIT License.
Only Python's standard library (including tkinter) is required.
"""
from __future__ import annotations

import argparse
import json
import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import filedialog, font as tkfont, messagebox, simpledialog
from typing import Optional

APP_NAME = "OpenPad"
VERSION = "1.2.0"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "openpad"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "openpad"
CONFIG_FILE = CONFIG_DIR / "settings.json"
SESSION_FILE = STATE_DIR / "session.json"

LIGHT = {
    "chrome": "#f3f4f7", "tabs": "#ebeef3", "tab": "#e2e7ee",
    "active": "#ffffff", "editor": "#ffffff", "fg": "#202020",
    "muted": "#62666b", "border": "#e2e4e9", "hover": "#d9dfe8",
    "selection": "#c3dcff", "accent": "#0067c0", "input": "#ffffff",
    "button": "#f1f2f5", "danger": "#c42b1c", "found": "#fff1b6",
}
DARK = {
    "chrome": "#252525", "tabs": "#202020", "tab": "#2c2c2c",
    "active": "#333333", "editor": "#272727", "fg": "#f6f6f6",
    "muted": "#b3b3b3", "border": "#3c3c3c", "hover": "#444444",
    "selection": "#264f78", "accent": "#65b7ff", "input": "#333333",
    "button": "#3c3c3c", "danger": "#e94c4c", "found": "#745b26",
}
DEFAULTS = {
    "theme": "light", "font_family": "", "font_size": 11,
    "word_wrap": True, "status_bar": True, "restore_session": True,
    "native_titlebar": True, "recent": [], "find_wrap": True,
    "find_whole_word": False, "markdown_toolbar": False, "show_hscroll": True,
}


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fallback


def atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".openpad-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def detect_text(data: bytes) -> tuple[str, str, str]:
    """Decode and retain original encoding + predominant line-ending convention."""
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        decoded = data.decode(encoding)
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        try:
            decoded = data.decode(encoding)
        except UnicodeDecodeError:
            encoding = "latin-1"
            decoded = data.decode(encoding)
    if "\x00" in decoded:
        raise ValueError("This appears to be a binary file, not a text document.")
    crlf = decoded.count("\r\n")
    cr = decoded.count("\r") - crlf
    lf = decoded.count("\n") - crlf
    eol = max(((crlf, "\r\n"), (lf, "\n"), (cr, "\r")), key=lambda item: item[0])[1]
    return decoded.replace("\r\n", "\n").replace("\r", "\n"), encoding, eol


def encoded_text(text: str, encoding: str, eol: str) -> bytes:
    return text.replace("\n", eol).encode(encoding)


def atomic_bytes(path: Path, data: bytes) -> None:
    """Write safely without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = None
    try:
        previous_mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if previous_mode is not None:
            os.chmod(name, previous_mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@dataclass
class Document:
    text: tk.Text
    panel: tk.Frame
    path: Optional[Path] = None
    encoding: str = "utf-8"
    eol: str = "\r\n"
    saved_text: str = ""
    untitled_name: str = "Untitled"
    suppress: bool = False
    dirty: bool = False
    metadata_dirty: bool = False

    def value(self) -> str:
        # Tk.Text's required trailing newline is not user content.
        return self.text.get("1.0", "end-1c")

    def title(self) -> str:
        if self.path:
            return self.path.name
        for line in self.value().splitlines():
            line = line.strip()
            if line:
                return line[:32] + ("…" if len(line) > 32 else "")
        return self.untitled_name


class TabStrip(tk.Canvas):
    """Deterministically rounded tabs, with symmetric top corners on Linux."""
    def __init__(self, master, app):
        super().__init__(master, height=41, highlightthickness=0, bd=0,
                         cursor="arrow", relief="flat", takefocus=1)
        self.app = app
        self.hits = []
        self.close_hits = []
        self.plus_hit = (0, 0)
        self.offset = 0
        self._dragging = None
        self.bind("<Configure>", lambda _: self.render())
        self.bind("<Button-1>", self.clicked)
        self.bind("<ButtonPress-1>", self.drag_start, add="+")
        self.bind("<ButtonRelease-1>", self.drag_end)
        self.bind("<MouseWheel>", self.wheel)
        self.bind("<Button-4>", lambda _: self.scroll(-1))
        self.bind("<Button-5>", lambda _: self.scroll(1))
        self.bind("<Button-2>", self.middle_close)

    def scroll(self, direction):
        self.offset = max(0, self.offset + direction * 135)
        self.render()

    def wheel(self, event):
        self.scroll(-1 if event.delta > 0 else 1)

    def hit_tab(self, x):
        for x1, x2, idx in self.hits:
            if x1 <= x <= x2:
                return idx
        return None

    def middle_close(self, event):
        i = self.hit_tab(event.x)
        if i is not None:
            self.app.close_tab(i)

    def clicked(self, event):
        if self.plus_hit[0] <= event.x <= self.plus_hit[1]:
            self.app.new_document()
            return
        for left, right, idx in self.close_hits:
            if left <= event.x <= right:
                self.app.close_tab(idx)
                return
        i = self.hit_tab(event.x)
        if i is not None:
            self.app.set_current(i)

    def drag_start(self, event):
        if any(left <= event.x <= right for left, right, _ in self.close_hits):
            self._dragging = None
        elif self.plus_hit[0] <= event.x <= self.plus_hit[1]:
            self._dragging = None
        else:
            self._dragging = self.hit_tab(event.x)

    def drag_end(self, event):
        src = self._dragging
        dst = self.hit_tab(event.x)
        self._dragging = None
        if src is not None and dst is not None and src != dst:
            self.app.move_tab(src, dst)

    def rounded_top(self, x1, y1, x2, y2, radius, color):
        # Four simple filled shapes give truly mirrored tab corners;
        # Tk's smooth Bezier polygon used before skewed the right edge.
        r = min(radius, (x2 - x1) / 3)
        self.create_rectangle(x1+r, y1, x2-r, y2, fill=color, outline="")
        self.create_rectangle(x1, y1+r, x2, y2, fill=color, outline="")
        self.create_arc(x1, y1, x1+2*r, y1+2*r, start=90, extent=90,
                        style=tk.PIESLICE, fill=color, outline="")
        self.create_arc(x2-2*r, y1, x2, y1+2*r, start=0, extent=90,
                        style=tk.PIESLICE, fill=color, outline="")

    def render(self):
        self.delete("all")
        p = self.app.palette
        w = self.winfo_width()
        self.configure(bg=p["tabs"])
        self.create_rectangle(0, 39, w, 41, fill=p["border"], outline="")
        self.hits.clear()
        self.close_hits.clear()
        tabs = self.app.documents
        if not tabs:
            return
        tabw = max(130, min(190, (w - 54) // max(1, min(len(tabs), 6))))
        total = tabw * len(tabs)
        visible = max(0, w - 47)
        self.offset = min(self.offset, max(0, total - visible))
        for i, doc in enumerate(tabs):
            x1 = 3 + i * tabw - self.offset
            x2 = x1 + tabw - 4
            if x2 <= 0 or x1 >= visible:
                continue
            selected = i == self.app.current
            self.rounded_top(x1, 6, x2, 40, 10, p["active"] if selected else p["tab"])
            text_limit = max(8, (tabw - 61) // 8)
            name = doc.title()
            if len(name) > text_limit:
                name = name[:text_limit-1] + "…"
            self.create_text(x1+15, 23, text=name, anchor="w", fill=p["fg"],
                             font=self.app.ui_font)
            if doc.dirty:
                self.create_oval(x2-41, 20, x2-35, 26,
                                 fill=p["muted"], outline="")
            self.create_text(x2-17, 22.5, text="×", fill=p["muted"],
                             font=(self.app.ui_family, 15))
            self.close_hits.append((max(0, x2-31), min(x2, visible), i))
            self.hits.append((max(0, x1), min(x2, visible), i))
        plus_x = min(max(4, 3+total-self.offset), max(4, w-39))
        self.create_text(plus_x+16, 22, text="+", fill=p["fg"],
                         font=(self.app.ui_family, 18))
        self.plus_hit = (plus_x, plus_x+37)


class OpenPad:
    def __init__(self, paths: list[str], frameless=False):
        self.root = tk.Tk(className="OpenPad")
        self.root.title(APP_NAME)
        # Ensure the window/taskbar icon matches the desktop launcher icon.
        # Keep this reference alive: Tk image objects vanish when GC'd.
        icon_path = Path(__file__).resolve().parent / "assets" / "openpad-128.png"
        if not icon_path.exists():
            icon_path = Path(__file__).resolve().parent / "openpad.png"
        self.window_icon = None
        if icon_path.exists():
            try:
                self.window_icon = tk.PhotoImage(file=str(icon_path))
                self.root.iconphoto(True, self.window_icon)
            except tk.TclError:
                pass
        self.root.geometry("970x660")
        self.root.minsize(450, 290)
        loaded = load_json(CONFIG_FILE, {})
        self.settings = {**DEFAULTS, **(loaded if isinstance(loaded, dict) else {})}
        self.palette = LIGHT if self.settings["theme"] == "light" else DARK
        self.ui_family = self.choose_ui_font()
        self.ui_font = (self.ui_family, 10)
        self.editor_font = tkfont.Font(
            family=self.settings["font_family"] or self.choose_editor_font(),
            size=int(self.settings["font_size"]))
        self.documents: list[Document] = []
        self.closed_documents = []
        self.current = -1
        self.untitled_counter = 0
        self.autosave_id = None
        self.restore_pending = False
        self.separate_window = False
        self.find_visible = False
        self.replace_visible = False
        self.search_case = tk.BooleanVar(value=False)
        self.wrap_var = tk.BooleanVar(value=self.settings["word_wrap"])
        self.status_var = tk.BooleanVar(value=self.settings["status_bar"])
        self.markdown_var = tk.BooleanVar(value=self.settings.get("markdown_toolbar", False))
        self.word_var = tk.BooleanVar(value=self.settings.get("find_whole_word", False))
        self.search_wrap_var = tk.BooleanVar(value=self.settings.get("find_wrap", True))
        self.frameless = frameless
        self.native = not frameless
        if frameless:
            self.root.overrideredirect(True)
        self.make_ui()
        self.setup_shortcuts()
        self.apply_theme()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.bind("<Configure>", self.on_root_configure)
        self.startup_id = self.root.after(300, self.initial_documents, paths)

    def choose_ui_font(self):
        families = set(tkfont.families(self.root))
        for name in ("Segoe UI", "Inter", "Noto Sans", "Liberation Sans", "Arial"):
            if name in families:
                return name
        return "TkDefaultFont"

    def choose_editor_font(self):
        families = set(tkfont.families(self.root))
        for name in ("Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "Courier New"):
            if name in families:
                return name
        return "TkFixedFont"

    def icon(self, canvas):
        canvas.delete("all")
        canvas.create_rectangle(4, 4, 19, 23, fill="#f7fcff", outline="#5386a5", width=1)
        canvas.create_rectangle(4, 4, 19, 8, fill="#48a6cf", outline="")
        for y in (11, 14, 17, 20):
            canvas.create_line(7, y, 16, y, fill="#76a9bf")

    def make_ui(self):
        p = self.palette
        self.outer = tk.Frame(self.root, bd=0, bg=p["chrome"])
        self.outer.pack(fill="both", expand=True)
        self.top = tk.Frame(self.outer, bg=p["tabs"], height=41)
        self.top.pack(side="top", fill="x")
        self.top.pack_propagate(False)
        self.appicon = tk.Canvas(self.top, width=28, height=32, bd=0,
                                 highlightthickness=0, bg=p["tabs"])
        self.appicon.pack(side="left", padx=(6, 0))
        if self.window_icon is not None:
            self.small_icon = self.window_icon.subsample(4, 4)
            self.appicon.create_image(14, 16, image=self.small_icon)
        else:
            self.icon(self.appicon)
        self.gear = tk.Label(self.top, text="⚙", width=3,
                             font=(self.ui_family, 16), bg=p["tabs"], fg=p["muted"],
                             cursor="hand2")
        if self.frameless:
            self.close_button = tk.Label(self.top, text="×", width=4,
                font=(self.ui_family, 15), bg=p["tabs"], fg=p["fg"], cursor="hand2")
            self.max_button = tk.Label(self.top, text="□", width=4,
                font=(self.ui_family, 12), bg=p["tabs"], fg=p["fg"], cursor="hand2")
            self.min_button = tk.Label(self.top, text="−", width=4,
                font=(self.ui_family, 15), bg=p["tabs"], fg=p["fg"], cursor="hand2")
            self.close_button.pack(side="right", fill="y")
            self.max_button.pack(side="right", fill="y")
            self.min_button.pack(side="right", fill="y")
            self.close_button.bind("<Button-1>", lambda _: self.quit())
            self.max_button.bind("<Button-1>", lambda _: self.toggle_maximize())
            self.min_button.bind("<Button-1>", lambda _: self.minimize())
            self.top.bind("<ButtonPress-1>", self.drag_start)
            self.top.bind("<B1-Motion>", self.drag_move)
            self.appicon.bind("<ButtonPress-1>", self.drag_start)
            self.appicon.bind("<B1-Motion>", self.drag_move)
            self.maximized = False
            self.restore_geometry = None
        self.gear.pack(side="right", fill="y", padx=(0, 4))
        self.gear.bind("<Button-1>", lambda _: self.show_settings())
        self.tabstrip = TabStrip(self.top, self)
        self.tabstrip.pack(side="left", fill="both", expand=True)

        self.menu_row = tk.Frame(self.outer, height=36, bg=p["chrome"])
        self.menu_row.pack(fill="x", side="top")
        self.menu_row.pack_propagate(False)
        self.menu_buttons = {}
        self.menus = {}
        for label in ("File", "Edit", "View"):
            # Explicitly post the menu when clicked. Original Tk Menubutton
            # used a root-owned menu; on some GTK/X11 setups clicks did nothing.
            btn = tk.Button(self.menu_row, text=label,
                font=self.ui_font, relief="flat", bd=0, padx=10, pady=5,
                activebackground=p["hover"], bg=p["chrome"], fg=p["fg"],
                activeforeground=p["fg"], cursor="hand2", takefocus=1,
                highlightthickness=0, overrelief="flat",
                command=lambda name=label: self.popup_menu(name))
            btn.pack(side="left", padx=(3, 0), pady=(3, 3))
            self.menu_buttons[label] = btn
            self.menus[label] = tk.Menu(btn, tearoff=False)
        self.fill_menus()

        self.findpanel = tk.Frame(self.outer, bg=p["chrome"], height=0)
        self.find_row = tk.Frame(self.findpanel, bg=p["chrome"])
        self.find_row.pack(fill="x", padx=10, pady=5)
        self.search_entry = tk.Entry(self.find_row, width=29, relief="flat", bd=1,
                         font=self.ui_font, highlightthickness=1)
        self.search_entry.pack(side="left", ipady=5)
        self.search_entry.bind("<Return>", lambda _: self.find_next())
        self.search_entry.bind("<Shift-Return>", lambda _: self.find_next(previous=True))
        self.search_entry.bind("<KeyRelease>", lambda _: self.highlight_matches())
        self.make_button(self.find_row, "↑", lambda: self.find_next(previous=True), compact=True)
        self.make_button(self.find_row, "↓", self.find_next, compact=True)
        self.case_button = tk.Checkbutton(self.find_row, text="Match case", variable=self.search_case,
                        command=self.update_search_settings, font=self.ui_font,
                        bd=0, relief="flat")
        self.case_button.pack(side="left", padx=(9, 4))
        self.word_button = tk.Checkbutton(self.find_row, text="Whole word", variable=self.word_var,
                             command=self.update_search_settings, font=self.ui_font, bd=0, relief="flat")
        self.word_button.pack(side="left", padx=(0, 4))
        self.wrap_button = tk.Checkbutton(self.find_row, text="Wrap", variable=self.search_wrap_var,
                             command=self.update_search_settings, font=self.ui_font, bd=0, relief="flat")
        self.wrap_button.pack(side="left", padx=(0, 4))
        self.results = tk.Label(self.find_row, font=self.ui_font, anchor="w")
        self.results.pack(side="left", padx=8)
        self.make_button(self.find_row, "×", self.hide_find, compact=True, side="right")
        self.replace_row = tk.Frame(self.findpanel, bg=p["chrome"])
        self.replace_entry = tk.Entry(self.replace_row, width=29, relief="flat", bd=1,
                         font=self.ui_font, highlightthickness=1)
        self.replace_entry.pack(side="left", padx=(0, 4), ipady=5)
        self.make_button(self.replace_row, "Replace", self.replace_one)
        self.make_button(self.replace_row, "Replace all", self.replace_all)

        self.markdown_row = tk.Frame(self.outer, bg=p["chrome"], height=32)
        for caption, action in (
            ("B", lambda: self.markdown_inline("**")),
            ("I", lambda: self.markdown_inline("*")),
            ("S̶", lambda: self.markdown_inline("~~")),
            ("H", lambda: self.markdown_heading()),
            ("• List", lambda: self.markdown_prefix("- ")),
            ("1. List", lambda: self.markdown_prefix("1. ")),
            ("Link", self.markdown_link),
            ("Table", self.markdown_table),
        ):
            self.make_button(self.markdown_row, caption, action)
        if self.markdown_var.get():
            self.markdown_row.pack(side="top", fill="x", pady=2)

        self.editor_container = tk.Frame(self.outer, bg=p["editor"])
        self.editor_container.pack(fill="both", expand=True)

        self.status = tk.Frame(self.outer, bg=p["chrome"], height=25,
                               highlightbackground=p["border"], highlightthickness=1)
        self.status.pack(side="bottom", fill="x")
        self.status.pack_propagate(False)
        self.status_parts = []
        for width, anchor in [(19, "w"), (16, "w"), (9, "center"), (17, "center"), (10, "center")]:
            label = tk.Label(self.status, width=width, anchor=anchor, bg=p["chrome"],
                             fg=p["muted"], font=(self.ui_family, 9))
            label.pack(side="left", fill="y", padx=7)
            self.status_parts.append(label)
        self.status_parts[2].bind("<Button-1>", lambda _: self.reset_zoom())
        self.status_parts[3].bind("<Button-1>", self.line_ending_menu)
        self.status_parts[4].bind("<Button-1>", self.encoding_menu)

    def make_button(self, parent, text, command, compact=False, side="left"):
        p = self.palette
        button = tk.Button(parent, text=text, command=command,
                           font=(self.ui_family, 12 if compact else 10),
                           bg=p["button"], fg=p["fg"], activebackground=p["hover"],
                           activeforeground=p["fg"], relief="flat", bd=0,
                           padx=9 if compact else 12, pady=2,
                           highlightthickness=0, cursor="hand2")
        button.pack(side=side, padx=3)
        return button

    def fill_menus(self):
        file = self.menus["File"]
        file.delete(0, "end")
        items = [
            ("New tab", self.new_document, "Ctrl+N"),
            ("New window", self.new_window, "Ctrl+Shift+N"),
            ("Open…", self.open_dialog, "Ctrl+O"),
        ]
        for name, action, shortcut in items:
            file.add_command(label=name, command=action, accelerator=shortcut)
        recent_menu = tk.Menu(file, tearoff=False)
        recent = self.settings.get("recent", [])[:12]
        for path in recent:
            recent_menu.add_command(label=path, command=lambda p=path: self.open_path(Path(p)))
        if not recent:
            recent_menu.add_command(label="No recent files", state="disabled")
        recent_menu.add_separator()
        recent_menu.add_command(label="Clear list", command=self.clear_recent)
        file.add_cascade(label="Recent", menu=recent_menu)
        file.add_command(label="Reopen closed tab", command=self.reopen_closed_tab,
                         accelerator="Ctrl+Shift+T",
                         state="normal" if self.closed_documents else "disabled")
        file.add_separator()
        for name, action, shortcut in [
            ("Save", self.save, "Ctrl+S"),
            ("Save as…", self.save_as, "Ctrl+Shift+S"),
            ("Save all", self.save_all, "Ctrl+Alt+S"),
        ]:
            file.add_command(label=name, command=action, accelerator=shortcut)
        file.add_separator()
        file.add_command(label="Print…", command=self.print_document, accelerator="Ctrl+P")
        file.add_separator()
        file.add_command(label="Close tab", command=self.close_tab, accelerator="Ctrl+W")
        file.add_command(label="Exit", command=self.quit)

        edit = self.menus["Edit"]
        edit.delete(0, "end")
        for label, cmd, key in [
            ("Undo", lambda: self.text_action("edit_undo"), "Ctrl+Z"),
            ("Redo", lambda: self.text_action("edit_redo"), "Ctrl+Y"),
        ]:
            edit.add_command(label=label, command=cmd, accelerator=key)
        edit.add_separator()
        for label, command, key in [
            ("Cut", lambda: self.text_event("<<Cut>>"), "Ctrl+X"),
            ("Copy", lambda: self.text_event("<<Copy>>"), "Ctrl+C"),
            ("Paste", lambda: self.text_event("<<Paste>>"), "Ctrl+V"),
            ("Delete", self.delete_selected, "Del"),
        ]:
            edit.add_command(label=label, command=command, accelerator=key)
        edit.add_separator()
        edit.add_command(label="Find…", command=self.show_find, accelerator="Ctrl+F")
        edit.add_command(label="Find next", command=self.find_next, accelerator="F3")
        edit.add_command(label="Find previous", command=lambda: self.find_next(True), accelerator="Shift+F3")
        edit.add_command(label="Replace…", command=lambda: self.show_find(True), accelerator="Ctrl+H")
        edit.add_command(label="Go to…", command=self.go_to, accelerator="Ctrl+G")
        edit.add_separator()
        edit.add_command(label="Select all", command=lambda: self.text_event("<<SelectAll>>"), accelerator="Ctrl+A")
        edit.add_command(label="Time / Date", command=self.insert_datetime, accelerator="F5")
        edit.add_separator()
        edit.add_command(label="Insert line above", command=lambda: self.insert_line(False), accelerator="Ctrl+Shift+Enter")
        edit.add_command(label="Insert line below", command=lambda: self.insert_line(True), accelerator="Ctrl+Enter")
        edit.add_command(label="Duplicate line", command=self.duplicate_line, accelerator="Ctrl+D")

        view = self.menus["View"]
        view.delete(0, "end")
        zoom = tk.Menu(view, tearoff=False)
        zoom.add_command(label="Zoom in", command=lambda: self.zoom(1), accelerator="Ctrl++")
        zoom.add_command(label="Zoom out", command=lambda: self.zoom(-1), accelerator="Ctrl+-")
        zoom.add_command(label="Restore default zoom", command=self.reset_zoom, accelerator="Ctrl+0")
        view.add_cascade(label="Zoom", menu=zoom)
        view.add_separator()
        view.add_checkbutton(label="Status bar", variable=self.status_var, command=self.set_status)
        view.add_checkbutton(label="Word wrap", variable=self.wrap_var, command=self.set_wrap)
        view.add_checkbutton(label="Markdown toolbar", variable=self.markdown_var,
                             command=self.set_markdown_toolbar)
        view.add_separator()
        view.add_command(label="Settings", command=self.show_settings)
        for menu in (file, edit, view, recent_menu, zoom):
            menu.configure(bg=self.palette["active"], fg=self.palette["fg"],
                           activebackground=self.palette["hover"],
                           activeforeground=self.palette["fg"],
                           bd=1, relief="flat")

    def popup_menu(self, name):
        button = self.menu_buttons[name]
        menu = self.menus[name]
        self.fill_menus()  # Refresh recent files and check states at opening.
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def clear_recent(self):
        self.settings["recent"] = []
        self.save_settings()
        self.fill_menus()

    def save_settings(self):
        atomic_json(CONFIG_FILE, self.settings)

    def remember_recent(self, path):
        path = str(path)
        self.settings["recent"] = [path] + [p for p in self.settings["recent"] if p != path][:11]
        self.save_settings()
        self.fill_menus()

    def apply_theme(self):
        p = self.palette
        self.root.configure(bg=p["chrome"])
        for widget in (self.outer, self.menu_row, self.findpanel, self.find_row,
                       self.replace_row, self.markdown_row, self.status):
            widget.configure(bg=p["chrome"])
        for widget in (self.top, self.appicon, self.tabstrip, self.gear):
            widget.configure(bg=p["tabs"])
        self.gear.configure(fg=p["muted"])
        for button in self.menu_buttons.values():
            button.configure(bg=p["chrome"], fg=p["fg"],
                             activebackground=p["hover"], activeforeground=p["fg"])
        for menu in self.menus.values():
            menu.configure(bg=p["active"], fg=p["fg"], activebackground=p["hover"],
                           activeforeground=p["fg"], bd=1, relief="flat")
        self.editor_container.configure(bg=p["editor"])
        self.status.configure(highlightbackground=p["border"])
        for part in self.status_parts:
            part.configure(bg=p["chrome"], fg=p["muted"])
        self.results.configure(bg=p["chrome"], fg=p["muted"])
        for option in (self.case_button, self.word_button, self.wrap_button):
            option.configure(bg=p["chrome"], fg=p["fg"],
                             activebackground=p["chrome"],
                             activeforeground=p["fg"], selectcolor=p["input"])
        for parent in (self.find_row, self.replace_row, self.markdown_row):
            for child in parent.winfo_children():
                if isinstance(child, tk.Button):
                    child.configure(bg=p["button"], fg=p["fg"],
                                    activebackground=p["hover"], activeforeground=p["fg"])
        for entry in (self.search_entry, self.replace_entry):
            entry.configure(bg=p["input"], fg=p["fg"], insertbackground=p["fg"],
                            highlightbackground=p["border"], highlightcolor=p["accent"])
        if self.frameless:
            for b in (self.close_button, self.max_button, self.min_button):
                b.configure(bg=p["tabs"], fg=p["fg"])
        for doc in self.documents:
            doc.panel.configure(bg=p["editor"])
            doc.text.configure(bg=p["editor"], fg=p["fg"],
                               insertbackground=p["fg"], selectbackground=p["selection"],
                               selectforeground=p["fg"], font=self.editor_font)
            doc.text.tag_configure("found", background=p["found"])
            for child in doc.panel.winfo_children():
                if isinstance(child, tk.Scrollbar):
                    child.configure(bg=p["chrome"], troughcolor=p["editor"],
                                    activebackground=p["hover"])
        self.tabstrip.render()
        self.update_status()

    def set_status(self):
        self.settings["status_bar"] = bool(self.status_var.get())
        if self.status_var.get():
            self.status.pack(side="bottom", fill="x")
        else:
            self.status.pack_forget()
        self.save_settings()

    def set_wrap(self):
        active = bool(self.wrap_var.get())
        self.settings["word_wrap"] = active
        for doc in self.documents:
            doc.text.configure(wrap="word" if active else "none")
            if active:
                doc.horizontal.pack_forget()
            else:
                doc.horizontal.pack(side="bottom", fill="x")
        self.save_settings()

    def make_document(self, title=None):
        p = self.palette
        panel = tk.Frame(self.editor_container, bg=p["editor"])
        horizontal = tk.Scrollbar(panel, orient="horizontal")
        if not self.wrap_var.get():
            horizontal.pack(side="bottom", fill="x")
        scroller = tk.Scrollbar(panel, orient="vertical")
        scroller.pack(side="right", fill="y")
        editor = tk.Text(panel, bd=0, relief="flat", wrap="word" if self.wrap_var.get() else "none",
                         undo=True, maxundo=-1, autoseparators=True,
                         font=self.editor_font, padx=14, pady=12, spacing1=1,
                         highlightthickness=0, bg=p["editor"], fg=p["fg"],
                         insertbackground=p["fg"], insertwidth=2,
                         selectbackground=p["selection"], selectforeground=p["fg"],
                         yscrollcommand=scroller.set, xscrollcommand=horizontal.set,
                         exportselection=False)
        editor.pack(side="left", fill="both", expand=True)
        scroller.config(command=editor.yview)
        horizontal.config(command=editor.xview)
        self.untitled_counter += 1
        doc = Document(editor, panel, untitled_name=title or
                       ("Untitled" if self.untitled_counter == 1 else f"Untitled {self.untitled_counter}"))
        editor.bind("<<Modified>>", lambda _, d=doc: self.on_modified(d))
        editor.bind("<KeyRelease>", lambda _, d=doc: self.on_caret(d))
        editor.bind("<ButtonRelease-1>", lambda _, d=doc: self.on_caret(d))
        editor.bind("<FocusIn>", lambda _, d=doc: self.on_caret(d))
        editor.bind("<Control-MouseWheel>", self.control_zoom)
        editor.bind("<Button-3>", lambda event, d=doc: self.context_menu(event, d))
        editor.bind("<Control-BackSpace>", lambda event, d=doc: self.delete_word_back(d))
        editor.tag_configure("found", background=p["found"])
        doc.horizontal = horizontal
        self.documents.append(doc)
        self.set_current(len(self.documents) - 1)
        return doc

    def new_document(self):
        self.make_document()
        self.schedule_session()

    def set_current(self, i):
        if not 0 <= i < len(self.documents):
            return
        for document in self.documents:
            document.panel.pack_forget()
        self.current = i
        doc = self.documents[i]
        doc.panel.pack(side="top", fill="both", expand=True)
        self.root.title(f"{doc.title()} - {APP_NAME}")
        self.tabstrip.render()
        self.update_status()
        doc.text.focus_set()
        if self.find_visible:
            self.highlight_matches()
        self.schedule_session()

    def move_tab(self, source, dest):
        if not (0 <= source < len(self.documents) and 0 <= dest < len(self.documents)):
            return
        doc = self.documents.pop(source)
        self.documents.insert(dest, doc)
        self.current = -1
        self.set_current(dest)
        self.schedule_session()

    def current_doc(self):
        if 0 <= self.current < len(self.documents):
            return self.documents[self.current]
        return None

    def on_modified(self, doc):
        if not doc.text.edit_modified():
            return
        doc.text.edit_modified(False)
        if doc.suppress:
            return
        doc.dirty = doc.metadata_dirty or doc.value() != doc.saved_text
        if doc == self.current_doc():
            self.root.title(f"{doc.title()} - {APP_NAME}")
            self.update_status()
            if self.find_visible:
                self.highlight_matches()
        self.tabstrip.render()
        self.schedule_session()

    def on_caret(self, doc):
        if doc == self.current_doc():
            self.update_status()

    def initial_documents(self, paths):
        self.startup_id = None
        if self.documents:
            return
        if paths:
            for item in paths:
                self.open_path(Path(item))
            if not self.documents:
                self.new_document()
        elif not self.separate_window and self.settings.get("restore_session", True) and self.restore_session():
            pass
        else:
            self.new_document()
        self.set_status()

    def load_into(self, doc, content, *, path=None, encoding="utf-8", eol="\n",
                  saved_text=None, dirty=False):
        doc.suppress = True
        doc.text.delete("1.0", "end")
        doc.text.insert("1.0", content)
        doc.text.edit_reset()
        doc.text.edit_modified(False)
        doc.suppress = False
        doc.path = path
        doc.encoding = encoding
        doc.eol = eol
        doc.saved_text = content if saved_text is None else saved_text
        doc.dirty = dirty or (content != doc.saved_text)
        doc.metadata_dirty = bool(dirty and content == doc.saved_text)
        self.root.title(f"{doc.title()} - {APP_NAME}")
        self.tabstrip.render()
        self.update_status()

    def open_path(self, path: Path):
        try:
            path = path.expanduser().resolve()
            for i, doc in enumerate(self.documents):
                if doc.path == path:
                    self.set_current(i)
                    return True
            data = path.read_bytes()
            content, encoding, eol = detect_text(data)
        except (OSError, UnicodeError, ValueError) as error:
            messagebox.showerror("Open file", f"Couldn't open {path}:\n\n{error}", parent=self.root)
            return False
        doc = self.make_document()
        self.load_into(doc, content, path=path, encoding=encoding, eol=eol)
        self.remember_recent(path)
        self.schedule_session()
        return True

    def open_dialog(self):
        names = filedialog.askopenfilenames(parent=self.root, title="Open",
                filetypes=[("Text documents", "*.txt *.md *.log *.csv *.json *.py *.sh"),
                           ("All files", "*")])
        for name in names:
            self.open_path(Path(name))

    def save(self):
        doc = self.current_doc()
        if doc is None:
            return False
        if doc.path is None:
            return self.save_as()
        try:
            atomic_bytes(doc.path, encoded_text(doc.value(), doc.encoding, doc.eol))
        except (OSError, UnicodeError) as error:
            messagebox.showerror("Save file", f"Couldn't save {doc.path}:\n\n{error}", parent=self.root)
            return False
        doc.saved_text = doc.value()
        doc.dirty = False
        doc.metadata_dirty = False
        self.remember_recent(doc.path)
        self.tabstrip.render()
        self.update_status()
        self.schedule_session()
        return True

    def save_as(self):
        doc = self.current_doc()
        if doc is None:
            return False
        name = filedialog.asksaveasfilename(parent=self.root,
                    title="Save as", initialfile=doc.path.name if doc.path else "Untitled.txt",
                    defaultextension=".txt",
                    filetypes=[("Text documents", "*.txt"), ("All files", "*")])
        if not name:
            return False
        target = Path(name).expanduser().resolve()
        if any(d is not doc and d.path == target for d in self.documents):
            messagebox.showerror("Save as", "This file is already open in another tab.", parent=self.root)
            return False
        original_path = doc.path
        doc.path = target
        if not self.save():
            doc.path = original_path
            return False
        self.root.title(f"{doc.title()} - {APP_NAME}")
        return True

    def print_document(self):
        """Print plain text using CUPS, if a Linux print command is present."""
        command = shutil.which("lp") or shutil.which("lpr")
        if not command:
            messagebox.showinfo("Print", "Printing requires CUPS (the 'lp' or 'lpr' command).\n"
                                "Install CUPS using your Linux package manager and configure a printer.",
                                parent=self.root)
            return
        doc = self.current_doc()
        if not doc:
            return
        if not messagebox.askyesno("Print", "Print this document to your default printer?",
                                   parent=self.root):
            return
        fd, name = tempfile.mkstemp(prefix="openpad-print-", suffix=".txt")
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(doc.value().encode("utf-8"))
            os.chmod(name, 0o600)
            subprocess.run([command, name], capture_output=True, text=True,
                           check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            messagebox.showerror("Print", f"Unable to print:\n\n{error}", parent=self.root)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass

    def line_ending_menu(self, event):
        doc = self.current_doc()
        if not doc:
            return
        menu = tk.Menu(self.root, tearoff=False)
        for name, value in (("Windows (CRLF)", "\r\n"),
                            ("Unix (LF)", "\n"),
                            ("Macintosh (CR)", "\r")):
            menu.add_command(label=("✓  " if value == doc.eol else "    ") + name,
                command=lambda ending=value: self.set_eol(ending))
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()

    def set_eol(self, ending):
        doc = self.current_doc()
        if doc and doc.eol != ending:
            doc.eol = ending
            doc.dirty = True
            doc.metadata_dirty = True
            self.update_status()
            self.tabstrip.render()
            self.schedule_session()

    def encoding_menu(self, event):
        doc = self.current_doc()
        if not doc:
            return
        menu = tk.Menu(self.root, tearoff=False)
        for name, encoding in (("UTF-8", "utf-8"), ("UTF-8 with BOM", "utf-8-sig"),
                               ("UTF-16", "utf-16")):
            menu.add_command(label=("✓  " if encoding == doc.encoding else "    ") + name,
                command=lambda value=encoding: self.set_encoding(value))
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()

    def set_encoding(self, encoding):
        doc = self.current_doc()
        if doc and doc.encoding != encoding:
            doc.encoding = encoding
            doc.dirty = True
            doc.metadata_dirty = True
            self.update_status()
            self.tabstrip.render()
            self.schedule_session()

    def save_all(self):
        prior = self.current
        for i in range(len(self.documents)):
            self.set_current(i)
            if self.documents[i].dirty or self.documents[i].path is None:
                if not self.save():
                    self.set_current(min(prior, len(self.documents) - 1))
                    return False
        self.set_current(min(prior, len(self.documents) - 1))
        return True

    def close_tab(self, index=None):
        if index is None:
            index = self.current
        if not 0 <= index < len(self.documents):
            return
        doc = self.documents[index]
        if doc.dirty:
            choice = messagebox.askyesnocancel("Unsaved changes",
                 f"Save changes to {doc.title()} before closing?", parent=self.root)
            if choice is None:
                return
            if choice:
                self.set_current(index)
                if not self.save():
                    return
        old_current = self.current
        self.closed_documents.append({"path": doc.path, "content": doc.value(),
                                      "encoding": doc.encoding, "eol": doc.eol,
                                      "saved_text": doc.saved_text,
                                      "dirty": doc.dirty, "title": doc.untitled_name})
        self.closed_documents = self.closed_documents[-20:]
        doc.panel.destroy()
        self.documents.pop(index)
        if not self.documents:
            self.current = -1
            self.new_document()
        else:
            new_current = (min(index, len(self.documents)-1) if old_current == index
                           else old_current - (1 if index < old_current else 0))
            self.current = -1
            self.set_current(new_current)
        self.schedule_session()

    def reopen_closed_tab(self):
        if not self.closed_documents:
            return
        item = self.closed_documents.pop()
        # Closing an otherwise blank new tab is harmless; reopening returns
        # the original tab in the same window, including unsaved text.
        doc = self.make_document(title=item["title"])
        self.load_into(doc, item["content"], path=item["path"],
                       encoding=item["encoding"], eol=item["eol"],
                       saved_text=item["saved_text"], dirty=item["dirty"])
        self.schedule_session()

    def new_window(self):
        args = [sys.executable, str(Path(__file__).resolve()), "--new-window"]
        if self.frameless:
            args.append("--frameless")
        subprocess.Popen(args, start_new_session=True)

    def quit(self):
        if self.startup_id is not None:
            self.root.after_cancel(self.startup_id)
            self.startup_id = None
        # Just like new Notepad: unsaved documents are recovered at next launch.
        if self.settings.get("restore_session", True) and not self.separate_window:
            self.persist_session()
        else:
            for i, doc in enumerate(self.documents):
                if doc.dirty:
                    self.set_current(i)
                    answer = messagebox.askyesnocancel("Unsaved changes",
                          f"Save changes to {doc.title()} before exiting?", parent=self.root)
                    if answer is None or (answer and not self.save()):
                        return
            if not self.separate_window:
                try:
                    SESSION_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
        self.save_settings()
        self.root.destroy()

    def schedule_session(self):
        if self.autosave_id:
            self.root.after_cancel(self.autosave_id)
        if self.settings.get("restore_session", True) and not self.separate_window:
            self.autosave_id = self.root.after(800, self.persist_session)

    def persist_session(self):
        self.autosave_id = None
        if not self.settings.get("restore_session", True) or self.separate_window:
            return
        data = {"version": 1, "active": self.current, "documents": []}
        for doc in self.documents:
            data["documents"].append({
                "path": str(doc.path) if doc.path else None,
                "content": doc.value(), "encoding": doc.encoding, "eol": doc.eol,
                "saved_text": doc.saved_text if doc.dirty else None,
                "title": doc.untitled_name, "dirty": doc.dirty,
                "cursor": doc.text.index("insert"),
            })
        try:
            atomic_json(SESSION_FILE, data)
        except OSError as error:
            print(f"OpenPad: couldn't save session: {error}", file=sys.stderr)

    def restore_session(self):
        data = load_json(SESSION_FILE, {})
        if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
            return False
        for item in data["documents"]:
            if not isinstance(item, dict):
                continue
            try:
                content = item.get("content", "")
                if not isinstance(content, str):
                    continue
                path = Path(item["path"]) if item.get("path") else None
                if path and not item.get("dirty", False) and path.exists():
                    try:
                        content, encoding_on_disk, eol_on_disk = detect_text(path.read_bytes())
                        item = {**item, "encoding": encoding_on_disk, "eol": eol_on_disk}
                    except (OSError, UnicodeError, ValueError):
                        pass
                doc = self.make_document(title=item.get("title", "Untitled"))
                saved_text = item.get("saved_text")
                self.load_into(doc, content, path=path,
                               encoding=item.get("encoding", "utf-8"),
                               eol=item.get("eol", "\n"),
                               saved_text=saved_text if isinstance(saved_text, str) else content,
                               dirty=bool(item.get("dirty", False)))
                doc.text.mark_set("insert", item.get("cursor", "1.0"))
            except (tk.TclError, ValueError, TypeError):
                continue
        if self.documents:
            self.set_current(max(0, min(int(data.get("active", 0)), len(self.documents) - 1)))
            return True
        return False

    def current_text(self):
        doc = self.current_doc()
        return doc.text if doc else None

    def text_action(self, method):
        editor = self.current_text()
        if editor:
            try:
                getattr(editor, method)()
            except tk.TclError:
                pass

    def text_event(self, sequence):
        editor = self.current_text()
        if editor:
            editor.event_generate(sequence)

    def delete_selected(self):
        editor = self.current_text()
        if editor:
            try:
                editor.delete("sel.first", "sel.last")
            except tk.TclError:
                pass

    def insert_datetime(self):
        editor = self.current_text()
        if editor:
            editor.insert("insert", datetime.now().strftime("%X %x"))

    def delete_word_back(self, doc):
        text = doc.text
        start = text.index("insert")
        prefix = text.get("1.0", start)
        match = re.search(r"(?:\w+|[^\w\s]+|\s+)$", prefix)
        if match:
            text.delete(f"{start}-{len(match.group())}c", start)
        return "break"

    def insert_line(self, below=True):
        editor = self.current_text()
        if not editor:
            return
        line = editor.index("insert").split(".")[0]
        if below:
            editor.insert(f"{line}.end", "\n")
            editor.mark_set("insert", f"{int(line)+1}.0")
        else:
            editor.insert(f"{line}.0", "\n")
            editor.mark_set("insert", f"{line}.0")
        editor.see("insert")

    def duplicate_line(self):
        editor = self.current_text()
        if not editor:
            return
        line = editor.index("insert").split(".")[0]
        content = editor.get(f"{line}.0", f"{line}.end")
        editor.insert(f"{line}.end", "\n" + content)
        editor.mark_set("insert", f"{int(line)+1}.0")

    def set_markdown_toolbar(self):
        visible = bool(self.markdown_var.get())
        self.settings["markdown_toolbar"] = visible
        if visible:
            self.markdown_row.pack(side="top", fill="x", pady=2,
                                   before=self.editor_container)
        else:
            self.markdown_row.pack_forget()
        self.save_settings()

    def markdown_inline(self, marker):
        editor = self.current_text()
        if not editor:
            return
        try:
            value = editor.get("sel.first", "sel.last")
            editor.replace("sel.first", "sel.last", marker + value + marker)
        except tk.TclError:
            editor.insert("insert", marker + marker)
            editor.mark_set("insert", f"insert-{len(marker)}c")
        editor.focus_set()

    def markdown_prefix(self, prefix):
        editor = self.current_text()
        if not editor:
            return
        try:
            start = int(editor.index("sel.first").split(".")[0])
            end = int(editor.index("sel.last").split(".")[0])
        except tk.TclError:
            start = end = int(editor.index("insert").split(".")[0])
        for line in range(end, start-1, -1):
            editor.insert(f"{line}.0", prefix)
        editor.focus_set()

    def markdown_heading(self):
        self.markdown_prefix("# ")

    def markdown_link(self):
        editor = self.current_text()
        if not editor:
            return
        try:
            label = editor.get("sel.first", "sel.last")
            editor.replace("sel.first", "sel.last", f"[{label}](https://)")
        except tk.TclError:
            editor.insert("insert", "[text](https://)")
        editor.focus_set()

    def markdown_table(self):
        editor = self.current_text()
        if editor:
            editor.insert("insert", "| Column 1 | Column 2 |\n| --- | --- |\n|  |  |\n")
            editor.focus_set()

    def context_menu(self, event, doc):
        menu = tk.Menu(self.root, tearoff=False)
        for label, action in [
            ("Undo", lambda: self.text_action("edit_undo")),
            ("Redo", lambda: self.text_action("edit_redo")),
            ("Cut", lambda: self.text_event("<<Cut>>")),
            ("Copy", lambda: self.text_event("<<Copy>>")),
            ("Paste", lambda: self.text_event("<<Paste>>")),
            ("Select all", lambda: self.text_event("<<SelectAll>>")),
        ]:
            menu.add_command(label=label, command=action)
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()

    def go_to(self):
        doc = self.current_doc()
        if not doc:
            return
        count = int(doc.text.index("end-1c").split(".")[0])
        line = simpledialog.askinteger("Go to line", "Line number:", parent=self.root,
                             initialvalue=int(doc.text.index("insert").split(".")[0]),
                             minvalue=1, maxvalue=max(1, count))
        if line:
            doc.text.mark_set("insert", f"{line}.0")
            doc.text.see("insert")
            doc.text.focus_set()
            self.update_status()

    def show_find(self, replace=False):
        self.find_visible = True
        self.replace_visible = replace
        if not self.findpanel.winfo_manager():
            self.findpanel.pack(side="top", fill="x", before=self.editor_container)
        if replace:
            self.replace_row.pack(fill="x", padx=10, pady=(0, 7))
        else:
            self.replace_row.pack_forget()
        doc = self.current_doc()
        if doc:
            try:
                selected = doc.text.get("sel.first", "sel.last")
                if selected and "\n" not in selected and len(selected) <= 200:
                    self.search_entry.delete(0, "end")
                    self.search_entry.insert(0, selected)
            except tk.TclError:
                pass
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")
        self.highlight_matches()

    def hide_find(self):
        self.find_visible = False
        self.findpanel.pack_forget()
        doc = self.current_doc()
        if doc:
            doc.text.tag_remove("found", "1.0", "end")
            doc.text.focus_set()

    def highlight_matches(self):
        doc = self.current_doc()
        if not doc:
            return
        editor = doc.text
        editor.tag_remove("found", "1.0", "end")
        search = self.search_entry.get()
        if not search or not self.find_visible:
            self.results.config(text="")
            return
        # Limit highlights for huge logs, so typing remains responsive.
        found = 0
        at = "1.0"
        while found < 1200:
            at = editor.search(search, at, "end", nocase=not self.search_case.get(),
                               regexp=False)
            if not at:
                break
            end = f"{at}+{len(search)}c"
            at = end
            if not self.word_var.get() or self.is_whole_word(editor, at, len(search)):
                editor.tag_add("found", f"{at}-{len(search)}c", end)
                found += 1
        self.results.config(text="No results" if found == 0 else
                            f"{found}{'+' if found == 1200 else ''} found")
        editor.tag_lower("found", "sel")

    def is_whole_word(self, editor, end, length):
        start = editor.index(f"{end}-{length}c")
        before = editor.get(f"{start}-1c", start) if editor.compare(start, ">", "1.0") else ""
        after = editor.get(end, f"{end}+1c") if editor.compare(end, "<", "end-1c") else ""
        return not (before and (before.isalnum() or before == "_")) and not (
            after and (after.isalnum() or after == "_"))

    def update_search_settings(self):
        self.settings["find_wrap"] = bool(self.search_wrap_var.get())
        self.settings["find_whole_word"] = bool(self.word_var.get())
        self.save_settings()
        self.highlight_matches()

    def search_range(self, editor, term, start, stop, previous):
        pos = start
        while True:
            result = editor.search(term, pos, stop, backwards=previous,
                                   nocase=not self.search_case.get(), regexp=False)
            if not result:
                return ""
            end = editor.index(f"{result}+{len(term)}c")
            if not self.word_var.get() or self.is_whole_word(editor, end, len(term)):
                return result
            pos = result if previous else end
            if not previous and editor.compare(pos, ">=", stop):
                return ""
            if previous and editor.compare(pos, "<=", stop):
                return ""

    def find_next(self, previous=False):
        doc = self.current_doc()
        if not doc:
            return
        editor = doc.text
        term = self.search_entry.get()
        if not term:
            self.show_find()
            return
        if not self.find_visible:
            self.show_find()
        selected = editor.tag_ranges("sel")
        start = editor.index("sel.first" if previous else "sel.last") if selected else editor.index("insert")
        result = self.search_range(editor, term, start, "1.0" if previous else "end", previous)
        if not result and self.search_wrap_var.get():
            result = self.search_range(editor, term, "end-1c" if previous else "1.0",
                                       start, previous)
        if result:
            editor.tag_remove("sel", "1.0", "end")
            editor.tag_add("sel", result, f"{result}+{len(term)}c")
            editor.mark_set("insert", f"{result}+{len(term)}c")
            editor.see(result)
            editor.focus_set()
        else:
            self.results.config(text="No results")

    def replace_one(self):
        editor = self.current_text()
        if not editor:
            return
        term = self.search_entry.get()
        if not term:
            return
        try:
            selected = editor.get("sel.first", "sel.last")
            same = selected == term if self.search_case.get() else selected.casefold() == term.casefold()
        except tk.TclError:
            same = False
        if not same:
            self.find_next()
            return
        editor.replace("sel.first", "sel.last", self.replace_entry.get())
        self.highlight_matches()
        self.find_next()

    def replace_all(self):
        editor = self.current_text()
        term = self.search_entry.get()
        if not editor or not term:
            return
        entire = editor.get("1.0", "end-1c")
        flags = 0 if self.search_case.get() else re.IGNORECASE
        replacement = self.replace_entry.get()
        pattern = re.escape(term)
        if self.word_var.get():
            pattern = r"(?<!\w)" + pattern + r"(?!\w)"
        updated, count = re.subn(pattern, lambda _: replacement, entire, flags=flags)
        if count:
            editor.edit_separator()
            editor.replace("1.0", "end-1c", updated)
            editor.edit_separator()
        self.highlight_matches()
        self.results.configure(text=f"Replaced {count}")

    def zoom(self, change):
        new = max(7, min(32, int(self.editor_font.cget("size")) + change))
        self.editor_font.configure(size=new)
        self.update_status()

    def reset_zoom(self):
        self.editor_font.configure(size=int(self.settings["font_size"]))
        self.update_status()

    def control_zoom(self, event):
        self.zoom(1 if event.delta > 0 else -1)
        return "break"

    def update_status(self):
        doc = self.current_doc()
        if not doc:
            return
        editor = doc.text
        try:
            line, col = map(int, editor.index("insert").split("."))
            length = len(doc.value())
            percentage = int(round(100 * int(self.editor_font.cget("size")) /
                                   max(1, int(self.settings["font_size"]))))
            self.status_parts[0].config(text=f"Ln {line}, Col {col + 1}")
            self.status_parts[1].config(text=f"{length:,} characters")
            self.status_parts[2].config(text=f"{percentage}%")
            ending = {"\r\n": "Windows (CRLF)", "\n": "Unix (LF)", "\r": "Macintosh (CR)"}
            self.status_parts[3].config(text=ending.get(doc.eol, "Unix (LF)"))
            enc = {"utf-8": "UTF-8", "utf-8-sig": "UTF-8 BOM", "utf-16": "UTF-16"}
            self.status_parts[4].config(text=enc.get(doc.encoding, doc.encoding.upper()))
        except (tk.TclError, ValueError):
            pass

    def show_settings(self):
        p = self.palette
        window = tk.Toplevel(self.root)
        window.title("Settings — OpenPad")
        window.geometry("415x412")
        window.resizable(False, False)
        window.configure(bg=p["active"])
        window.transient(self.root)
        container = tk.Frame(window, bg=p["active"], padx=24, pady=17)
        container.pack(fill="both", expand=True)
        tk.Label(container, text="Settings", font=(self.ui_family, 20, "bold"),
                 bg=p["active"], fg=p["fg"]).pack(anchor="w", pady=(0, 20))
        def label(text):
            tk.Label(container, text=text, font=self.ui_font, bg=p["active"],
                     fg=p["fg"]).pack(anchor="w", pady=(9, 3))
        label("App theme")
        theme_var = tk.StringVar(value=self.settings["theme"])
        theme = tk.OptionMenu(container, theme_var, "light", "dark")
        theme.pack(anchor="w", fill="x")
        label("Font")
        font_var = tk.StringVar(value=self.editor_font.cget("family"))
        fonts = ["Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Liberation Mono", "Courier New"]
        families = set(tkfont.families(self.root))
        fonts = [x for x in fonts if x in families]
        if font_var.get() not in fonts:
            fonts.insert(0, font_var.get())
        chooser = tk.OptionMenu(container, font_var, *fonts)
        chooser.pack(anchor="w", fill="x")
        label("Font size")
        size_var = tk.IntVar(value=int(self.settings["font_size"]))
        size = tk.Spinbox(container, from_=7, to=32, textvariable=size_var, width=6)
        size.pack(anchor="w")
        restore_var = tk.BooleanVar(value=self.settings["restore_session"])
        restore = tk.Checkbutton(container, text="Open previous tabs and unsaved notes on startup",
                       variable=restore_var, bg=p["active"], fg=p["fg"],
                       activebackground=p["active"], activeforeground=p["fg"],
                       selectcolor=p["input"], font=self.ui_font)
        restore.pack(anchor="w", pady=(14, 0))
        native_var = tk.BooleanVar(value=not self.frameless)
        native = tk.Checkbutton(container, text="Use native Linux window frame (restart required)",
                       variable=native_var, bg=p["active"], fg=p["fg"],
                       activebackground=p["active"], activeforeground=p["fg"],
                       selectcolor=p["input"], font=self.ui_font)
        native.pack(anchor="w", pady=(3, 0))
        def apply():
            self.settings["theme"] = theme_var.get()
            self.settings["font_family"] = font_var.get()
            self.settings["font_size"] = max(7, min(32, int(size_var.get())))
            self.settings["restore_session"] = bool(restore_var.get())
            self.settings["native_titlebar"] = bool(native_var.get())
            self.palette = LIGHT if theme_var.get() == "light" else DARK
            self.editor_font.configure(family=font_var.get(), size=self.settings["font_size"])
            self.apply_theme()
            self.save_settings()
            self.set_status()
            window.destroy()
        self.make_button(container, "Apply", apply, side="right")
        window.bind("<Escape>", lambda _: window.destroy())

    def setup_shortcuts(self):
        keys = {
            "<Control-n>": self.new_document,
            "<Control-N>": self.new_window,
            "<Control-Shift-t>": self.reopen_closed_tab,
            "<Control-Shift-T>": self.reopen_closed_tab,
            "<Control-o>": self.open_dialog,
            "<Control-s>": self.save,
            "<Control-p>": self.print_document,
            "<Control-S>": self.save_as,
            "<Control-Alt-s>": self.save_all,
            "<Control-w>": self.close_tab,
            "<Control-f>": self.show_find,
            "<Control-h>": lambda: self.show_find(True),
            "<Alt-f>": lambda: self.popup_menu("File"),
            "<Alt-e>": lambda: self.popup_menu("Edit"),
            "<Alt-v>": lambda: self.popup_menu("View"),
            "<Control-Return>": lambda: self.insert_line(True),
            "<Control-Shift-Return>": lambda: self.insert_line(False),
            "<Control-d>": self.duplicate_line,
            "<Control-y>": lambda: self.text_action("edit_redo"),
            "<Control-Shift-z>": lambda: self.text_action("edit_redo"),
            "<Control-Shift-Z>": lambda: self.text_action("edit_redo"),
            "<Control-g>": self.go_to,
            "<F3>": self.find_next,
            "<Shift-F3>": lambda: self.find_next(True),
            "<F5>": self.insert_datetime,
            "<Control-Tab>": lambda: self.cycle_tab(1),
            "<Control-Shift-Tab>": lambda: self.cycle_tab(-1),
            "<Control-plus>": lambda: self.zoom(1),
            "<Control-equal>": lambda: self.zoom(1),
            "<Control-minus>": lambda: self.zoom(-1),
            "<Control-0>": self.reset_zoom,
            "<Escape>": self.escape,
        }
        for shortcut, action in keys.items():
            def handler(event, callback=action, key=shortcut):
                # TK may deliver Ctrl+Shift+N both as n and N; use exact key symbol.
                if key == "<Control-n>" and (event.state & 0x1):
                    return None
                callback()
                return "break"
            self.root.bind_all(shortcut, handler, add="+")
        # Intercept root focused global undo while leaving native text control shortcuts alone.

    def cycle_tab(self, move):
        if self.documents:
            self.set_current((self.current + move) % len(self.documents))

    def escape(self):
        if self.find_visible:
            self.hide_find()

    def on_root_configure(self, event):
        if event.widget == self.root and self.documents:
            self.tabstrip.render()

    def drag_start(self, event):
        if self.frameless:
            self._drag_x = event.x_root - self.root.winfo_x()
            self._drag_y = event.y_root - self.root.winfo_y()

    def drag_move(self, event):
        if self.frameless and not self.maximized:
            x = event.x_root - self._drag_x
            y = event.y_root - self._drag_y
            self.root.geometry(f"+{x}+{y}")

    def toggle_maximize(self):
        if not self.frameless:
            return
        if self.maximized:
            self.root.geometry(self.restore_geometry)
        else:
            self.restore_geometry = self.root.geometry()
            self.root.geometry(f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
        self.maximized = not self.maximized

    def minimize(self):
        if self.frameless:
            self.root.overrideredirect(False)
            self.root.iconify()
            self.root.bind("<Map>", self.on_restore, add="+")
        else:
            self.root.iconify()

    def on_restore(self, event):
        if event.widget == self.root and self.frameless:
            self.root.after(30, lambda: self.root.overrideredirect(True))
            self.root.unbind("<Map>")

    def run(self):
        self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="OpenPad: a minimal Notepad-style editor for Linux")
    parser.add_argument("files", nargs="*", help="Text files to open")
    parser.add_argument("--new-window", action="store_true", help="Skip restoring prior tabs")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--frameless", action="store_true", help="Draw Windows-style window controls")
    group.add_argument("--native-frame", action="store_true", help="Use your Linux desktop's window frame")
    args = parser.parse_args(argv)
    prefs = load_json(CONFIG_FILE, {})
    frameless = args.frameless or (not args.native_frame and isinstance(prefs, dict)
                                   and not prefs.get("native_titlebar", True))
    app = OpenPad(args.files, frameless=frameless)
    if args.new_window:
        app.separate_window = True
    app.run()


if __name__ == "__main__":
    main()
