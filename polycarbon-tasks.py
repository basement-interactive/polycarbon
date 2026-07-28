#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 vermin <vermin.gov@proton.me>
"""Polycarbon task manager — `polycarbon tasks`.

Stopping a stuck Windows program was, until this existed, a research project.
Killing the polycarbon launcher does NOT stop the program: the launcher is a
shell script, and the Windows process it started belongs to a Wine session that
outlives it. `pkill -f` on the name catches the wrong things, because the shell
running pkill matches its own command line. And a session's daemons — wineserver,
services.exe, plugplay.exe, explorer.exe — are invisible to anyone who does not
already know they exist, yet they are what holds a prefix open.

So this lists what is actually running in polycarbon's prefix and offers the two
things worth having: end one program, or end the whole session.

Processes are identified by reading WINEPREFIX out of /proc/<pid>/environ, so
only this prefix's processes are ever listed or touched — a Wine program from
some other launcher on the same machine is never in scope.
"""
import os
import signal
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gtk  # noqa: E402

BASE = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "polycarbon"
)
PREFIX = os.path.join(BASE, "prefix")
WINESERVER = os.path.join(BASE, "runner", "bin", "wineserver")

# Wine's own session processes. They are not the user's programs, and ending one
# individually usually just breaks the session, so they are grouped separately
# and hidden unless asked for.
SESSION_EXES = {
    "services.exe", "winedevice.exe", "plugplay.exe", "explorer.exe",
    "rpcss.exe", "svchost.exe", "conhost.exe", "wineboot.exe",
    "start.exe", "tabtip.exe", "sihost.exe",
}
REFRESH_MS = 2000


def _read(path, binary=False):
    # errors= is only valid in text mode; passing it with "rb" raises ValueError.
    try:
        if binary:
            with open(path, "rb") as fh:
                return fh.read()
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return b"" if binary else ""


def scan():
    """Every process belonging to polycarbon's prefix.

    Returns (apps, session, launchers). A process qualifies only if its
    environment names OUR prefix — matching on "wine" in the command line would
    sweep in other people's Wine programs and, worse, this process.
    """
    apps, session, launchers = [], [], []
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        env = _read(f"/proc/{pid}/environ", binary=True)
        if not env:
            continue
        wanted = f"WINEPREFIX={PREFIX}".encode()
        is_ours = wanted in env
        raw = _read(f"/proc/{pid}/cmdline", binary=True)
        # argv is NUL-separated. Keep the boundaries: joining on spaces first and
        # splitting later turns "C:\Program Files\Everything\Everything.exe" into
        # "C:\Program", so every program under Program Files was listed as
        # "Program".
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        cmd = " ".join(argv).strip()
        if not is_ours:
            # polycarbon's own launcher shells: they set WINEPREFIX for children
            # but may be matched here before exporting, so catch them by path.
            if "/polycarbon" in cmd and "polycarbon-tasks" not in cmd:
                launchers.append((pid, cmd, rss(pid), etime(pid)))
            continue
        if not argv:
            continue
        name = os.path.basename(argv[0].replace("\\", "/"))
        row = (pid, name or cmd[:40], rss(pid), etime(pid), cmd)
        if name.lower() in SESSION_EXES or name.lower() == "wineserver":
            session.append(row)
        else:
            apps.append(row)
    return apps, session, launchers


def rss(pid):
    for line in _read(f"/proc/{pid}/status").splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1]) // 1024
            except (ValueError, IndexError):
                return 0
    return 0


