"""Shared native widgets. Message text stays selectable and never executes HTML."""
from __future__ import annotations

import re
import math
import tkinter as tk
from tkinter import font, ttk

from .ui_surfaces import RoundedPanel, RoundedButton, RoundedEntry, RoundedLabel, RoundedTreeview

BG = "#f5f8f6"
WHITE = "#ffffff"
INK = "#1c342b"
MUTED = "#536b61"
LINE = "#dce6df"
TEAL = "#176b52"
SOFT = "#e7f3ed"
NAV = "#edf3ef"
NAV_HOVER = "#dcece3"
NAV_MUTED = "#526a5e"
ERROR = "#a13e38"


def _tab_image(root, fill, border=None):
    # Native ttk tabs use a stretchable rounded image, preserving tab semantics.
    size, radius = 36, 10
    image = tk.PhotoImage(master=root, width=size, height=size)
    def rgb(color): return tuple(int(color[i:i+2], 16) for i in (1, 3, 5))
    def inside(x, y, inset=0):
        edge = radius - inset
        cx = min(max(x, radius), size - radius)
        cy = min(max(y, radius), size - radius)
        return inset <= x <= size - inset and inset <= y <= size - inset and (x - cx) ** 2 + (y - cy) ** 2 <= edge ** 2
    background, foreground, stroke = rgb(BG), rgb(fill), rgb(border or fill)
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            samples = []
            for dx, dy in ((.25, .25), (.75, .25), (.25, .75), (.75, .75)):
                samples.append(foreground if inside(x+dx, y+dy, 1.5 if border else 0) else stroke if inside(x+dx, y+dy) else background)
            row.append("#" + "".join(f"{round(sum(c[k] for c in samples)/4):02x}" for k in range(3)))
        rows.append("{" + " ".join(row) + "}")
    image.put(" ".join(rows))
    return image


def setup_theme(root):
    families = {x.lower(): x for x in font.families(root)}
    family = next((families[x.lower()] for x in ("Yu Gothic UI", "Noto Sans CJK JP", "Meiryo", "Arial", "Helvetica") if x.lower() in families), font.nametofont("TkDefaultFont").actual("family"))
    mono = next((families[x.lower()] for x in ("Consolas", "Noto Sans Mono CJK JP", "DejaVu Sans Mono", "Courier") if x.lower() in families), family)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        font.nametofont(name).configure(family=family, size=10)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TScrollbar", background="#c8d8cf", troughcolor=BG, borderwidth=0, arrowsize=11)
    style.configure("TProgressbar", background=TEAL, troughcolor=SOFT, borderwidth=0)
    style.configure("Treeview", font=(family, 10), rowheight=max(36, font.Font(family=family, size=10).metrics("linespace") + 12), background=WHITE, fieldbackground=WHITE, foreground=INK, borderwidth=0, relief="flat", lightcolor=WHITE, darkcolor=WHITE, bordercolor=WHITE)
    style.configure("Treeview.Heading", font=(family, 9, "bold"), background=WHITE, foreground=MUTED, borderwidth=0, relief="flat", padding=(10, 10))
    style.map("Treeview", background=[("selected", SOFT)], foreground=[("selected", TEAL)])
    style.configure("TNotebook", background=BG, borderwidth=0, bordercolor=BG, lightcolor=BG, darkcolor=BG, relief="flat", tabmargins=(0, 0, 0, 10))
    style.configure("TNotebook.Tab", background=NAV, foreground=MUTED, font=(family, 9), padding=(12, 9), borderwidth=0)
    style.map("TNotebook.Tab", foreground=[("selected", TEAL), ("active", INK)], expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))])
    root._tab_images = [_tab_image(root, BG), _tab_image(root, SOFT), _tab_image(root, NAV_HOVER), _tab_image(root, SOFT, TEAL)]
    normal, selected, hover, focused = root._tab_images
    style.element_create("Rounded.tab", "image", normal, ("selected", "focus", focused), ("selected", selected), ("active", hover), border=10, sticky="nsew")
    style.layout("TNotebook.Tab", [("Rounded.tab", {"sticky": "nsew", "children": [("Notebook.padding", {"side": "top", "sticky": "nsew", "children": [("Notebook.label", {"side": "top", "sticky": ""})]})]})])
    style.configure("TCombobox", fieldbackground=WHITE, background=WHITE, foreground=INK, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, arrowsize=14, padding=8)
    style.map("TCombobox", fieldbackground=[("readonly", WHITE)], selectbackground=[("readonly", SOFT)], selectforeground=[("readonly", INK)])
    root.configure(bg=BG)
    return family, mono


