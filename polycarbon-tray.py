#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 vermin <vermin.gov@proton.me>
#
# polycarbon-tray — put Windows tray icons in the Linux system tray.
#
# Wine's explorer.exe implements exactly one tray protocol: the old XEmbed
# system-tray spec, where the panel owns an X selection and clients ask it to
# adopt their icon window. Modern Wayland bars implement exactly one *other*
# protocol: StatusNotifierItem over D-Bus. Nothing on Arch bridges the two any
# more (snixembed goes the opposite way, SNI->XEmbed; KDE's xembed-sni-proxy was
# dropped in Plasma 6), so a Windows program that lives in the tray — Telegram,
# Everything, Discord — has nowhere to put its icon. Wine notices there is no
# tray and parks the icons in a 160x20 window of its own instead, which is the
# untitled sliver that shows up on a tiling compositor.
#
# This is the missing bridge, and it is deliberately dependency-free: the X11
# calls go through ctypes against libX11/libXcomposite/libXdamage, all of which
# gtk3 already pulls in for `polycarbon config`, and D-Bus goes through GLib,
# which python-gobject already provides. No new package to install.
#
# How an icon gets from Wine to the bar:
#   1. own _NET_SYSTEM_TRAY_S<n> so Wine can find a tray at all
#   2. adopt each icon window it sends (reparent + XEMBED_EMBEDDED_NOTIFY),
#      keeping the container offscreen — the icon is never meant to be a window
#   3. redirect the icon with XComposite so it renders into its own pixmap, and
#      watch that pixmap with XDamage to know when it repaints
#   4. publish each icon as its own StatusNotifierItem, re-reading the pixmap
#      whenever damage says it changed
#   5. forward clicks from the bar back to the icon window as X button events
#
# Run it with --probe to check the pieces without touching the tray selection.
import argparse
import ctypes
import ctypes.util
import os
import signal
import struct
import sys
import time
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_char,
    c_char_p,
    c_int,
    c_long,
    c_uint,
    c_ulong,
    c_void_p,
)

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio  # noqa: E402

# The unix-specific helpers moved to their own namespace in newer pygobject and
# warn from GLib. Prefer the new home when the running version has it, so this
# stays quiet on current systems without breaking older ones.
try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix

    _unix_fd_add = GLibUnix.fd_add_full
    _unix_signal_add = GLibUnix.signal_add
except (ValueError, ImportError):  # pragma: no cover - depends on host pygobject
    _unix_fd_add = GLib.unix_fd_add_full
    _unix_signal_add = GLib.unix_signal_add

LOG_PREFIX = "polycarbon-tray"
# Capture is the part of this bridge with the most ways to fail silently — a
# pixmap that never got named, a visual whose channel masks come back empty, a
# window that is not drawable yet. Set POLYCARBON_TRAY_DEBUG=1 to have every
# such give-up say which step it was.
DEBUG = os.environ.get("POLYCARBON_TRAY_DEBUG") == "1"


def log(msg):
    # stderr, unbuffered: polycarbon redirects this into its own run log, where
    # it has to interleave correctly with the shell script's own lines.
    print(f"{LOG_PREFIX}: {msg}", file=sys.stderr, flush=True)


def debug(msg):
    if DEBUG:
        log(f"debug: {msg}")


# --- X11 through ctypes -------------------------------------------------------
# Only the calls this bridge actually makes are declared. Everything is typed
# explicitly rather than left to ctypes' int-by-default guesses, because an XID
# is 64-bit here and a silently truncated window id fails in ways that look like
# a protocol bug rather than a binding bug.

Atom = c_ulong
Window = c_ulong
Drawable = c_ulong
Pixmap = c_ulong
Damage = c_ulong
Time = c_ulong
Colormap = c_ulong
VisualID = c_ulong

NONE = 0
COPY_FROM_PARENT = 0
INPUT_OUTPUT = 1
ALL_PLANES = ~0 & 0xFFFFFFFFFFFFFFFF
Z_PIXMAP = 2
CURRENT_TIME = 0
PROP_MODE_REPLACE = 0
GRAB_MODE_ASYNC = 1

# Event types
DESTROY_NOTIFY = 17
UNMAP_NOTIFY = 18
MAP_NOTIFY = 19
REPARENT_NOTIFY = 21
CONFIGURE_NOTIFY = 22
PROPERTY_NOTIFY = 28
CLIENT_MESSAGE = 33
SELECTION_CLEAR = 29

# Event masks
STRUCTURE_NOTIFY_MASK = 1 << 17
SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
SUBSTRUCTURE_REDIRECT_MASK = 1 << 20
PROPERTY_CHANGE_MASK = 1 << 22
EXPOSURE_MASK = 1 << 15
BUTTON_PRESS_MASK = 1 << 2
BUTTON_RELEASE_MASK = 1 << 3

# XComposite
COMPOSITE_REDIRECT_MANUAL = 0
COMPOSITE_REDIRECT_AUTOMATIC = 1
# XDamage
DAMAGE_REPORT_NON_EMPTY = 2

# XEmbed
XEMBED_EMBEDDED_NOTIFY = 0
XEMBED_WINDOW_ACTIVATE = 1
XEMBED_WINDOW_DEACTIVATE = 2
XEMBED_VERSION = 0
# _XEMBED_INFO is (version, flags); this is the one flag that matters here. It,
# not UnmapNotify, is how an embedded client asks to be shown or hidden.
XEMBED_MAPPED = 1 << 0

SYSTEM_TRAY_REQUEST_DOCK = 0

# Where the offscreen container lives. Far enough off any real monitor that it
# cannot be seen, while still being a mapped window — an unmapped parent means
# the client never draws and every captured pixmap comes back blank.
OFFSCREEN_X = -20000
OFFSCREEN_Y = -20000

# Channel layout of 24/32-bit TrueColor, which is the only layout any X server
# in practice uses for these depths. Last resort for the case where neither the
# image nor the window's visual reports masks.
DEFAULT_MASKS = (0xFF0000, 0x00FF00, 0x0000FF)

# How long polycarbon's "the program I am starting is called X" note stays
# usable. Long enough to cover a slow Windows program getting to its first
# Shell_NotifyIcon call, short enough that an icon appearing much later is not
# labelled with the name of something launched ages ago.
HINT_MAX_AGE = 120


class XClientMessageEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", c_void_p),
        ("window", Window),
        ("message_type", Atom),
        ("format", c_int),
        ("data", c_long * 5),
    ]


class XAnyWindowEvent(Structure):
    # Shared prefix of Destroy/Unmap/Map/Reparent notify: enough to learn which
    # window an event is about.
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", c_void_p),
        ("event", Window),
        ("window", Window),
    ]


class XPropertyEvent(Structure):
    # Unlike the notify events, this one carries the window in the first window
    # slot and an atom in the second — reading it with the generic struct above
    # would take the atom for the window id.
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", c_void_p),
        ("window", Window),
        ("atom", Atom),
        ("time", Time),
        ("state", c_int),
    ]


class XConfigureEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", c_void_p),
        ("event", Window),
        ("window", Window),
        ("x", c_int),
        ("y", c_int),
        ("width", c_int),
        ("height", c_int),
        ("border_width", c_int),
        ("above", Window),
        ("override_redirect", c_int),
    ]


class XRectangle(Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


class XDamageNotifyEvent(Structure):
    # Field order matters and is easy to get wrong: drawable comes before damage.
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", c_void_p),
        ("drawable", Drawable),
        ("damage", Damage),
        ("level", c_int),
        ("more", c_int),
        ("timestamp", Time),
        ("area", XRectangle),
        ("geometry", XRectangle),
    ]


class XImage(Structure):
    # Declared only as far as the channel masks — everything past blue_mask is
    # the private data pointer and the function table, which this never touches.
    _fields_ = [
        ("width", c_int),
        ("height", c_int),
        ("xoffset", c_int),
        ("format", c_int),
        ("data", POINTER(c_char)),
        ("byte_order", c_int),
        ("bitmap_unit", c_int),
        ("bitmap_bit_order", c_int),
        ("bitmap_pad", c_int),
        ("depth", c_int),
        ("bytes_per_line", c_int),
        ("bits_per_pixel", c_int),
        ("red_mask", c_ulong),
        ("green_mask", c_ulong),
        ("blue_mask", c_ulong),
    ]


class XWindowAttributes(Structure):
    # Declared only as far as `visual`, which is the one field this needs: the
    # channel masks have to come from the window's visual, because XGetImage on a
    # pixmap cannot supply them.
    _fields_ = [
        ("x", c_int),
        ("y", c_int),
        ("width", c_int),
        ("height", c_int),
        ("border_width", c_int),
        ("depth", c_int),
        ("visual", c_void_p),
        ("root", Window),
        ("class_", c_int),
        ("bit_gravity", c_int),
        ("win_gravity", c_int),
        ("backing_store", c_int),
        ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong),
        ("save_under", c_int),
        ("colormap", Colormap),
        ("map_installed", c_int),
        ("map_state", c_int),
        ("all_event_masks", c_long),
        ("your_event_mask", c_long),
        ("do_not_propagate_mask", c_long),
        ("override_redirect", c_int),
        ("screen", c_void_p),
    ]


class XVisualInfo(Structure):
    _fields_ = [
        ("visual", c_void_p),
        ("visualid", VisualID),
        ("screen", c_int),
        ("depth", c_int),
        ("class_", c_int),
        ("red_mask", c_ulong),
        ("green_mask", c_ulong),
        ("blue_mask", c_ulong),
        ("colormap_size", c_int),
        ("bits_per_rgb", c_int),
    ]


class XSetWindowAttributes(Structure):
    _fields_ = [
        ("background_pixmap", Pixmap),
        ("background_pixel", c_ulong),
        ("border_pixmap", Pixmap),
        ("border_pixel", c_ulong),
        ("bit_gravity", c_int),
        ("win_gravity", c_int),
        ("backing_store", c_int),
        ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong),
        ("save_under", c_int),
        ("event_mask", c_long),
        ("do_not_propagate_mask", c_long),
        ("override_redirect", c_int),
        ("colormap", Colormap),
        ("cursor", c_ulong),
    ]


CWBorderPixel = 1 << 3
CWEventMask = 1 << 11
CWOverrideRedirect = 1 << 9
CWColormap = 1 << 13


class XErrorEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("display", c_void_p),
        ("resourceid", c_ulong),
        ("serial", c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


XErrorHandler = CFUNCTYPE(c_int, c_void_p, POINTER(XErrorEvent))


def _load(name, soname):
    path = ctypes.util.find_library(name) or soname
    try:
        return ctypes.CDLL(path)
    except OSError as exc:
        raise SystemExit(f"{LOG_PREFIX}: cannot load {soname}: {exc}")


class X11:
    """The ctypes surface. One instance per X connection."""

    def __init__(self):
        self.xlib = _load("X11", "libX11.so.6")
        self.xcomposite = _load("Xcomposite", "libXcomposite.so.1")
        self.xdamage = _load("Xdamage", "libXdamage.so.1")
        self._declare()

        self.dpy = self.xlib.XOpenDisplay(None)
        if not self.dpy:
            raise SystemExit(
                f"{LOG_PREFIX}: cannot open display {os.environ.get('DISPLAY', '(unset)')}"
            )
        self.screen = self.xlib.XDefaultScreen(self.dpy)
        self.root = self.xlib.XRootWindow(self.dpy, self.screen)
        self.fd = self.xlib.XConnectionNumber(self.dpy)

        # A tray client can vanish between the event that mentions it and the
        # request that acts on it, and the resulting BadWindow would abort the
        # whole process by default. Every call here is best-effort by nature, so
        # errors are logged at most once per code and otherwise ignored.
        self._seen_errors = set()
        self._error_handler = XErrorHandler(self._on_error)
        self.xlib.XSetErrorHandler(self._error_handler)

        self._atoms = {}

    def _declare(self):
        x = self.xlib
        x.XOpenDisplay.restype = c_void_p
        x.XOpenDisplay.argtypes = [c_char_p]
        x.XDefaultScreen.restype = c_int
        x.XDefaultScreen.argtypes = [c_void_p]
        x.XRootWindow.restype = Window
        x.XRootWindow.argtypes = [c_void_p, c_int]
        x.XConnectionNumber.restype = c_int
        x.XConnectionNumber.argtypes = [c_void_p]
        x.XInternAtom.restype = Atom
        x.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
        x.XCreateWindow.restype = Window
        x.XCreateWindow.argtypes = [
            c_void_p, Window, c_int, c_int, c_uint, c_uint, c_uint, c_int,
            c_uint, c_void_p, c_ulong, POINTER(XSetWindowAttributes),
        ]
        x.XCreateSimpleWindow.restype = Window
        x.XCreateSimpleWindow.argtypes = [
            c_void_p, Window, c_int, c_int, c_uint, c_uint, c_uint, c_ulong, c_ulong,
        ]
        x.XDestroyWindow.argtypes = [c_void_p, Window]
        x.XMapWindow.argtypes = [c_void_p, Window]
        x.XUnmapWindow.argtypes = [c_void_p, Window]
        x.XMapRaised.argtypes = [c_void_p, Window]
        x.XReparentWindow.argtypes = [c_void_p, Window, Window, c_int, c_int]
        x.XSelectInput.argtypes = [c_void_p, Window, c_long]
        x.XSetSelectionOwner.argtypes = [c_void_p, Atom, Window, Time]
        x.XGetSelectionOwner.restype = Window
        x.XGetSelectionOwner.argtypes = [c_void_p, Atom]
        x.XSendEvent.argtypes = [c_void_p, Window, c_int, c_long, c_void_p]
        x.XChangeProperty.argtypes = [
            c_void_p, Window, Atom, Atom, c_int, c_int, c_void_p, c_int,
        ]
        x.XFlush.argtypes = [c_void_p]
        x.XSync.argtypes = [c_void_p, c_int]
        x.XPending.restype = c_int
        x.XPending.argtypes = [c_void_p]
        x.XNextEvent.argtypes = [c_void_p, c_void_p]
        x.XGetImage.restype = POINTER(XImage)
        x.XGetImage.argtypes = [
            c_void_p, Drawable, c_int, c_int, c_uint, c_uint, c_ulong, c_int,
        ]
        x.XDestroyImage.argtypes = [POINTER(XImage)]
        x.XFreePixmap.argtypes = [c_void_p, Pixmap]
        x.XGetWindowAttributes.argtypes = [c_void_p, Window, c_void_p]
        x.XGetGeometry.argtypes = [
            c_void_p, Drawable, POINTER(Window), POINTER(c_int), POINTER(c_int),
            POINTER(c_uint), POINTER(c_uint), POINTER(c_uint), POINTER(c_uint),
        ]
        x.XResizeWindow.argtypes = [c_void_p, Window, c_uint, c_uint]
        x.XGetVisualInfo.restype = POINTER(XVisualInfo)
        x.XGetVisualInfo.argtypes = [c_void_p, c_long, POINTER(XVisualInfo), POINTER(c_int)]
        x.XCreateColormap.restype = Colormap
        x.XCreateColormap.argtypes = [c_void_p, Window, c_void_p, c_int]
        x.XFree.argtypes = [c_void_p]
        x.XSetErrorHandler.restype = c_void_p
        x.XSetErrorHandler.argtypes = [XErrorHandler]
        x.XDefaultRootWindow.restype = Window
        x.XDefaultRootWindow.argtypes = [c_void_p]
        x.XVisualIDFromVisual.restype = VisualID
        x.XVisualIDFromVisual.argtypes = [c_void_p]
        x.XGetWindowProperty.restype = c_int
        x.XGetWindowProperty.argtypes = [
            c_void_p, Window, Atom, c_long, c_long, c_int, Atom,
            POINTER(Atom), POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong),
            POINTER(POINTER(c_ulong)),
        ]

        c = self.xcomposite
        c.XCompositeQueryExtension.restype = c_int
        c.XCompositeQueryExtension.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
        c.XCompositeRedirectWindow.argtypes = [c_void_p, Window, c_int]
        c.XCompositeUnredirectWindow.argtypes = [c_void_p, Window, c_int]
        c.XCompositeNameWindowPixmap.restype = Pixmap
        c.XCompositeNameWindowPixmap.argtypes = [c_void_p, Window]

        d = self.xdamage
        d.XDamageQueryExtension.restype = c_int
        d.XDamageQueryExtension.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
        d.XDamageCreate.restype = Damage
        d.XDamageCreate.argtypes = [c_void_p, Drawable, c_int]
        d.XDamageDestroy.argtypes = [c_void_p, Damage]
        d.XDamageSubtract.argtypes = [c_void_p, Damage, c_ulong, c_ulong]

    def _on_error(self, _dpy, err):
        e = err.contents
        key = (e.request_code, e.error_code)
        if key not in self._seen_errors:
            self._seen_errors.add(key)
            log(f"ignoring X error: request {e.request_code} code {e.error_code}")
        return 0

    def atom(self, name):
        if name not in self._atoms:
            self._atoms[name] = self.xlib.XInternAtom(self.dpy, name.encode(), False)
        return self._atoms[name]

    def flush(self):
        self.xlib.XFlush(self.dpy)

    def sync(self):
        self.xlib.XSync(self.dpy, False)

    def send_client_message(self, dest, message_type, data, mask=0, propagate=False):
        ev = XClientMessageEvent()
        ev.type = CLIENT_MESSAGE
        ev.window = dest
        ev.message_type = message_type
        ev.format = 32
        for i, v in enumerate(data[:5]):
            ev.data[i] = v
        self.xlib.XSendEvent(self.dpy, dest, propagate, mask, byref(ev))

    def cardinals(self, win, prop_name, count=2):
        """Read up to `count` 32-bit values from a window property, or None."""
        actual_type, actual_format = Atom(), c_int()
        nitems, after = c_ulong(), c_ulong()
        data = POINTER(c_ulong)()
        ok = self.xlib.XGetWindowProperty(
            self.dpy, win, self.atom(prop_name), 0, count, False, 0,
            byref(actual_type), byref(actual_format), byref(nitems),
            byref(after), byref(data),
        )
        if ok != 0 or not data:
            return None
        try:
            if nitems.value == 0:
                return None
            # X returns 32-bit property values widened to long, so read them as
            # longs and mask rather than reinterpreting the buffer as int32.
            return [data[i] & 0xFFFFFFFF for i in range(min(nitems.value, count))]
        finally:
            self.xlib.XFree(data)

    def visual_masks(self, win):
        """The RGB channel masks of a window's visual, or None.

        XGetImage fills these in when it reads a Window, but not when it reads a
        Pixmap — a pixmap has a depth and no visual, so the masks come back zero
        and the image looks undecodable even though the pixels are fine. Asking
        the window that owns the pixmap is the way to get them.
        """
        attrs = XWindowAttributes()
        if not self.xlib.XGetWindowAttributes(self.dpy, win, byref(attrs)):
            return None
        if not attrs.visual:
            return None
        template = XVisualInfo()
        template.visualid = self.xlib.XVisualIDFromVisual(attrs.visual)
        VisualIDMask = 0x1
        count = c_int()
        infos = self.xlib.XGetVisualInfo(
            self.dpy, VisualIDMask, byref(template), byref(count)
        )
        if not infos or count.value == 0:
            return None
        try:
            vi = infos[0]
            if not (vi.red_mask and vi.green_mask and vi.blue_mask):
                return None
            return vi.red_mask, vi.green_mask, vi.blue_mask
        finally:
            self.xlib.XFree(infos)

    def window_size(self, win):
        root = Window()
        xx, yy = c_int(), c_int()
        w, h, bw, depth = c_uint(), c_uint(), c_uint(), c_uint()
        ok = self.xlib.XGetGeometry(
            self.dpy, win, byref(root), byref(xx), byref(yy),
            byref(w), byref(h), byref(bw), byref(depth),
        )
        if not ok:
            return None
        return w.value, h.value, depth.value


