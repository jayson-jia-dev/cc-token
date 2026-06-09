#!/bin/bash
# cc-token installer & updater
# Install: curl -fsSL https://raw.githubusercontent.com/jayson-jia-dev/cc-token/main/install.sh | bash
# Update:  curl -fsSL https://raw.githubusercontent.com/jayson-jia-dev/cc-token/main/install.sh | bash -s -- --update
set -euo pipefail

REPO="https://raw.githubusercontent.com/jayson-jia-dev/cc-token/main"
PLUGIN_NAME="cc-token.5m.py"
VERSION="1.6.1"
UPDATE_MODE=false

# Parse args
for arg in "$@"; do
    case "$arg" in
        --update|-u) UPDATE_MODE=true ;;
    esac
done

if $UPDATE_MODE; then
    echo "cc-token updater v${VERSION}"
else
    echo "cc-token installer v${VERSION}"
fi
echo ""

# ─── 1. Check Claude Code ───
CLAUDE_DIR="$HOME/.claude"
if [ ! -d "$CLAUDE_DIR" ]; then
    echo "⚠ Claude Code not found at $CLAUDE_DIR"
    echo "  Install: https://claude.ai/download"
    echo ""
    if ! $UPDATE_MODE; then
        if (echo -n < /dev/tty) 2>/dev/null; then
            read -p "Continue anyway? (y/N) " -n 1 -r < /dev/tty
        else
            REPLY="y"
        fi
        echo ""
        [[ $REPLY =~ ^[Yy]$ ]] || exit 1
    fi
else
    echo "✓ Claude Code"
fi

# ─── 2. Check/Install SwiftBar ───
if [ -d "/Applications/SwiftBar.app" ]; then
    echo "✓ SwiftBar"
else
    echo "Installing SwiftBar..."
    if command -v brew &>/dev/null; then
        brew install --cask swiftbar
    else
        echo ""
        echo "SwiftBar is required. Options:"
        echo "  1. brew install --cask swiftbar"
        echo "  2. https://github.com/swiftbar/SwiftBar/releases"
        echo ""
        exit 1
    fi
fi

# ─── 3. Find/Create plugin directory ───
PLUGIN_DIR=$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || echo "")
if [ -z "$PLUGIN_DIR" ]; then
    PLUGIN_DIR="$HOME/Library/Application Support/SwiftBar/plugins"
    mkdir -p "$PLUGIN_DIR"
fi

# ─── 4. Clean up old/conflicting plugins ───
for old in "$PLUGIN_DIR"/ccpeek* "$PLUGIN_DIR"/cc-pulse*; do
    [ -f "$old" ] && rm -f "$old" && echo "✓ Removed: $(basename "$old")"
done

# ─── 5. Download plugin (atomic: tmp + mv, SHA256-verified) ───
# Non-atomic 'curl -o target' leaves a half-written file on network failure,
# which SwiftBar will still try to execute (→ visible Python syntax errors
# in the menu bar). Download to a hidden tmp in the same dir, then rename.
# The tmp name lacks the '.5m.' refresh pattern so SwiftBar ignores it.
# SHA256 verification matches auto_update()'s integrity check so first
# install and subsequent updates trust the same source of truth.
echo "Downloading latest plugin..."
TMP_PLUGIN="$PLUGIN_DIR/.cc-token.download.$$"
TMP_SUM="$PLUGIN_DIR/.cc-token.sum.$$"
trap 'rm -f "$TMP_PLUGIN" "$TMP_SUM"' EXIT
curl -fsSL "${REPO}/${PLUGIN_NAME}?v=${VERSION}" -o "$TMP_PLUGIN"

# Verify SHA256 (best-effort: skip if checksum file unreachable, which
# can happen on older versions or transient network; the curl already
# used HTTPS so we're not regressing on security, just adding a layer).
if curl -fsSL "${REPO}/checksum.sha256?v=${VERSION}" -o "$TMP_SUM" 2>/dev/null; then
    EXPECTED=$(awk '{print $1}' "$TMP_SUM")
    ACTUAL=$(shasum -a 256 "$TMP_PLUGIN" | awk '{print $1}')
    if [ -n "$EXPECTED" ] && [ "$EXPECTED" != "$ACTUAL" ]; then
        echo "✗ Checksum mismatch!"
        echo "  expected: $EXPECTED"
        echo "  got:      $ACTUAL"
        echo "  Refusing to install. File left at $TMP_PLUGIN for inspection."
        trap - EXIT
        exit 1
    fi
    echo "✓ SHA256 verified"
