#!/bin/bash
# cc-token uninstaller
set -euo pipefail

echo "cc-token uninstaller"
echo ""

# 1. Remove plugin
PLUGIN_DIR=$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || echo "$HOME/Library/Application Support/SwiftBar/plugins")
rm -f "$PLUGIN_DIR/cc-token.5m.py"
echo "✓ Plugin removed"

# 2. Remove config and cache
rm -rf ~/.config/cc-token
echo "✓ Config removed"

# 3. iCloud sync data
ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/cc-token"
if [ -d "$ICLOUD_DIR" ]; then
    echo ""
    echo "iCloud sync data found at:"
    echo "  $ICLOUD_DIR"
    echo "  This contains usage stats shared across your Macs."
    echo "  Removing it will delete sync data on ALL your machines."
    echo ""
    if (echo -n < /dev/tty) 2>/dev/null; then
        read -p "Remove iCloud sync data? (y/N) " -n 1 -r < /dev/tty
    else
        REPLY="n"
    fi
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$ICLOUD_DIR"
        echo "✓ iCloud data removed"
    else
        echo "  Kept (can remove manually later)"
    fi
fi

# 4. SwiftBar
echo ""
if [ -d "/Applications/SwiftBar.app" ]; then
    # Guard against `set -e -o pipefail` killing the script when the glob
    # expands to nothing (no .py files left, or $PLUGIN_DIR gone entirely).
    # Wrap `ls` in a subshell with `|| true` so the pipe never fails.
    REMAINING=$({ ls "$PLUGIN_DIR"/*.py 2>/dev/null || true; } | wc -l | tr -d ' ')
    if [ "$REMAINING" = "0" ]; then
        echo "SwiftBar has no other plugins installed."
    else
        echo "SwiftBar still has $REMAINING other plugin(s)."
    fi
    echo "SwiftBar is the menu bar engine that runs this plugin."
    echo "If you don't use it for anything else, you can remove it."
    echo ""
    if (echo -n < /dev/tty) 2>/dev/null; then
        read -p "Uninstall SwiftBar? (y/N) " -n 1 -r < /dev/tty
    else
        REPLY="n"
    fi
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        osascript -e 'quit app "SwiftBar"' 2>/dev/null || true
        sleep 1
        if command -v brew &>/dev/null; then
            brew uninstall --cask swiftbar 2>/dev/null || true
        fi
        rm -rf "/Applications/SwiftBar.app"
        rm -rf ~/Library/Application\ Support/SwiftBar
        rm -rf ~/Library/Caches/com.ameba.SwiftBar
        rm -rf ~/Library/Preferences/com.ameba.SwiftBar.plist
        defaults delete com.ameba.SwiftBar 2>/dev/null || true
        echo "✓ SwiftBar uninstalled"
    else
        echo "  SwiftBar kept"
    fi
fi

echo ""
echo "✓ cc-token fully uninstalled"
