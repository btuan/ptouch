#!/usr/bin/env bash
# One-time setup on NixOS (no Docker, no system config changes): run from the repo root.
#  - Python deps with the system python (uv must not download its own: NixOS can't run it)
#  - libusb, zlib and libstdc++ from nixpkgs, linked into ./.nix (GC roots kept here, nothing
#    added to your profile). Pillow's prebuilt wheels need zlib + libstdc++; USB needs libusb.
set -euo pipefail
cd "$(dirname "$0")/.."
UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --python "$(command -v python3)"
mkdir -p .nix
nix build --out-link .nix/libusb1 nixpkgs#libusb1
nix build --out-link .nix/zlib nixpkgs#zlib
nix build --out-link .nix/libstdcxx nixpkgs#stdenv.cc.cc.lib   # creates .nix/libstdcxx-lib
echo "ok. Start with scripts/run-nixos.sh (USB printers need the lp group; see README)."