def etime(pid):
    try:
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        starttime = int(_read(f"/proc/{pid}/stat").split(") ")[-1].split()[19])
        secs = int(up - starttime / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


class TaskWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Polycarbon — Running Windows programs")
        self.set_default_size(640, 420)
        self.set_icon_name("application-x-executable")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(12)
        self.add(outer)

        self.summary = Gtk.Label(xalign=0)
        outer.pack_start(self.summary, False, False, 0)

        # name, pid, memory, uptime, full command (hidden, for the tooltip)
        self.store = Gtk.ListStore(str, int, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_tooltip_column(4)
        for i, (title, expand) in enumerate(
            [("Program", True), ("PID", False), ("Memory", False), ("Running", False)]
        ):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            col.set_expand(expand)
            self.view.append_column(col)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.view)
        outer.pack_start(scroll, True, True, 0)

        self.show_session = Gtk.CheckButton(label="Show Wine's own background processes")
        self.show_session.connect("toggled", lambda _w: self.refresh())
        outer.pack_start(self.show_session, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.pack_start(row, False, False, 0)

        self.btn_end = Gtk.Button(label="End program")
        self.btn_end.connect("clicked", self.on_end)
        row.pack_start(self.btn_end, False, False, 0)

        self.btn_force = Gtk.Button(label="Force end")
        self.btn_force.connect("clicked", self.on_force)
        row.pack_start(self.btn_force, False, False, 0)

        row.pack_start(Gtk.Box(), True, True, 0)  # spacer

        # The one that actually works when a program will not die: everything in
        # the prefix goes down together, which is also what frees a prefix whose
        # daemons are holding it open.
        self.btn_all = Gtk.Button(label="Stop everything")
        self.btn_all.get_style_context().add_class("destructive-action")
        self.btn_all.connect("clicked", self.on_stop_all)
        row.pack_start(self.btn_all, False, False, 0)

        self.refresh()
        GLib.timeout_add(REFRESH_MS, self._tick)

    def _tick(self):
        self.refresh()
        return True

    def refresh(self):
        apps, session, launchers = scan()
        sel = self.selected_pid()
        self.store.clear()
        for pid, name, mem, up, cmd in sorted(apps, key=lambda r: r[1].lower()):
            self.store.append([name, pid, f"{mem} MB", up, cmd])
        if self.show_session.get_active():
            for pid, name, mem, up, cmd in sorted(session, key=lambda r: r[1].lower()):
                self.store.append([f"{name}  (Wine)", pid, f"{mem} MB", up, cmd])
        if sel is not None:
            self.select_pid(sel)

        total = sum(r[2] for r in apps)
        bits = [f"{len(apps)} program{'' if len(apps) == 1 else 's'}"]
        if total:
            bits.append(f"{total} MB")
        if session:
            bits.append(f"{len(session)} Wine background")
        if launchers:
            bits.append(f"{len(launchers)} launcher{'' if len(launchers) == 1 else 's'}")
        self.summary.set_markup(
            "<b>Nothing is running</b>" if not apps and not session
            else "<b>" + "  ·  ".join(bits) + "</b>"
        )
        for b in (self.btn_end, self.btn_force):
            b.set_sensitive(self.selected_pid() is not None)
        self.btn_all.set_sensitive(bool(apps or session))

    def selected_pid(self):
        model, it = self.view.get_selection().get_selected()
        return None if it is None else model[it][1]

    def select_pid(self, pid):
        for row in self.store:
            if row[1] == pid:
                self.view.get_selection().select_iter(row.iter)
                return

    def _signal(self, sig):
        pid = self.selected_pid()
        if pid is None:
            return
        try:
            os.kill(pid, sig)
        except OSError as exc:
            self.summary.set_markup(f"<b>Could not end {pid}: {GLib.markup_escape_text(str(exc))}</b>")
            return
        GLib.timeout_add(400, lambda: (self.refresh(), False)[1])

    def on_end(self, _b):
        self._signal(signal.SIGTERM)

    def on_force(self, _b):
        self._signal(signal.SIGKILL)

    def on_stop_all(self, _b):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Stop every Windows program?",
        )
        dlg.format_secondary_text(
            "Everything running in polycarbon's Windows environment closes "
            "immediately, without saving. Use this when a program will not close "
            "on its own."
        )
        if dlg.run() == Gtk.ResponseType.OK:
            dlg.destroy()
            # wineserver -k ends the session and every process in it, which is
            # the only thing that reliably clears a wedged prefix.
            try:
                subprocess.run([WINESERVER, "-k"], timeout=30,
                               env={**os.environ, "WINEPREFIX": PREFIX},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as exc:
                self.summary.set_markup(
                    f"<b>Could not stop the session: {GLib.markup_escape_text(str(exc))}</b>"
                )
            GLib.timeout_add(800, lambda: (self.refresh(), False)[1])
        else:
            dlg.destroy()


def main():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        print("polycarbon tasks needs a graphical session", file=sys.stderr)
        return 1
    win = TaskWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
