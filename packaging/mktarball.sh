#!/bin/sh
# Cut a release: push the tag, then stamp the checksum of the archive GitHub
# generates for it into the PKGBUILD.
#
# This script used to BUILD a tarball locally and commit it into the AUR repo.
# That is what got polycarbon deleted from the AUR — "no source url, sources
# hosted on AUR". The AUR carries build recipes; the source has to be
# downloadable from a real upstream. So the tarball is no longer produced here
# at all: the PKGBUILD points at the tag archive and this only records its hash.
#
# GitHub's archive honours .gitattributes export-ignore, so packaging/ stays out
# of it exactly as it did before — the contents are unchanged, only the host is.
set -eu

pkgver="${1:?usage: mktarball.sh <version>   e.g. mktarball.sh 1.0.2}"
cd "$(dirname "$0")/.."

repo=$(git config --get remote.origin.url | sed 's|.*github.com[:/]||; s|\.git$||')
[ -n "$repo" ] || { echo "no origin remote — the source must be hosted somewhere" >&2; exit 1; }

# packaging/ is skipped in both checks: it is export-ignored so it never reaches
# the archive, and this script's own last act modifies it.
git diff-index --quiet HEAD -- . ':!packaging' || {
	echo "working tree is dirty — commit before releasing" >&2
	git diff-index --name-only HEAD -- . ':!packaging' >&2
	exit 1
}

untracked=$(git ls-files --others --exclude-standard -- . ':!packaging')
[ -z "$untracked" ] || {
	echo "untracked files would be missing from the archive:" >&2
	echo "$untracked" >&2
	exit 1
}

git rev-parse -q --verify "v$pkgver" >/dev/null || {
	echo "no tag v$pkgver — tag the release first" >&2
	exit 1
}

# The tag has to be on the remote before GitHub can serve an archive for it.
git push origin "v$pkgver" 2>/dev/null || true

url="https://github.com/$repo/archive/refs/tags/v$pkgver.tar.gz"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "fetching $url"
curl -fsSL -o "$tmp/src.tar.gz" "$url" || {
	echo "could not fetch the tag archive — is the tag pushed and the repo public?" >&2
	exit 1
}

# Prove the archive is the shape the PKGBUILD expects before recording its hash.
top=$(tar tzf "$tmp/src.tar.gz" | head -1)
[ "$top" = "polycarbon-$pkgver/" ] || {
	echo "archive top-level dir is '$top', expected 'polycarbon-$pkgver/'" >&2
	exit 1
}

sum=$(sha256sum "$tmp/src.tar.gz" | cut -d' ' -f1)
sed -i "s/^sha256sums=.*/sha256sums=('$sum')/" packaging/PKGBUILD
echo "sha256 $sum  (written into packaging/PKGBUILD)"
echo
echo "next: copy packaging/PKGBUILD and packaging/polycarbon.install into the AUR"
echo "      checkout, regenerate .SRCINFO with 'makepkg --printsrcinfo > .SRCINFO',"
echo "      commit and push. Do NOT add a tarball to the AUR repo."
