#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 vermin <vermin.gov@proton.me>
"""Polycarbon permission manager — `polycarbon config`.

Lists every Windows program that has run through polycarbon and edits its
permission file ($BASE/perms/<key>.perm). The runner reads those files on
every launch; this GUI is just a friendly editor for them.
"""
import os
import re
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

PERMS_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "polycarbon/perms",
)

FILE_LEVELS = [
    ("full", "Everything (all files, all drives)"),
    ("home", "Home folder only"),
    ("docs", "Documents && Downloads only"),
    ("app", "Its own folder only"),
]
KEYS = ("ROOT", "FILES", "NET", "MEM", "CPU")


def read_perm(path):
    perm = {"ROOT": "on", "FILES": "full", "NET": "on", "MEM": "", "CPU": ""}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"^([A-Z]+)=(.*)$", line.strip())
            if m and m.group(1) in perm:
                perm[m.group(1)] = m.group(2)
    return perm


def write_perm(path, perm):
    body = [
        f"# Polycarbon permissions for {os.path.basename(path)[:-5]}"
        " — edit by hand or run: polycarbon config",
        "# ROOT on = unrestricted system access; every other key is then ignored",
        f"ROOT={perm['ROOT']}",
        "# FILES what the program may modify: full | home | docs | app",
        f"FILES={perm['FILES']}",
        "# NET on | off",
        f"NET={perm['NET']}",
        "# MEM memory cap, e.g. 2G (empty = unlimited)",
        f"MEM={perm['MEM']}",
        "# CPU percent of one core, e.g. 50 (empty = unlimited)",
        f"CPU={perm['CPU']}",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")


class ConfigWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Polycarbon — application permissions")
        self.set_default_size(720, 460)
        self.set_border_width(12)
        self.current = None  # (path, perm-dict)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(paned)

        # App list (left)
        self.store = Gtk.ListStore(str, str)  # display name, file path
        for fn in sorted(os.listdir(PERMS_DIR)) if os.path.isdir(PERMS_DIR) else []:
            if fn.endswith(".perm"):
                self.store.append([fn[:-5], os.path.join(PERMS_DIR, fn)])
        tree = Gtk.TreeView(model=self.store, headers_visible=False)
        tree.append_column(Gtk.TreeViewColumn("App", Gtk.CellRendererText(), text=0))
        tree.get_selection().connect("changed", self.on_select)
        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(220, -1)
        scroll.add(tree)
        paned.pack1(scroll, resize=False, shrink=False)

        # Permission editor (right)
        self.editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.editor.set_margin_start(16)
        self.editor.set_sensitive(False)
        paned.pack2(self.editor, resize=True, shrink=False)

        self.title_label = Gtk.Label(xalign=0)
        self.editor.pack_start(self.title_label, False, False, 0)

        self.root_switch = self._row(
            "System access (root of the sandbox)",
            "Full, unrestricted access as your user — how Windows programs"
            " normally run. Turning this off enables the controls below.",
            Gtk.Switch(),
        )
        self.root_switch.connect("notify::active", self.on_root_toggled)

        self.advanced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.editor.pack_start(self.advanced, False, False, 0)

        self.files_combo = Gtk.ComboBoxText()
        for level, label in FILE_LEVELS:
            self.files_combo.append(level, label)
        self._row("File access", "Which files the program may modify. Everything"
                  " else stays readable but read-only.", self.files_combo,
                  box=self.advanced)

        self.net_switch = self._row(
            "Network", "Off cuts the program from the network entirely.",
            Gtk.Switch(), box=self.advanced)

        self.mem_entry = Gtk.Entry(placeholder_text="e.g. 2G — empty = unlimited")
        self._row("Memory limit", "Hard cap on RAM. The program is stopped if"
                  " it exceeds this.", self.mem_entry, box=self.advanced)

        self.cpu_entry = Gtk.Entry(placeholder_text="e.g. 50 — empty = unlimited")
        self._row("CPU limit (% of one core)", "Throttles the program to this"
                  " share of CPU time.", self.cpu_entry, box=self.advanced)

        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self.on_save)
        bar = Gtk.Box(spacing=6)
        bar.pack_end(save, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        bar.pack_start(self.status, True, True, 0)
        self.editor.pack_end(bar, False, False, 0)

        if len(self.store) == 0:
            self.title_label.set_markup(
                "<i>No applications yet — run a Windows program first.</i>")

    def _row(self, title, subtitle, widget, box=None):
        (box or self.editor).pack_start(self._make_row(title, subtitle, widget),
                                        False, False, 0)
        return widget

    @staticmethod
    def _make_row(title, subtitle, widget):
        row = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        t = Gtk.Label(xalign=0)
        t.set_markup(f"<b>{title}</b>")
        s = Gtk.Label(xalign=0, wrap=True)
        s.set_markup(f"<small>{subtitle}</small>")
        text.pack_start(t, False, False, 0)
        text.pack_start(s, False, False, 0)
        row.pack_start(text, True, True, 0)
        align = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        align.set_valign(Gtk.Align.CENTER)
        align.pack_start(widget, False, False, 0)
        row.pack_start(align, False, False, 0)
        return row

    def on_select(self, selection):
        model, it = selection.get_selected()
        if not it:
            return
        path = model[it][1]
        perm = read_perm(path)
        self.current = (path, perm)
        self.title_label.set_markup(f"<big><b>{model[it][0]}</b></big>")
        self.root_switch.set_active(perm["ROOT"] == "on")
        self.files_combo.set_active_id(
            perm["FILES"] if perm["FILES"] in dict(FILE_LEVELS) else "full")
        self.net_switch.set_active(perm["NET"] != "off")
        self.mem_entry.set_text(perm["MEM"])
        self.cpu_entry.set_text(perm["CPU"])
        self.editor.set_sensitive(True)
        self.advanced.set_sensitive(perm["ROOT"] != "on")
        self.status.set_text("")

    def on_root_toggled(self, switch, _param):
        self.advanced.set_sensitive(not switch.get_active())

    def on_save(self, _button):
        if not self.current:
            return
        path, perm = self.current
        mem = self.mem_entry.get_text().strip().upper()
        cpu = self.cpu_entry.get_text().strip()
        if mem and not re.fullmatch(r"[0-9]+[KMGT]?", mem):
            self.status.set_markup("<span foreground='red'>Memory: use a number"
                                   " with optional K/M/G/T, e.g. 2G</span>")
            return
        if cpu and not re.fullmatch(r"[0-9]+", cpu):
            self.status.set_markup("<span foreground='red'>CPU: whole percent"
                                   " number, e.g. 50</span>")
            return
        perm.update(
            ROOT="on" if self.root_switch.get_active() else "off",
            FILES=self.files_combo.get_active_id() or "full",
            NET="on" if self.net_switch.get_active() else "off",
            MEM=mem, CPU=cpu,
        )
        write_perm(path, perm)
        self.status.set_text("Saved — applies on the program's next launch.")


def main():
    win = ConfigWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    sys.exit(main())
