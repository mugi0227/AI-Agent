"""Rounded surfaces around native Tk controls; no external UI dependencies."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def rounded_shape(canvas, x, y, width, height, radius, color):
    radius = max(0, min(radius, width / 2, height / 2))
    right, bottom = x + width, y + height
    canvas.create_rectangle(x + radius, y, right - radius, bottom, fill=color, outline="")
    canvas.create_rectangle(x, y + radius, right, bottom - radius, fill=color, outline="")
    for left, top in ((x, y), (right - 2 * radius, y), (x, bottom - 2 * radius), (right - 2 * radius, bottom - 2 * radius)):
        canvas.create_oval(left, top, left + 2 * radius, top + 2 * radius, fill=color, outline="")


class RoundedPanel(tk.Frame):
    def __init__(self, parent, *, fill, border=None, radius=16, padding=16, outer=None, **kwargs):
        if outer is None:
            try: outer = parent.cget("bg")
            except tk.TclError: outer = parent._root().cget("bg")
        super().__init__(parent, bg=outer, **kwargs)
        self.fill, self.border, self.radius = fill, border, radius
        self.destroying = False
        self.backdrop = tk.Canvas(self, bg=outer, highlightthickness=0, borderwidth=0, takefocus=False)
        self.backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self.content = tk.Frame(self, bg=fill, borderwidth=0)
        horizontal, vertical = padding if isinstance(padding, tuple) else (padding, padding)
        self.content.pack(fill="both", expand=True, padx=horizontal, pady=vertical)
        self.bind("<Configure>", self.draw)

    def appearance(self, fill=None, border=None):
        if fill == self.fill and border == self.border: return
        if fill is not None: self.fill = fill
        self.border = border
        self.content.configure(bg=self.fill)
        self.draw()

    def draw(self, _=None):
        if not self.winfo_exists(): return
        width, height = self.winfo_width(), self.winfo_height()
        self.backdrop.delete("all")
        if self.border:
            rounded_shape(self.backdrop, 0, 0, width, height, self.radius, self.border)
            rounded_shape(self.backdrop, 2, 2, max(0, width - 4), max(0, height - 4), max(0, self.radius - 2), self.fill)
        else:
            rounded_shape(self.backdrop, 0, 0, width, height, self.radius, self.fill)

    def destroy(self):
        self.destroying = True
        super().destroy()


class SurfaceControl:
    """Expose the native control API while its surface participates in layout."""
    def pack(self, *args, **kwargs): return self.surface.pack(*args, **kwargs)
    pack_configure = pack
    def pack_forget(self): return self.surface.pack_forget()
    def pack_info(self): return self.surface.pack_info()
    def grid(self, *args, **kwargs): return self.surface.grid(*args, **kwargs)
    grid_configure = grid
    def grid_forget(self): return self.surface.grid_forget()
    def place(self, *args, **kwargs): return self.surface.place(*args, **kwargs)
    place_configure = place
    def place_forget(self): return self.surface.place_forget()
    def winfo_manager(self): return self.surface.winfo_manager()
    def destroy(self):
        if self.surface.destroying:
            super().destroy()
        else:
            self.surface.destroy()


class RoundedButton(SurfaceControl, tk.Button):
    def __init__(self, parent, text, command, *, fill, foreground, hover, pressed, border,
                 focus, disabled_fill, disabled_foreground, font, **kwargs):
        self.normal_fill, self.normal_fg = fill, foreground
        self.hover_fill, self.pressed_fill, self.normal_border = hover, pressed, border
        self.focus_color = focus
        self.disabled_fill, self.disabled_fg = disabled_fill, disabled_foreground
        self.hovering, self.pressing, self.edge_pressed = False, False, False
        self.surface = RoundedPanel(parent, fill=fill, border=border, radius=10, padding=(10, 3))
        padx, pady = kwargs.pop("padx", 4), kwargs.pop("pady", 7)
        super().__init__(self.surface.content, text=text, command=command, bg=fill, fg=foreground,
            activebackground=hover, activeforeground=foreground, disabledforeground=disabled_foreground,
            borderwidth=0, relief="flat", highlightthickness=0, padx=padx, pady=pady, font=font,
            cursor="hand2", takefocus=True, **kwargs)
        tk.Button.pack(self, fill="both", expand=True)
        for widget in (self, self.surface.backdrop, self.surface.content):
            widget.bind("<Enter>", self.enter, add="+")
            widget.bind("<Leave>", self.leave, add="+")
        self.bind("<FocusIn>", lambda _: self.paint(), add="+")
        self.bind("<FocusOut>", lambda _: self.paint(), add="+")
        self.bind("<ButtonPress-1>", self.press, add="+")
        self.bind("<ButtonRelease-1>", self.release, add="+")
        self.bind("<KeyPress-space>", self.press, add="+")
        self.bind("<KeyRelease-space>", self.release, add="+")
        self.bind("<Return>", self.keyboard_invoke)
        for widget in (self.surface.backdrop, self.surface.content):
            widget.bind("<ButtonPress-1>", self.edge_press)
            widget.bind("<ButtonRelease-1>", self.edge_release)
        self.paint()

    def configure(self, cnf=None, **kwargs):
        if cnf is not None and not isinstance(cnf, dict): return tk.Button.configure(self, cnf, **kwargs)
        if isinstance(cnf, dict): kwargs = {**cnf, **kwargs}
        if not kwargs: return tk.Button.configure(self)
        for key in ("bg", "background"):
            if key in kwargs: self.normal_fill = kwargs.pop(key)
        for key in ("fg", "foreground"):
            if key in kwargs: self.normal_fg = kwargs.pop(key)
        # Focus is rendered on the rounded surface, not the rectangular native box.
        kwargs.pop("highlightbackground", None); kwargs.pop("highlightcolor", None)
        before = (self.cget("state"), self.cget("text"), self.cget("bg"), self.cget("fg"))
        result = tk.Button.configure(self, **kwargs) if kwargs else None
        if before != (self.cget("state"), self.cget("text"), self.normal_fill, self.normal_fg): self.paint()
        return result
    config = configure

    def paint(self):
        if not self.winfo_exists(): return
        disabled = str(self.cget("state")) == "disabled"
        fill = self.disabled_fill if disabled else self.pressed_fill if self.pressing else self.hover_fill if self.hovering else self.normal_fill
        foreground = self.disabled_fg if disabled else self.normal_fg
        # A native ttk popup owns a Tcl-only focus window. Tkinter cannot map
        # that window to a Python widget; it is not this button's focus.
        try: focused = self.focus_get() == self
        except (KeyError, tk.TclError): focused = False
        self.surface.appearance(fill, self.focus_color if focused else self.normal_border)
        tk.Button.configure(self, bg=fill, fg=foreground, activebackground=fill,
                            activeforeground=foreground, cursor="arrow" if disabled else "hand2")

    def enter(self, _): self.hovering = True; self.paint()
    def leave(self, _):
        x, y = self.winfo_pointerxy(); panel = self.surface
        if not (panel.winfo_rootx() <= x < panel.winfo_rootx() + panel.winfo_width() and panel.winfo_rooty() <= y < panel.winfo_rooty() + panel.winfo_height()):
            self.hovering = self.pressing = False; self.paint()
    def press(self, _):
        if str(self.cget("state")) != "disabled": self.focus_set(); self.pressing = True; self.paint()
    def release(self, _): self.pressing = False; self.paint()
    def keyboard_invoke(self, _): self.invoke(); return "break"
    def edge_press(self, event): self.edge_pressed = True; self.press(event)
    def edge_release(self, event):
        pressed = self.edge_pressed; self.edge_pressed = False; self.release(event)
        if pressed and 0 <= event.x < event.widget.winfo_width() and 0 <= event.y < event.widget.winfo_height(): self.invoke()


class RoundedEntry(SurfaceControl, tk.Entry):
    def __init__(self, parent, variable=None, *, fill, foreground, border, focus, font, **kwargs):
        self.surface = RoundedPanel(parent, fill=fill, border=border, radius=10, padding=(12, 5))
        self.normal_border, self.focus_color = border, focus
        super().__init__(self.surface.content, textvariable=variable, bg=fill, fg=foreground,
            insertbackground=focus, highlightthickness=0, relief="flat", borderwidth=0,
            disabledbackground="#edf2ef", disabledforeground="#687b70",
            selectbackground="#d2ece5", selectforeground=foreground, font=font, **kwargs)
        tk.Entry.pack(self, fill="both", expand=True)
        self.bind("<FocusIn>", lambda _: self.surface.appearance(fill, focus), add="+")
        self.bind("<FocusOut>", lambda _: self.surface.appearance(fill, border), add="+")
        self.surface.backdrop.bind("<Button-1>", lambda _: self.focus_set())


class RoundedLabel(SurfaceControl, tk.Label):
    def __init__(self, parent, text="", *, fill, foreground, font, **kwargs):
        self.surface = RoundedPanel(parent, fill=fill, radius=12, padding=(10, 5))
        super().__init__(self.surface.content, text=text, bg=fill, fg=foreground, font=font, **kwargs)
        tk.Label.pack(self, fill="both", expand=True)


class RoundedTreeview(SurfaceControl, ttk.Treeview):
    def __init__(self, parent, *, fill, border, **kwargs):
        self.surface = RoundedPanel(parent, fill=fill, border=border, radius=14, padding=10)
        super().__init__(self.surface.content, **kwargs)
        ttk.Treeview.pack(self, fill="both", expand=True)
