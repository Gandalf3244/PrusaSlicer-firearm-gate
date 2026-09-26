#!/usr/bin/env bash
# Downloads the Mini404 emulator (QEMU with Prusa boards, https://github.com/vintagepc/MINI404) and
# the three shared libraries it needs that a desktop Ubuntu may lack, into $FIREARM_EMU_DIR
# (default ~/.cache/firearm-check-emulator). No root: the libraries are unpacked from `apt download`.
set -euo pipefail

DIR="${FIREARM_EMU_DIR:-$HOME/.cache/firearm-check-emulator}"
URL=https://github.com/vintagepc/MINI404/releases/download/dev/Mini404-dev-linux.tar.bz2
SHA256=3761b1a3f016ab816e247098b1a42e7f5edb20cfb1a90252403f4645be7cd864   # the build tested on 2026-09-26
QEMU=Mini404-dev-linux/qemu-system-buddy
LIBS="$DIR/libs/usr/lib/x86_64-linux-gnu"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL="$HERE/downloads"          # optional offline copies: Mini404-dev-linux.tar.bz2, debs/*.deb

mkdir -p "$DIR"
cd "$DIR"

if [ ! -x "$QEMU" ]; then
    if [ -f "$LOCAL/Mini404-dev-linux.tar.bz2" ]; then
        cp "$LOCAL/Mini404-dev-linux.tar.bz2" mini404.tar.bz2
    else
        curl -fL -o mini404.tar.bz2 "$URL"
    fi
    if ! echo "$SHA256  mini404.tar.bz2" | sha256sum -c --quiet -; then
        echo "warning: the Mini404 'dev' build changed since this test was written; it may behave differently" >&2
    fi
    tar xjf mini404.tar.bz2
fi

missing() { LD_LIBRARY_PATH="$LIBS" ldd "$QEMU" | awk '/not found/ {print $1}'; }

if [ -n "$(missing)" ]; then
    mkdir -p debs
    if compgen -G "$LOCAL/debs/*.deb" > /dev/null; then
        cp "$LOCAL"/debs/*.deb debs/
    else
        (cd debs && for p in libglut3.12 libglew2.2 libslirp0; do apt download "$p"; done)
    fi
    for d in debs/*.deb; do dpkg-deb -x "$d" libs; done
fi

if [ -n "$(missing)" ]; then
    echo "still missing: $(missing | tr '\n' ' ')" >&2
    exit 1
fi
echo "Mini404 ready in $DIR"
