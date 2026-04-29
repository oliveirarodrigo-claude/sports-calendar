#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/cruzeiro_calendar.py"
ICS_PATH="$SCRIPT_DIR/cruzeiro.ics"
PLIST_LABEL="com.cruzeiro.calendar"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

# ── 1. Get API key ─────────────────────────────────────────────────────────────
if [ -z "$API_FOOTBALL_KEY" ]; then
    echo ""
    echo "Paste your API-Football key (from dashboard.api-football.com):"
    read -r API_FOOTBALL_KEY
fi

if [ -z "$API_FOOTBALL_KEY" ]; then
    echo "ERROR: No API key provided. Exiting."
    exit 1
fi

# ── 2. Check Python 3 ─────────────────────────────────────────────────────────
PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "Python 3 not found. Install it from python.org and re-run."
    exit 1
fi
echo "Using Python: $($PYTHON --version)"

# ── 3. Run the script once to verify everything works ─────────────────────────
echo ""
echo "Running calendar fetch..."
API_FOOTBALL_KEY="$API_FOOTBALL_KEY" "$PYTHON" "$SCRIPT_PATH"

# ── 4. Create LaunchAgent plist (runs every 6 hours) ──────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_PATH</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>API_FOOTBALL_KEY</key>
        <string>$API_FOOTBALL_KEY</string>
    </dict>
    <key>StartInterval</key>
    <integer>21600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/cruzeiro_calendar.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/cruzeiro_calendar.log</string>
</dict>
</plist>
PLIST

# ── 5. Register with launchd ───────────────────────────────────────────────────
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo ""
echo "✅ Done! The calendar updates automatically every 6 hours."
echo ""
echo "To subscribe in Apple Calendar:"
echo "  1. Open Calendar app"
echo "  2. File → New Calendar Subscription"
echo "  3. Paste this path:"
echo "     file://$ICS_PATH"
echo "  4. Set auto-refresh to 'Every Hour'"
echo "  5. Click Subscribe"
