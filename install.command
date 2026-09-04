#!/bin/bash
# Novix Mac runner installer. Run it on the Mac that should do the Apple work:
#
#     bash install.command
#
# It asks for the token, installs one LaunchAgent per worker, starts them, and
# says whether Novix saw the check-in. Re-run it any time to change the token or
# the number of workers; it clears the previous install first.
set -e

# INSTALLED TO ~/novix, NEVER RUN FROM WHERE IT WAS DROPPED. ~/Desktop, ~/Documents
# and ~/Downloads are TCC-protected, and a launchd agent does NOT inherit the access
# the Terminal that started it has. So a runner left in an AirDropped folder reads
# "Operation not permitted" from launchd forever while working perfectly when a person
# runs it by hand, and KeepAlive turns that into a restart loop. Home root is not
# protected. Measured on a real mini, 2026-08-31.
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/novix"

echo "Novix Mac runner"
echo

RUNNER_SRC="$SRC/novix_mac_runner.py"
if [ ! -f "$RUNNER_SRC" ]; then
  # Finder appends " 1", " 2", ... when a newer download lands beside an older
  # file. The first real reinstall hit exactly that shape: all three files were in
  # the folder, but the installer called it incomplete because it knew only the
  # unsuffixed spelling. Adopt one unambiguous numbered copy; with several, stop and
  # show them rather than guessing which program should run unattended.
  shopt -s nullglob
  NUMBERED=("$SRC"/novix_mac_runner\ [0-9]*.py)
  shopt -u nullglob
  if [ "${#NUMBERED[@]}" -eq 1 ]; then
    RUNNER_SRC="${NUMBERED[0]}"
    echo "Using Finder's renamed copy: $(basename "$RUNNER_SRC")"
  else
    echo "novix_mac_runner.py is not next to this installer. Copy the whole folder over."
    if [ "${#NUMBERED[@]}" -gt 1 ]; then
      echo "More than one numbered copy is here; keep the newest one and name it novix_mac_runner.py:"
      printf '  %s\n' "${NUMBERED[@]}"
    fi
    exit 1
  fi
fi

if ! xcodebuild -version >/dev/null 2>&1; then
  echo "Xcode is not set up on this Mac yet. Run these, then start again:"
  echo "  sudo xcodebuild -license accept"
  echo "  sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
  echo "  xcodebuild -runFirstLaunch"
  exit 1
fi
echo "Xcode: $(xcodebuild -version | head -1)"

mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  cp "$RUNNER_SRC" "$DEST/novix_mac_runner.py"
  # Whichever name the README arrived under. It shipped as README.md and this line
  # asked only for README.txt, so the one file explaining the machine was the one
  # file the install left behind.
  for DOC in README.md README.txt; do
    [ -f "$SRC/$DOC" ] && cp "$SRC/$DOC" "$DEST/$DOC"
  done
  cp "$0" "$DEST/install.command" 2>/dev/null || true
  echo "Installed to $DEST"
fi
# AirDrop and any download stamp a quarantine flag; strip it from our own copy so
# nothing downstream refuses to read it.
xattr -d com.apple.quarantine "$DEST/novix_mac_runner.py" 2>/dev/null || true

read -r -s -p "Paste the NOVIX_MAC_RUNNER_TOKEN (it will not echo): " TOKEN
echo
[ -n "$TOKEN" ] || { echo "No token, nothing to do."; exit 1; }

read -r -p "How many jobs at once? [1]: " WORKERS
WORKERS="${WORKERS:-1}"
case "$WORKERS" in ''|*[!0-9]*) echo "That is not a number."; exit 1;; esac
[ "$WORKERS" -ge 1 ] || { echo "At least one."; exit 1; }

URL="${NOVIX_URL:-https://app.getnovix.ai}"
mkdir -p ~/Library/LaunchAgents

# Clear any previous install, however many workers it had, so lowering the count
# never leaves an orphan running.
for OLD in ~/Library/LaunchAgents/ai.getnovix.macrunner*.plist; do
  [ -e "$OLD" ] || continue
  launchctl unload "$OLD" 2>/dev/null || true
  rm -f "$OLD"
done
rm -f /tmp/novix-mac-runner*.log

i=1
while [ "$i" -le "$WORKERS" ]; do
  if [ "$WORKERS" -eq 1 ]; then LABEL="ai.getnovix.macrunner"; ARGS=""; LOG="/tmp/novix-mac-runner.log";
  else LABEL="ai.getnovix.macrunner.$i"; ARGS="<string>--worker</string><string>w$i</string>"; LOG="/tmp/novix-mac-runner-$i.log"; fi
  PLIST=~/Library/LaunchAgents/$LABEL.plist
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$DEST/novix_mac_runner.py</string>
    $ARGS
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NOVIX_URL</key><string>$URL</string>
    <key>NOVIX_MAC_RUNNER_TOKEN</key><string>$TOKEN</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PL
  chmod 600 "$PLIST"
  plutil -lint "$PLIST" >/dev/null
  launchctl load "$PLIST"
  echo "started $LABEL, logging to $LOG"
  i=$((i + 1))
done

echo
echo "Waiting for the first check-in..."
sleep 12
curl -s -m 20 "$URL/api/health" | python3 -c "
import json,sys
m = json.load(sys.stdin).get('macRunner') or {}
print('connected:', m.get('connected'), '| can build:', m.get('canBuild'))
for r in m.get('runners', []):
    print(' -', r.get('label'), r.get('id'), 'version=' + str(r.get('version')), r.get('capabilities'))
" 2>/dev/null || echo "Could not reach $URL."
echo
echo "Installed runner: $(/usr/bin/python3 "$DEST/novix_mac_runner.py" --version)"
echo "Future runner versions will install automatically between jobs."
echo "If connected is False, the reason is one line in the log:"
echo "  tail -5 $LOG"