def label(parent, text="", *, size=10, bold=False, fg=INK, bg=WHITE, **kwargs):
    return tk.Label(parent, text=text, font=(parent._root().ui_font, size, "bold" if bold else "normal"),
                    fg=fg, bg=bg, anchor="w", **kwargs)


def button(parent, text, command, *, primary=False, dark=False, **kwargs):
    fill = TEAL if primary else NAV if dark else WHITE
    foreground = WHITE if primary else INK
    return RoundedButton(parent, text, command, fill=fill, foreground=foreground,
        hover="#125c46" if primary else NAV_HOVER if dark else SOFT,
        pressed="#0e4e3a" if primary else "#d1e6da", border=None if primary or dark else LINE,
        focus=WHITE if primary else TEAL, disabled_fill="#eaf0ec", disabled_foreground="#687b70",
        font=(parent._root().ui_font, 10), **kwargs)


def entry(parent, variable=None, *, dark=False, **kwargs):
    return RoundedEntry(parent, variable, fill=WHITE, foreground=INK, border=LINE,
                        focus=TEAL, font=(parent._root().ui_font, 10), **kwargs)


def badge(parent, text="", *, fill=SOFT, foreground=TEAL, **kwargs):
    return RoundedLabel(parent, text, fill=fill, foreground=foreground,
                        font=(parent._root().ui_font, 9), **kwargs)


def treeview(parent, **kwargs):
    return RoundedTreeview(parent, fill=WHITE, border=LINE, **kwargs)


