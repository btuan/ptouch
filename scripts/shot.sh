#!/bin/sh
# Headless screenshots of the running UI: scripts/shot.sh OUTDIR [URL]
out=${1:-out/shots}; url=${2:-http://127.0.0.1:8765/}
mkdir -p "$out"
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
for size in 1440,900 1024,768 390,844; do
  mode=--headless=new; [ "${size%,*}" -lt 500 ] && mode=--headless=old
  "$chrome" $mode --disable-gpu --hide-scrollbars --window-size=$size \
    --virtual-time-budget=4000 --screenshot="$out/ui-${size%,*}.png" "$url" >/dev/null 2>&1
done
ls "$out"