# --- One docked icon ----------------------------------------------------------


class TrayIcon:
    """An adopted XEmbed icon window, published as a StatusNotifierItem.

    Two identities have to be kept apart: the X window Wine drew the icon into,
    and the D-Bus name the bar sees. The SNI spec gives one item per bus name, so
    each icon gets a private connection to the session bus — sharing one
    connection would collide on /StatusNotifierItem the moment a second Windows
    program put an icon in the tray.
    """

    SNI_XML = """
    <node>
      <interface name="org.kde.StatusNotifierItem">
        <property name="Category" type="s" access="read"/>
        <property name="Id" type="s" access="read"/>
        <property name="Title" type="s" access="read"/>
        <property name="Status" type="s" access="read"/>
        <property name="WindowId" type="i" access="read"/>
        <property name="IconName" type="s" access="read"/>
        <property name="IconPixmap" type="a(iiay)" access="read"/>
        <property name="OverlayIconName" type="s" access="read"/>
        <property name="AttentionIconName" type="s" access="read"/>
        <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
        <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
        <property name="ItemIsMenu" type="b" access="read"/>
        <property name="Menu" type="o" access="read"/>
        <method name="Activate">
          <arg name="x" type="i" direction="in"/>
          <arg name="y" type="i" direction="in"/>
        </method>
        <method name="SecondaryActivate">
          <arg name="x" type="i" direction="in"/>
          <arg name="y" type="i" direction="in"/>
        </method>
        <method name="ContextMenu">
          <arg name="x" type="i" direction="in"/>
          <arg name="y" type="i" direction="in"/>
        </method>
        <method name="Scroll">
          <arg name="delta" type="i" direction="in"/>
          <arg name="orientation" type="s" direction="in"/>
        </method>
        <signal name="NewIcon"/>
        <signal name="NewToolTip"/>
        <signal name="NewStatus">
          <arg name="status" type="s"/>
        </signal>
      </interface>
    </node>
    """

    OBJECT_PATH = "/StatusNotifierItem"

    def __init__(self, host, client, index):
        self.host = host
        self.x = host.x
        self.client = client
        self.index = index
        self.container = 0
        self.pixmap = 0
        self.damage = 0
        self.width = 0
        self.height = 0
        self.depth = 0
        self._argb = []          # cached IconPixmap payload
        self._masks = None       # channel masks of the client's visual
        self._refresh_pending = 0
        self._conn = None
        self._reg_id = 0
        self._owner_id = 0
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-{index}"
        self._status = "Active"
        # Which Windows program this icon belongs to. Wine gives no way to ask:
        # every tray icon window is owned by explorer.exe, so WM_CLASS and
        # _NET_WM_PID both name the Wine session process, and WM_NAME — where the
        # XEmbed convention would put a tooltip — is left empty. The name comes
        # from polycarbon instead, which knows what it just launched.
        self._app = host.claim_app_hint() or "Windows program"

    # -- X side ------------------------------------------------------------
    def adopt(self):
        """Reparent, XEmbed and redirect the client. False if it went away."""
        x = self.x
        geom = x.window_size(self.client)
        if geom is None:
            return False
        self.width, self.height, self.depth = geom
        # A client may report 0x0 until it is mapped; give it the tray's nominal
        # icon size so the first capture has something to read.
        self.width = self.width or 24
        self.height = self.height or 24
        # Read while the window is still its own: after reparenting into the
        # offscreen container the visual is unchanged, but taking it now keeps the
        # lookup off the capture path, which runs on every repaint.
        self._masks = self.x.visual_masks(self.client)

        attrs = XSetWindowAttributes()
        attrs.override_redirect = 1
        attrs.event_mask = SUBSTRUCTURE_NOTIFY_MASK | EXPOSURE_MASK
        attrs.border_pixel = 0
        self.container = x.xlib.XCreateWindow(
            x.dpy, x.root, OFFSCREEN_X, OFFSCREEN_Y,
            max(self.width, 1), max(self.height, 1), 0,
            COPY_FROM_PARENT, INPUT_OUTPUT, None,
            CWOverrideRedirect | CWEventMask | CWBorderPixel, byref(attrs),
        )
        if not self.container:
            return False

        x.xlib.XSelectInput(
            x.dpy, self.client,
            STRUCTURE_NOTIFY_MASK | PROPERTY_CHANGE_MASK,
        )
        x.xlib.XReparentWindow(x.dpy, self.client, self.container, 0, 0)

        # The container has to be mapped for the client to render at all, but it
        # is parked far offscreen so it is never visible.
        x.xlib.XMapWindow(x.dpy, self.container)

        # XEmbed handshake. Without EMBEDDED_NOTIFY a conforming client will not
        # consider itself embedded and may never draw; WINDOW_ACTIVATE is what
        # makes it treat itself as live and accept input.
        xembed = x.atom("_XEMBED")
        x.send_client_message(
            self.client, xembed,
            [CURRENT_TIME, XEMBED_EMBEDDED_NOTIFY, 0, self.container, XEMBED_VERSION],
        )
        x.send_client_message(
            self.client, xembed,
            [CURRENT_TIME, XEMBED_WINDOW_ACTIVATE, 0, 0, 0],
        )
        x.xlib.XMapWindow(x.dpy, self.client)
        x.sync()

        self._redirect()
        return True

    def _redirect(self):
        """Redirect the client offscreen and start watching it for repaints."""
        x = self.x
        if self.host.have_composite:
            # Manual redirect: the contents go to an offscreen pixmap and are
            # never composited to the screen, which is exactly right for a window
            # that only exists to be screenshotted.
            x.xcomposite.XCompositeRedirectWindow(
                x.dpy, self.client, COMPOSITE_REDIRECT_MANUAL
            )
            self._name_pixmap()
        if self.host.have_damage:
            self.damage = x.xdamage.XDamageCreate(
                x.dpy, self.client, DAMAGE_REPORT_NON_EMPTY
            )
        x.sync()

    def _name_pixmap(self):
        x = self.x
        if self.pixmap:
            x.xlib.XFreePixmap(x.dpy, self.pixmap)
            self.pixmap = 0
        self.pixmap = x.xcomposite.XCompositeNameWindowPixmap(x.dpy, self.client)

    def sync_embed_state(self):
        """Follow the client's _XEMBED_INFO mapped flag.

        A Windows program hiding its tray icon (Shell_NotifyIcon modifying it,
        or a state change) clears XEMBED_MAPPED rather than destroying anything.
        Mirroring it into SNI's Active/Passive status is what keeps a hidden icon
        out of the bar without throwing the item away — the item has to survive,
        because Wine will not send a second dock request for this window.
        """
        info = self.x.cardinals(self.client, "_XEMBED_INFO", 2)
        mapped = True if info is None else bool(info[1] & XEMBED_MAPPED)
        status = "Active" if mapped else "Passive"
        if status == self._status:
            return
        self._status = status
        if mapped:
            self.x.xlib.XMapWindow(self.x.dpy, self.client)
        else:
            self.x.xlib.XUnmapWindow(self.x.dpy, self.client)
        self.x.flush()
        self._emit("NewStatus", GLib.Variant("(s)", (status,)))
        log(f"icon {self.index}: now {status.lower()}")

    def resized(self, width, height):
        if (width, height) == (self.width, self.height) or width <= 0 or height <= 0:
            return
        self.width, self.height = width, height
        self.x.xlib.XResizeWindow(self.x.dpy, self.container, width, height)
        # A named pixmap is tied to the window's size at naming time, so a resize
        # invalidates it and it has to be re-named or every later capture reads
        # the stale geometry.
        if self.host.have_composite:
            self._name_pixmap()
        self.refresh()

    def capture(self):
        """Read the icon's pixels as SNI ARGB32. None when nothing is drawable."""
        x = self.x
        source = self.pixmap or self.client
        if not source:
            debug(f"icon {self.index}: no pixmap and no client to read")
            return None
        geom = x.window_size(source)
        if geom is None:
            debug(f"icon {self.index}: XGetGeometry failed on 0x{source:x}")
            return None
        w, h, depth = geom
        if w <= 0 or h <= 0:
            debug(f"icon {self.index}: source is {w}x{h}")
            return None
        img_p = x.xlib.XGetImage(x.dpy, source, 0, 0, w, h, ALL_PLANES, Z_PIXMAP)
        if not img_p:
            debug(
                f"icon {self.index}: XGetImage failed on "
                f"{'pixmap' if self.pixmap else 'window'} 0x{source:x} {w}x{h} depth {depth}"
            )
            return None
        try:
            img = img_p.contents
            debug(
                f"icon {self.index}: XImage {img.width}x{img.height} depth {img.depth} "
                f"bpp {img.bits_per_pixel} stride {img.bytes_per_line} "
                f"masks r=0x{img.red_mask:x} g=0x{img.green_mask:x} b=0x{img.blue_mask:x}"
            )
            masks = (img.red_mask, img.green_mask, img.blue_mask)
            if not all(masks):
                masks = self._masks or DEFAULT_MASKS
                debug(f"icon {self.index}: using visual masks {[hex(m) for m in masks]}")
            return self._to_argb32(img, w, h, masks)
        finally:
            x.xlib.XDestroyImage(img_p)

    @staticmethod
    def _to_argb32(img, w, h, masks):
        """XImage -> (w, h, ARGB32 big-endian bytes), the SNI IconPixmap format.

        The spec wants ARGB32 in network byte order; X hands over native-endian
        pixels whose channel positions are described by masks rather than fixed,
        so the channels are pulled out by mask instead of assumed.
        """
        bpp = img.bits_per_pixel
        if bpp not in (24, 32):
            return None
        stride = img.bytes_per_line
        raw = ctypes.string_at(img.data, stride * h)

        rm, gm, bm = masks
        if not (rm and gm and bm):
            return None
        # Lowest set bit of each mask is that channel's shift.
        rs = (rm & -rm).bit_length() - 1
        gs = (gm & -gm).bit_length() - 1
        bs = (bm & -bm).bit_length() - 1
        # Whatever bits none of the colour channels claim is the alpha channel;
        # a 24-bit visual has none, and those icons are simply opaque.
        am = (~(rm | gm | bm)) & 0xFFFFFFFF if bpp == 32 else 0
        a_shift = ((am & -am).bit_length() - 1) if am else 0

        little = img.byte_order == 0
        pixel_bytes = 4 if bpp == 32 else 3
        out = bytearray(w * h * 4)
        any_alpha = False
        o = 0
        for y in range(h):
            base = y * stride
            for xx in range(w):
                off = base + xx * pixel_bytes
                chunk = raw[off:off + pixel_bytes]
                if len(chunk) < pixel_bytes:
                    return None
                if pixel_bytes == 4:
                    px = struct.unpack("<I" if little else ">I", chunk)[0]
                else:
                    px = (
                        int.from_bytes(chunk, "little" if little else "big")
                        & 0xFFFFFF
                    )
                a = ((px & am) >> a_shift) & 0xFF if am else 0xFF
                if a:
                    any_alpha = True
                out[o] = a
                out[o + 1] = (px & rm) >> rs & 0xFF
                out[o + 2] = (px & gm) >> gs & 0xFF
                out[o + 3] = (px & bm) >> bs & 0xFF
                o += 4

        # A client that drew on a 32-bit visual without ever setting alpha yields
        # a fully transparent image, which the bar renders as a blank gap. Treat
        # an all-zero alpha channel as "no alpha information" and force opaque.
        if not any_alpha:
            for i in range(0, len(out), 4):
                out[i] = 0xFF
        return w, h, bytes(out)

    def forward_click(self, button):
        """Send a press/release pair to the icon window.

        The bar knows nothing about X, so a click on the published item has to be
        replayed onto the window Wine is listening to. These are synthetic events
        (send_event set): Wine's X11 driver acts on them, but a client that
        filters synthetic input would not — the known limit of doing this without
        driving the real pointer, which would mean warping it offscreen.
        """
        x = self.x
        for kind, name in ((4, "ButtonPress"), (5, "ButtonRelease")):
            ev = (c_long * 24)()
            buf = ctypes.cast(ev, c_void_p)
            # XButtonEvent laid out by hand: type, serial, send_event, display,
            # window, root, subwindow, time, x, y, x_root, y_root, state, button,
            # same_screen.
            fields = ctypes.cast(buf, POINTER(c_long))
            ints = ctypes.cast(buf, POINTER(c_int))
            ints[0] = kind
            fields[1] = 0
            ints[4] = 1                      # send_event
            fields[3] = 0                    # display, filled by the server
            fields[4] = self.client          # window
            fields[5] = x.root               # root
            fields[6] = 0                    # subwindow
            fields[7] = CURRENT_TIME
            ints[16] = max(self.width // 2, 1)
            ints[17] = max(self.height // 2, 1)
            ints[18] = 0
            ints[19] = 0
            fields[10] = 0                   # state
            fields[11] = button
            ints[24] = 1                     # same_screen
            mask = BUTTON_PRESS_MASK if kind == 4 else BUTTON_RELEASE_MASK
            x.xlib.XSendEvent(x.dpy, self.client, False, mask, buf)
        x.flush()
        log(f"forwarded button {button} to icon {self.index}")

    # -- D-Bus side --------------------------------------------------------
    def publish(self):
        """Own a private bus name and register the item with the watcher."""
        try:
            self._conn = Gio.DBusConnection.new_for_address_sync(
                Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
        except GLib.Error as exc:
            log(f"icon {self.index}: no session bus ({exc.message})")
            return False

        node = Gio.DBusNodeInfo.new_for_xml(self.SNI_XML)
        # register_object is deprecated in current pygobject, which offers
        # register_object_with_closures2 instead; the plain name is all an older
        # pygobject has. Take the first that exists so this neither warns on new
        # systems nor breaks on old ones.
        register = next(
            (
                getattr(self._conn, name)
                for name in (
                    "register_object_with_closures2",
                    "register_object_with_closures",
                    "register_object",
                )
                if hasattr(self._conn, name)
            ),
            None,
        )
        if register is None:
            log(f"icon {self.index}: pygobject exposes no object registration call")
            return False
        try:
            self._reg_id = register(
                self.OBJECT_PATH, node.interfaces[0],
                self._on_method, self._on_get, None,
            )
        except (GLib.Error, TypeError) as exc:
            log(f"icon {self.index}: cannot export item ({exc})")
            return False

        self._owner_id = Gio.bus_own_name_on_connection(
            self._conn,
            self._bus_name,
            Gio.BusNameOwnerFlags.NONE,
            self._on_name_acquired,
            self._on_name_lost,
        )
        return True

    def _on_name_acquired(self, _conn, name):
        # Only register with the watcher once the name is actually ours, or the
        # bar looks up a name nobody answers on and drops the item.
        try:
            self._conn.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (name,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            log(f"icon {self.index}: registered as {name}")
        except GLib.Error as exc:
            log(f"icon {self.index}: watcher refused registration ({exc.message})")

    def _on_name_lost(self, _conn, name):
        log(f"icon {self.index}: lost bus name {name}")

    # PyGObject calls the property getter with (connection, sender, object_path,
    # interface_name, property_name) — no GError out-parameter and no user_data,
    # unlike the C signature. Getting this wrong raises TypeError inside the
    # callback, which D-Bus reports as a failed property read, and the bar then
    # quietly drops the item.
    def _on_get(self, _conn, _sender, _path, _iface, prop):
        if prop == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if prop == "Id":
            return GLib.Variant("s", f"polycarbon-{self.index}")
        if prop == "Title":
            return GLib.Variant("s", self._app)
        if prop == "Status":
            return GLib.Variant("s", self._status)
        if prop == "WindowId":
            # Deliberately 0: the icon window is an offscreen container, not a
            # toplevel the bar could sensibly raise or focus.
            return GLib.Variant("i", 0)
        if prop in ("IconName", "OverlayIconName", "AttentionIconName"):
            return GLib.Variant("s", "")
        if prop == "IconPixmap":
            return GLib.Variant("a(iiay)", self._argb)
        if prop == "AttentionIconPixmap":
            return GLib.Variant("a(iiay)", [])
        if prop == "ToolTip":
            # What the bar shows on hover. The icon goes in the pixmap slot so a
            # bar that renders an image in its popup gets one. Windows' own tip
            # text (szTip) is not reachable — Wine keeps it for the tooltip window
            # it would draw itself, and never puts it on the X window — so the
            # program name plus a plain description is the honest best here.
            return GLib.Variant(
                "(sa(iiay)ss)",
                ("", self._argb, self._app, "Windows tray icon via Polycarbon"),
            )
        if prop == "ItemIsMenu":
            # False: clicks are forwarded to the Windows program, which draws its
            # own menu. There is no D-Bus menu to hand the bar.
            return GLib.Variant("b", False)
        if prop == "Menu":
            return GLib.Variant("o", "/NO_DBUSMENU")
        return None

    def _on_method(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "Activate":
            self.forward_click(1)
        elif method == "SecondaryActivate":
            self.forward_click(2)
        elif method == "ContextMenu":
            self.forward_click(3)
        elif method == "Scroll":
            delta = params.unpack()[0] if params else 0
            self.forward_click(4 if delta < 0 else 5)
        invocation.return_value(None)

    def refresh_soon(self):
        """Coalesce a burst of damage into one capture.

        Reading pixels is a per-pixel loop in Python, and an animated tray icon
        reports damage on every frame it draws. Collapsing a burst into a single
        capture bounds that cost to a few per second, which is far more than a
        tray icon needs to look live.
        """
        if self._refresh_pending:
            return
        self._refresh_pending = GLib.timeout_add(120, self._refresh_now)

    def _refresh_now(self):
        self._refresh_pending = 0
        self.refresh()
        return False

    def refresh(self):
        """Re-read the pixels and tell the bar if they changed."""
        shot = self.capture()
        if shot is None:
            return
        payload = [shot]
        if payload == self._argb:
            return
        self._argb = payload
        self._emit("NewIcon", None)
        # The hover popup carries the same pixels, so it is stale the moment the
        # icon changes and needs its own signal — bars cache the tooltip.
        self._emit("NewToolTip", None)

    def _emit(self, signal, args):
        if not (self._conn and self._reg_id):
            return
        try:
            self._conn.emit_signal(
                None, self.OBJECT_PATH, "org.kde.StatusNotifierItem", signal, args
            )
        except GLib.Error:
            pass

    def dispose(self):
        x = self.x
        if self.damage:
            x.xdamage.XDamageDestroy(x.dpy, self.damage)
            self.damage = 0
        if self.pixmap:
            x.xlib.XFreePixmap(x.dpy, self.pixmap)
            self.pixmap = 0
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        if self._conn and self._reg_id:
            self._conn.unregister_object(self._reg_id)
            self._reg_id = 0
        if self._conn:
            try:
                self._conn.close_sync(None)
            except GLib.Error:
                pass
            self._conn = None
        if self.container:
            x.xlib.XDestroyWindow(x.dpy, self.container)
            self.container = 0
        x.flush()


# --- The tray host ------------------------------------------------------------


class TrayHost:
    def __init__(self, x):
        self.x = x
        self.owner = 0
        self.selection = x.atom(f"_NET_SYSTEM_TRAY_S{x.screen}")
        self.icons = {}
        self.have_composite = False
        self.have_damage = False
        self.damage_event_base = 0
        self._next_index = 0
        self._pending = {}

    def claim_app_hint(self):
        """The name of the program polycarbon most recently launched, if fresh.

        Wine publishes nothing that identifies which program an icon belongs to,
        so polycarbon drops the name of whatever it is about to run into a file
        and this reads it when an icon appears. Sequential launches — which is
        how polycarbon runs things — attribute correctly. Two programs starting
        at once could in principle swap names; a stale hint is ignored outright
        rather than mislabelling an icon that docks minutes later.
        """
        path = os.environ.get("POLYCARBON_TRAY_HINT")
        if not path:
            return None
        try:
            if time.time() - os.path.getmtime(path) > HINT_MAX_AGE:
                return None
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    def query_extensions(self):
        major, minor = c_int(), c_int()
        self.have_composite = bool(
            self.x.xcomposite.XCompositeQueryExtension(
                self.x.dpy, byref(major), byref(minor)
            )
        )
        ev_base, err_base = c_int(), c_int()
        self.have_damage = bool(
            self.x.xdamage.XDamageQueryExtension(
                self.x.dpy, byref(ev_base), byref(err_base)
            )
        )
        if self.have_damage:
            self.damage_event_base = ev_base.value
        return self.have_composite, self.have_damage

    def claim(self):
        """Take ownership of the tray selection. False if a tray already exists."""
        x = self.x
        existing = x.xlib.XGetSelectionOwner(x.dpy, self.selection)
        if existing:
            log(f"a system tray already owns the selection (window 0x{existing:x})")
            return False

        self.owner = x.xlib.XCreateSimpleWindow(
            x.dpy, x.root, -1, -1, 1, 1, 0, 0, 0
        )
        x.xlib.XSelectInput(x.dpy, self.owner, STRUCTURE_NOTIFY_MASK | PROPERTY_CHANGE_MASK)

        # Advertise a 32-bit visual when one exists, so clients that can draw
        # translucent icons do; without this property they fall back to the
        # screen's visual and lose their alpha channel.
        self._advertise_visual()
        # Horizontal, matching how every bar lays a tray out.
        orientation = ctypes.c_int32(0)
        x.xlib.XChangeProperty(
            x.dpy, self.owner, x.atom("_NET_SYSTEM_TRAY_ORIENTATION"),
            x.atom("CARDINAL"), 32, PROP_MODE_REPLACE, byref(orientation), 1,
        )

        x.xlib.XSetSelectionOwner(x.dpy, self.selection, self.owner, CURRENT_TIME)
        x.sync()
        if x.xlib.XGetSelectionOwner(x.dpy, self.selection) != self.owner:
            log("failed to take the tray selection")
            return False

        # Tell every X client a tray manager just appeared. Wine checks for an
        # owner when it first needs the tray, but an app already running would
        # otherwise never look again.
        x.send_client_message(
            x.root, x.atom("MANAGER"),
            [CURRENT_TIME, self.selection, self.owner, 0, 0],
            mask=STRUCTURE_NOTIFY_MASK,
        )
        x.flush()
        log(f"owning _NET_SYSTEM_TRAY_S{x.screen} (window 0x{self.owner:x})")
        return True

    def _advertise_visual(self):
        x = self.x
        template = XVisualInfo()
        template.screen = x.screen
        template.depth = 32
        template.class_ = 4  # TrueColor
        VisualScreenMask, VisualDepthMask, VisualClassMask = 0x2, 0x4, 0x8
        count = c_int()
        infos = x.xlib.XGetVisualInfo(
            x.dpy, VisualScreenMask | VisualDepthMask | VisualClassMask,
            byref(template), byref(count),
        )
        if not infos or count.value == 0:
            return
        try:
            visual_id = ctypes.c_uint32(infos[0].visualid)
            x.xlib.XChangeProperty(
                x.dpy, self.owner, x.atom("_NET_SYSTEM_TRAY_VISUAL"),
                x.atom("VISUALID"), 32, PROP_MODE_REPLACE, byref(visual_id), 1,
            )
        finally:
            x.xlib.XFree(infos)

    def dock(self, client):
        if client in self.icons or client == 0:
            return
        index = self._next_index
        self._next_index += 1
        icon = TrayIcon(self, client, index)
        if not icon.adopt():
            log(f"dock request for 0x{client:x} — window vanished before adoption")
            return
        if not icon.publish():
            icon.dispose()
            return
        self.icons[client] = icon
        log(f"docked icon 0x{client:x} as index {index} ({len(self.icons)} total)")
        # The first paint often lands before the damage watch is armed, so take a
        # capture immediately and another shortly after the client settles.
        icon.refresh()
        GLib.timeout_add(400, self._settle, client)

    def _settle(self, client):
        icon = self.icons.get(client)
        if icon:
            icon.refresh()
        return False

    def undock(self, client):
        icon = self.icons.pop(client, None)
        if icon:
            log(f"icon 0x{client:x} (index {icon.index}) went away")
            icon.dispose()

    def handle_event(self, buf):
        x = self.x
        etype = ctypes.cast(buf, POINTER(c_int))[0]

        if self.have_damage and etype == self.damage_event_base + 0:
            ev = ctypes.cast(buf, POINTER(XDamageNotifyEvent)).contents
            # Subtract first: leaving the region un-subtracted means the server
            # stops reporting further damage for this drawable.
            x.xdamage.XDamageSubtract(x.dpy, ev.damage, NONE, NONE)
            for icon in self.icons.values():
                if icon.damage == ev.damage:
                    icon.refresh()
                    break
            return

        if etype == CLIENT_MESSAGE:
            ev = ctypes.cast(buf, POINTER(XClientMessageEvent)).contents
            if ev.message_type == x.atom("_NET_SYSTEM_TRAY_OPCODE"):
                if ev.data[1] == SYSTEM_TRAY_REQUEST_DOCK:
                    self.dock(ev.data[2] & 0xFFFFFFFF)
            return

        if etype == DESTROY_NOTIFY:
            # The only event that actually means the icon is gone for good. Wine
            # destroys the window when the program calls Shell_NotifyIcon with
            # NIM_DELETE, or when the program exits.
            ev = ctypes.cast(buf, POINTER(XAnyWindowEvent)).contents
            if ev.window in self.icons:
                self.undock(ev.window)
            return

        if etype == UNMAP_NOTIFY:
            # Deliberately NOT an undock. In XEmbed the embedder decides whether
            # the client is mapped, so an unmap is routine — Wine unmaps and
            # remaps the icon window while modifying an icon. Tearing the item
            # down here made icons vanish permanently, because Wine never sends a
            # second dock request for a window it has already handed over.
            # Visibility is driven by _XEMBED_INFO below instead.
            return

        if etype == PROPERTY_NOTIFY:
            ev = ctypes.cast(buf, POINTER(XPropertyEvent)).contents
            icon = self.icons.get(ev.window)
            if icon and ev.atom == x.atom("_XEMBED_INFO"):
                icon.sync_embed_state()
            return

        if etype == CONFIGURE_NOTIFY:
            ev = ctypes.cast(buf, POINTER(XConfigureEvent)).contents
            icon = self.icons.get(ev.window)
            if icon:
                icon.resized(ev.width, ev.height)
            return

        if etype == SELECTION_CLEAR:
            log("another tray took the selection — standing down")
            self.shutdown()
            return

    def shutdown(self):
        for client in list(self.icons):
            self.undock(client)
        if self.owner:
            self.x.xlib.XDestroyWindow(self.x.dpy, self.owner)
            self.owner = 0
        self.x.flush()


# --- Wiring -------------------------------------------------------------------


def watcher_available():
    """Is there an SNI host to publish to?"""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 2000, None,
        )
        return reply.unpack()[0]
    except GLib.Error as exc:
        log(f"cannot reach the session bus: {exc.message}")
        return False


def probe():
    """Report what the bridge would have to work with, changing nothing."""
    print(f"DISPLAY               = {os.environ.get('DISPLAY', '(unset)')}")
    x = X11()
    host = TrayHost(x)
    composite, damage = host.query_extensions()
    print(f"XComposite            = {'yes' if composite else 'NO'}")
    print(f"XDamage               = {'yes' if damage else 'NO'}")
    owner = x.xlib.XGetSelectionOwner(x.dpy, host.selection)
    print(f"_NET_SYSTEM_TRAY_S{x.screen} owner = "
          f"{'0x%x' % owner if owner else '(none — free to claim)'}")
    print(f"StatusNotifierWatcher = {'present' if watcher_available() else 'ABSENT'}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="polycarbon-tray",
        description="Bridge Wine's XEmbed tray icons to the StatusNotifierItem tray.",
    )
    ap.add_argument("--probe", action="store_true",
                    help="report display/extension/tray state and exit")
    args = ap.parse_args()

    if args.probe:
        return probe()

    if not os.environ.get("DISPLAY"):
        log("no DISPLAY — nothing to bridge")
        return 1
    if not watcher_available():
        log("no StatusNotifierWatcher on the session bus — no tray to publish to")
        return 1

    x = X11()
    host = TrayHost(x)
    composite, damage = host.query_extensions()
    if not composite:
        # Without XComposite the icon has no offscreen pixmap to read, so the
        # capture falls back to reading the window directly. That works only
        # while the window is not obscured, which offscreen it always is — so
        # say plainly that icons will likely come out blank.
        log("XComposite missing — icon capture will be unreliable")
    if not damage:
        log("XDamage missing — icons will not update after their first paint")
    if not host.claim():
        return 1

    loop = GLib.MainLoop()

    def on_x_ready(*_):
        buf = (c_long * 24)()
        while x.xlib.XPending(x.dpy) > 0:
            x.xlib.XNextEvent(x.dpy, byref(buf))
            host.handle_event(ctypes.cast(byref(buf), c_void_p))
        return True

    _unix_fd_add(GLib.PRIORITY_DEFAULT, x.fd, GLib.IOCondition.IN, on_x_ready)

    def stop(*_):
        host.shutdown()
        loop.quit()
        return False

    _unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, stop)
    _unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, stop)

    log("bridge ready — waiting for Windows tray icons")
    try:
        loop.run()
    finally:
        host.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