class ScrollFrame(tk.Frame):
    def __init__(self, parent, *, bg=BG, on_scroll=None):
        super().__init__(parent, bg=bg)
        self.follow = True
        self.on_scroll = on_scroll
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, borderwidth=0, yscrollincrement=24)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._scrolled)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._layout)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.bind_wheel(self.canvas)

    def _layout(self, _=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _scrolled(self, first, last):
        self.scrollbar.set(first, last)
        self.follow = float(last) >= 0.985
        if self.on_scroll:
            self.on_scroll(self.follow)

    def wheel(self, event):
        step = -1 if getattr(event, "num", 0) == 4 else 1 if getattr(event, "num", 0) == 5 else -int(event.delta / 120) if abs(event.delta) >= 120 else -event.delta
        self.canvas.yview_scroll(step, "units")
        return "break"

    def bind_wheel(self, widget):
        for name in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(name, self.wheel)
        for child in widget.winfo_children():
            self.bind_wheel(child)

    def bottom(self, *, force=False):
        if force or self.follow:
            self.after_idle(lambda: self.canvas.yview_moveto(1) if self.winfo_exists() else None)

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()


def insert_markdown(widget: tk.Text, text: str):
    """A deliberately small, native Markdown renderer; URLs are inert text."""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    code = False
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("```"):
            code = not code
            continue
        if code:
            widget.insert("end", line, "code")
            continue
        if not line.strip():
            widget.insert("end", "\n", "gap")
            continue
        heading = re.match(r"^(#{1,4})\s+(.+?)(\n?)$", line)
        if heading:
            widget.insert("end", heading[2] + "\n", "heading")
            continue
        if re.match(r"^\s*[-*]\s", line):
            line = re.sub(r"^(\s*)[-*]\s", r"\1•  ", line)
        for part in re.split(r"(\*\*.+?\*\*|`[^`\n]+`)", line):
            if part.startswith("**") and part.endswith("**"):
                widget.insert("end", part[2:-2], "strong")
            elif part.startswith("`") and part.endswith("`"):
                widget.insert("end", part[1:-1], "inline")
            else:
                widget.insert("end", part)
    widget.configure(state="disabled")


class MessageCard(RoundedPanel):
    def __init__(self, parent, *, role="assistant", text="", timestamp="", status="", on_copy=None):
        bg = SOFT if role == "user" else WHITE
        super().__init__(parent, fill=bg, border=None if role == "user" else LINE, radius=16, padding=(20, 18))
        self.pack(fill="x", padx=28, pady=(0, 14))
        self.bg = bg
        top = tk.Frame(self.content, bg=bg); top.pack(fill="x")
        label(top, "あなた" if role == "user" else "Work Assistant", bold=True, bg=bg).pack(side="left")
        label(top, timestamp, size=8, fg=MUTED, bg=bg).pack(side="left", padx=10)
        self.state_label = label(top, status, size=8, fg=TEAL, bg=bg); self.state_label.pack(side="right")
        self.body = tk.Text(self.content, wrap="word", width=1, height=2, relief="flat", borderwidth=0,
                            font=(self._root().ui_font, 11), bg=bg, fg=INK,
                            padx=0, pady=0, spacing1=0, spacing3=3, highlightthickness=0, selectbackground="#b8e4dc", cursor="xterm")
        self.body.tag_configure("strong", font=(self._root().ui_font, 11, "bold"))
        self.body.tag_configure("heading", font=(self._root().ui_font, 12, "bold"), spacing1=4, spacing3=4)
        self.body.tag_configure("gap", font=(self._root().ui_font, 4), spacing1=0, spacing3=1)
        self.body.tag_configure("code", font=(self._root().mono_font, 10), background="#e8edf2", lmargin1=10, lmargin2=10)
        self.body.tag_configure("inline", font=(self._root().mono_font, 10), foreground=TEAL)
        self.body.pack(fill="x", pady=(12, 0))
        self._height_job = None
        self.body.bind("<Configure>", self._resize)
        self.footer = tk.Frame(self.content, bg=bg); self.footer.pack(fill="x", pady=(8, 0))
        if on_copy:
            copy = button(self.footer, "コピー", lambda: on_copy(self.raw_text)); copy.configure(pady=4); copy.pack(side="right")
        self.expanded = False
        self.expand_button = button(self.footer, "全文を表示", self.toggle_expanded)
        self.set_text(text)

    def set_text(self, text):
        self.raw_text = text
        long = len(text) > 6000 or text.count("\n") > 100
        if long:
            self.expand_button.configure(text="折りたたむ" if self.expanded else "全文を表示")
            self.expand_button.pack(side="left")
        else:
            self.expand_button.pack_forget()
        preview = text if self.expanded or not long else "\n".join(text[:6000].splitlines()[:100]) + "\n…"
        insert_markdown(self.body, preview)
        self._resize()

    def toggle_expanded(self):
        self.expanded = not self.expanded
        self.set_text(self.raw_text)

    def _resize(self, _=None):
        if self._height_job:
            self.after_cancel(self._height_job)
        self._height_job = self.after_idle(self._fit)

    def _fit(self):
        self._height_job = None
        if self.winfo_exists():
            pixels = self.body.count("1.0", "end", "ypixels")
            line_height = font.Font(font=self.body["font"]).metrics("linespace")
            self.body.configure(height=max(2, min(1500, math.ceil((pixels[0] if pixels else line_height) / line_height))))

    def set_status(self, text):
        self.state_label.configure(text=text)

    def destroy(self):
        if self._height_job:
            self.after_cancel(self._height_job)
            self._height_job = None
        super().destroy()
