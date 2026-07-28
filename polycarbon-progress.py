#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 vermin <vermin.gov@proton.me>
"""Polycarbon setup progress window.

Setting a prefix up is not instant: the runtime is ~95 MB, the component stack
another ~150 MB, and the winetricks layer downloads the better part of a
gigabyte. All of that used to happen behind a single desktop notification, so a
first launch looked like nothing happening at all — the program the user
double-clicked simply did not appear for several minutes, with no way to tell
whether it was working, downloading, or wedged.

This reads a tiny line protocol on stdin and shows it. polycarbon writes to it;
when the pipe closes, the window goes away.

    STEP <text>     what is happening now
    PCT  <0-100>    overall progress; anything else leaves the bar pulsing
    SUB  <text>     detail line under the step (the current verb, a size, …)
    TITLE <text>    window heading
    DONE            finish and close

Deliberately not a dialog with buttons: it reports, it does not ask. Closing the
window does not cancel the setup — the work is polycarbon's, not this window's —
so the close button is disabled rather than lying about what it does.
"""
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gtk  # noqa: E402


class ProgressWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Polycarbon")
        self.set_default_size(460, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        # No close button: this window does not own the work it reports on, and
        # a close button that cannot cancel is worse than none.
        self.set_deletable(False)
        self.set_icon_name("application-x-executable")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(18)
        self.add(box)

        self.heading = Gtk.Label(xalign=0)
        self.heading.set_markup("<b>Setting up the Windows environment</b>")
        box.pack_start(self.heading, False, False, 0)

        self.step = Gtk.Label(xalign=0)
        self.step.set_text("Starting…")
        self.step.set_line_wrap(True)
        box.pack_start(self.step, False, False, 0)

        self.bar = Gtk.ProgressBar()
        self.bar.set_show_text(False)
        box.pack_start(self.bar, False, False, 0)

        self.sub = Gtk.Label(xalign=0)
        self.sub.get_style_context().add_class("dim-label")
        self.sub.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        box.pack_start(self.sub, False, False, 0)

        self.note = Gtk.Label(xalign=0)
        self.note.get_style_context().add_class("dim-label")
        self.note.set_markup(
            "<small>This happens once. The program you opened starts when it finishes.</small>"
        )
        self.note.set_line_wrap(True)
        box.pack_start(self.note, False, False, 0)

        self._pulsing = True
        self._pulse_id = GLib.timeout_add(90, self._pulse)

        # Read stdin without blocking the UI thread.
        self._buf = b""
        GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT, sys.stdin.fileno(), GLib.IOCondition.IN, self._on_input
        )

    def _pulse(self):
        if self._pulsing:
            self.bar.pulse()
        return True

    def _on_input(self, _fd, condition):
        try:
            chunk = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            chunk = b""
        if not chunk:
            # Writer closed the pipe: the setup finished (or polycarbon died).
            # Either way this window has nothing left to report.
            self._finish()
            return False
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._handle(line.decode("utf-8", "replace").rstrip())
        return True

    def _handle(self, line):
        verb, _, rest = line.partition(" ")
        if verb == "STEP":
            self.step.set_text(rest)
            self.sub.set_text("")
        elif verb == "SUB":
            self.sub.set_text(rest)
        elif verb == "TITLE":
            self.heading.set_markup(f"<b>{GLib.markup_escape_text(rest)}</b>")
        elif verb == "PCT":
            try:
                frac = max(0.0, min(1.0, float(rest) / 100.0))
            except ValueError:
                return
            self._pulsing = False
            self.bar.set_fraction(frac)
        elif verb == "DONE":
            self._finish()

    def _finish(self):
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0
        Gtk.main_quit()


def main():
    # No display, no window — polycarbon still runs, it just reports through the
    # log as before. Failing here must never take a launch down with it.
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return 0
    win = ProgressWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
