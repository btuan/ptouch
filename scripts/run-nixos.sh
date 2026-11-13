#!/usr/bin/env bash
# Start the ptlabel server on NixOS after scripts/setup-nixos.sh. Keep it running with e.g.
#   tmux new -d -s ptlabel 'sg lp -c ~/ptlabel/scripts/run-nixos.sh'
# (`sg lp` gives the process the lp group for USB without logging out and back in).
# Settings and secrets go in ptlabel.env in the repo root (chmod 600, git-ignored), e.g.
#   ANTHROPIC_API_KEY=...   PTLABEL_PRINTER=192.0.2.10   PTLABEL_TOKEN=...   PTLABEL_PORT=8765
set -euo pipefail
cd "$(dirname "$0")/.."
here=$PWD
export PTLABEL_LIBUSB="$here/.nix/libusb1/lib/libusb-1.0.so.0"
export PTLABEL_DB="${PTLABEL_DB:-$here/data/ptlabel.db}"
# NixOS has no /usr/lib: Pillow's prebuilt binaries need zlib and libstdc++ from the store
export LD_LIBRARY_PATH="$here/.nix/zlib/lib:$here/.nix/libstdcxx-lib/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p "$(dirname "$PTLABEL_DB")"
set -a; [ -f ptlabel.env ] && . ./ptlabel.env; set +a
exec .venv/bin/ptlabel serve --host "${PTLABEL_HOST:-0.0.0.0}" --port "${PTLABEL_PORT:-8765}"