fi

chmod +x "$TMP_PLUGIN"
mv "$TMP_PLUGIN" "$PLUGIN_DIR/$PLUGIN_NAME"
echo "✓ Plugin installed"

# ─── 6. Create config (skip in update mode or if exists) ───
CONFIG_DIR="$HOME/.config/cc-token"
CONFIG_FILE="$CONFIG_DIR/config.json"
if [ -f "$CONFIG_FILE" ]; then
    echo "✓ Config preserved: $CONFIG_FILE"
elif $UPDATE_MODE; then
    echo "⚠ No config found, creating default..."
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG_FILE" << CFGEOF
{
  "claude_dir": "$HOME/.claude",
  "sync_mode": "auto",
  "subscription": 0,
  "subscription_label": "",
  "language": "auto",
  "machine_labels": {},
  "menu_bar_icon": "sfSymbol=sparkles.rectangle.stack"
}
CFGEOF
else
    mkdir -p "$CONFIG_DIR"

    # Interactive subscription picker
    SUB_PRICE=0
    SUB_LABEL=""
    echo ""
    echo "What's your Claude subscription?"
    echo "   1) Pro (\$20/mo)"
    echo "   2) Max 5x (\$100/mo)"
    echo "   3) Max 20x (\$200/mo)"
    echo "   4) Team (\$30/mo)"
    echo "   5) API only / Skip"
    echo ""
    if (echo -n < /dev/tty) 2>/dev/null; then
        read -p "Choose [1-5, default 5]: " -n 1 -r SUB_CHOICE < /dev/tty
    else
        SUB_CHOICE="5"
    fi
    echo ""
    case "$SUB_CHOICE" in
        1) SUB_PRICE=20;  SUB_LABEL="Pro" ;;
        2) SUB_PRICE=100; SUB_LABEL="Max" ;;
        3) SUB_PRICE=200; SUB_LABEL="Max" ;;
        4) SUB_PRICE=30;  SUB_LABEL="Team" ;;
        *) SUB_PRICE=0;   SUB_LABEL="" ;;
    esac
    [ "$SUB_PRICE" -gt 0 ] 2>/dev/null && echo "✓ $SUB_LABEL \$$SUB_PRICE/mo" || echo "✓ Skipped (edit config.json later)"

    cat > "$CONFIG_FILE" << CFGEOF
{
  "claude_dir": "$HOME/.claude",
  "sync_mode": "auto",
  "subscription": $SUB_PRICE,
  "subscription_label": "$SUB_LABEL",
  "language": "auto",
  "machine_labels": {},
  "menu_bar_icon": "sfSymbol=sparkles.rectangle.stack"
}
CFGEOF
    echo "✓ Config: $CONFIG_FILE"

    ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
    [ -d "$ICLOUD_DIR" ] && echo "✓ iCloud Drive — multi-machine sync enabled"
fi

# ─── 7. Launch SwiftBar ───
if ! defaults read com.ameba.SwiftBar PluginDirectory &>/dev/null; then
    defaults write com.ameba.SwiftBar PluginDirectory -string "$PLUGIN_DIR"
fi
if ! pgrep -q SwiftBar; then
    echo "Launching SwiftBar..."
    open -a SwiftBar
    sleep 2
fi

echo ""
if $UPDATE_MODE; then
    echo "✓ Updated to v${VERSION}!"
else
    echo "✓ cc-token v${VERSION} installed!"
fi
echo ""
echo "   Config: $CONFIG_FILE"
echo "   Plugin: $PLUGIN_DIR/$PLUGIN_NAME"
echo "   Repo:   https://github.com/jayson-jia-dev/cc-token"
echo ""
echo "To update later:  curl -fsSL ${REPO}/install.sh | bash -s -- --update"
