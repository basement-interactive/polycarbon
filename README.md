# Polycarbon

Run Windows programs on Linux by double-clicking them. Like glass, but fake:
looks and feels like the real thing.

Polycarbon is a Wine front-end with no front-end. There is no bottle manager, no
prefix wizard, no "configure this app" step — you double-click a `.exe` and it
runs. Everything underneath is set up on first use, in the background, and never
shows itself: no update dialogs, no crash dialogs, no runner-branded entries in
your app menu.

## What it does on its own

- **Fetches and maintains the runtime.** Kron4ek's `wine-staging` wow64 build
  (64- and 32-bit programs from one prefix), kept on the latest release by a
  daily background check that migrates the environment in place. Downloads are
  verified against the release's own published `sha256sums.txt`.
- **Sets up one shared prefix silently.** Windows' Documents/Downloads/Desktop
  point straight at your real XDG directories, and your whole home tree is
  reachable as `H:\`. Dotfiles and dotfolders stay reachable too — Wine flags
  them hidden by default, which makes a Windows Open/Save dialog refuse to show
  `~/.config` and friends with no way to toggle them back on.
- **Installs the compatibility stack unattended.** wine-mono (.NET), wine-gecko
  (embedded HTML), and — on Vulkan-capable machines — DXVK for Direct3D 8-11 and
  VKD3D-Proton for Direct3D 12. Without Vulkan those two are skipped and the GL
  renderer carries Direct3D instead. Then a winetricks layer: the VC++ runtimes,
  core fonts, the D3D shader compilers, MSXML.
- **Turns installers into app-menu entries.** After a setup wizard finishes, the
  Start Menu and Desktop shortcuts it created become real `.desktop` launchers,
  icons and all — installed Windows programs show up in your launcher exactly
  like they would on Windows.
- **Bridges links and file types both ways.** A `vortex://` or `nxm://` link in
  your browser reaches the Windows program that registered it, and a document
  whose type only a Windows program knows becomes double-clickable in your file
  manager. Types the Linux desktop already handles are never taken over.
- **Puts tray programs in your system tray.** Windows programs that live in the
  tray — Telegram, Everything, Discord — put their icon in your bar like any
  native app, with a working hover tooltip and click-through. Wine only speaks
  the old XEmbed tray spec and Wayland bars only speak StatusNotifierItem, so
  Polycarbon ships the bridge between them; without it Wine parks every icon in a
  small untitled window of its own. Only programs that genuinely ask Windows for
  a tray icon get one.
- **Recovers from crashes by itself.** When a program dies on launch, Polycarbon
  walks a ladder of known Wine quirks — discrete-GPU selection, Direct3D over
  OpenGL, the gamepad-API stub, a Windows 7 version report, CPU rendering — one
  per restart, until it stays up. The fix that worked is saved per program, so
  the next launch is clean and immediate.
- **Sandboxes what you tell it to.** The first time a program runs you choose
  full access or restricted; restricted means a real bubblewrap sandbox with
  optional memory and CPU caps.

## Install

```sh
yay -S polycarbon
```

Then double-click any `.exe`, `.msi`, `.lnk`, `.bat`, `.cmd`, `.vbs` or `.reg`
file. The first run downloads about 95 MB of runtime and takes a few minutes;
everything after that is instant.

Polycarbon claims the Windows file types on its first run, but only the ones no
other program owns — an existing Bottles, Lutris or Wine setup is never
hijacked. To claim them explicitly, or to take them over anyway:

```sh
polycarbon --register          # claim only unowned types
polycarbon --register --force  # take over the rest as well
```

## Usage

```
polycarbon <file> [args...]   run a .exe .msi .lnk .bat .cmd .vbs .reg
polycarbon config             open the per-app permission manager
polycarbon --setup            install/update the runtime, then exit
polycarbon --register [-f]    become the default handler for Windows file types
polycarbon --url <uri>        open a link with the program that registered it
polycarbon --file <path>      open a file with the program that registered its type
polycarbon --version          print the version
```

## Permissions

Every program gets a permission file the first time it runs, and
`polycarbon config` is a GUI over the same files.

- **Full access** — what Windows programs on Wine always had: everything your
  user can reach.
- **Restricted** — a bubblewrap sandbox. The filesystem is read-only apart from
  the level you pick (home, Documents + Downloads, or the program's own folder),
  the session bus and compositor sockets are replaced rather than merely
  mounted read-only, and the network can be cut entirely. Memory and CPU caps
  ride on a systemd user scope.

Files live in `~/.local/share/polycarbon/perms/<program>.perm` and are plain
`KEY=value` text — editing them by hand works fine.

Restricted mode needs `bubblewrap` installed and unprivileged user namespaces
enabled in the kernel. If either is missing, Polycarbon says so in a
notification and runs the program unrestricted rather than failing silently.

## Where things live

| Path | What |
| --- | --- |
| `~/.local/share/polycarbon/` | everything: runtime, prefix, caches, state |
| `~/.local/share/polycarbon/prefix/` | the shared Wine prefix |
| `~/.local/share/polycarbon/last-run.log` | full trace of the most recent launch |
| `~/.local/share/polycarbon/previous-run.log` | the one before it |
| `~/.local/share/polycarbon/env` | per-machine tuning, applied to every program |
| `~/.local/share/polycarbon/perms/` | per-program permissions |
| `~/.local/share/polycarbon/quirks/` | crash fixes Polycarbon learned per program |
| `/usr/lib/polycarbon/polycarbon-tray.py` | the XEmbed → StatusNotifierItem tray bridge |

Run the tray bridge with `--probe` to see what it has to work with (display,
XComposite/XDamage, whether a tray already exists, whether a bar is listening),
or set `POLYCARBON_TRAY_DEBUG=1` to trace icon capture.

Nothing is written outside your home directory, and nothing needs root.

## When a program still won't run

`last-run.log` is written as the launch happens, so it survives a hard crash and
records exactly how far things got — including the program's own log files and
any crash dump it left. When the recovery ladder is exhausted, Polycarbon makes
one more run with full Wine diagnostics enabled and appends the real cause.

`~/.local/share/polycarbon/env` is the escape hatch for hardware quirks a shared
default cannot guess. It ships commented out, with the fixes worth trying in the
order worth trying them — forcing the discrete GPU, routing Direct3D through
OpenGL, turning on a performance overlay.

## Building from source

```sh
./packaging/mktarball.sh 1.0.0    # archive the tag, stamp its sha256 into the PKGBUILD
cp polycarbon-1.0.0.tar.gz packaging/ && cd packaging && makepkg -si
```

Or just run `./polycarbon` out of the checkout — it finds `polycarbon-config.py`
next to itself and behaves identically, state included.

## Credits

Polycarbon runs on other people's work: [Wine](https://www.winehq.org/) and
[Kron4ek's builds](https://github.com/Kron4ek/Wine-Builds),
[DXVK](https://github.com/doitsujin/dxvk),
[VKD3D-Proton](https://github.com/HansKristian-Work/vkd3d-proton) and
[winetricks](https://github.com/Winetricks/winetricks). None of them are
redistributed here — Polycarbon downloads each from its own upstream, verified
against upstream's own checksums, into your home directory.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
