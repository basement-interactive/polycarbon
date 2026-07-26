#!/bin/sh
# Produce the AUR source tarball for a tagged release and stamp its checksum into
# the PKGBUILD. Always from the tag, never from the working tree — a release
# tarball must be reproducible from what is committed, not from whatever happened
# to be lying around when it was cut.
#
# The untracked check exists because `git archive` silently omits untracked
# files. A new file that was never `git add`ed would produce a tarball that
# builds on this machine and is missing a file for everyone else.
set -eu

pkgver="${1:?usage: mktarball.sh <version>   e.g. mktarball.sh 1.0.0}"
cd "$(dirname "$0")/.."

# Both checks skip packaging/, which .gitattributes marks export-ignore so it
# never reaches the tarball — and which this script's last act modifies, so
# including it here would make every second run refuse to start.
git diff-index --quiet HEAD -- . ':!packaging' || {
	echo "working tree is dirty — commit before releasing" >&2
	git diff-index --name-only HEAD -- . ':!packaging' >&2
	exit 1
}

untracked=$(git ls-files --others --exclude-standard -- . ':!packaging')
[ -z "$untracked" ] || {
	echo "untracked files would be silently omitted from the tarball:" >&2
	echo "$untracked" >&2
	exit 1
}

git rev-parse -q --verify "v$pkgver" >/dev/null || {
	echo "no tag v$pkgver — tag the release first" >&2
	exit 1
}

out="polycarbon-$pkgver.tar.gz"
git archive --format=tar.gz --prefix="polycarbon-$pkgver/" "v$pkgver" -o "$out"
sum=$(sha256sum "$out" | cut -d' ' -f1)
sed -i "s/^sha256sums=.*/sha256sums=('$sum')/" packaging/PKGBUILD

# The AUR git repo carries this tarball, and aurweb rejects blobs over 250 KB.
bytes=$(stat -c %s "$out")
echo "$out  ${bytes} bytes"
echo "sha256 $sum  (written into packaging/PKGBUILD)"
[ "$bytes" -lt 256000 ] || {
	echo "WARNING: over the 250 KB aurweb blob limit — the push will be rejected" >&2
	exit 1
}
echo
echo "next: copy $out, packaging/PKGBUILD and packaging/polycarbon.install into"
echo "      the AUR checkout, regenerate .SRCINFO with"
echo "      'makepkg --printsrcinfo > .SRCINFO', commit, push."
