#!/bin/sh
# Install the reviewed BetterWright runtime outside the application checkout.
set -eu

VERSION=1.10.0
RUNTIME_DIR=${1:-/opt/kimi/betterwright}
NODE_BIN=${NODE_BIN:-/usr/bin/node}
NPM_BIN=${NPM_BIN:-/usr/bin/npm}

[ "$(id -u)" = 0 ] || { echo "run this installer as root" >&2; exit 2; }
[ -x "$NODE_BIN" ] || { echo "Node is missing: $NODE_BIN" >&2; exit 2; }
[ -x "$NPM_BIN" ] || { echo "npm is missing: $NPM_BIN" >&2; exit 2; }
"$NODE_BIN" -e '
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 18)) process.exit(1);
' || { echo "BetterWright requires Node >=22.18.0" >&2; exit 2; }

install -d -m 0755 -o root -g root "$RUNTIME_DIR"
"$NPM_BIN" install \
  --prefix "$RUNTIME_DIR" \
  --omit=dev --omit=optional --ignore-scripts --no-save \
  "betterwright@$VERSION"

PACKAGE_DIR="$RUNTIME_DIR/node_modules/betterwright"
ENTRY="$PACKAGE_DIR/dist/src/index.js"
CLI="$PACKAGE_DIR/dist/bin/betterwright.js"
[ -f "$ENTRY" ] && [ -f "$CLI" ] || {
  echo "installed BetterWright package is incomplete" >&2
  exit 3
}
INSTALLED_VERSION=$("$NODE_BIN" -e \
  'const p=require(process.argv[1]); process.stdout.write(p.version)' \
  "$PACKAGE_DIR/package.json")
[ "$INSTALLED_VERSION" = "$VERSION" ] || {
  echo "expected BetterWright $VERSION, got $INSTALLED_VERSION" >&2
  exit 3
}

cp "$NODE_BIN" "$RUNTIME_DIR/node"
chmod 0755 "$RUNTIME_DIR/node"
ln -sfn "node_modules/betterwright/dist/src/index.js" \
  "$RUNTIME_DIR/betterwright-entry.mjs"
# Chromium-fork installation resolves from the operating-system home while the
# rest of BetterWright resolves BETTERWRIGHT_HOME, so pin both to this immutable
# runtime root during setup.
HOME="$RUNTIME_DIR" BETTERWRIGHT_HOME="$RUNTIME_DIR" \
  "$RUNTIME_DIR/node" "$CLI" setup

CHROMIUM="$RUNTIME_DIR/.betterwright/chromium/linux-x64/betterchromium"
[ -x "$CHROMIUM" ] || {
  echo "BetterChromium setup did not produce $CHROMIUM" >&2
  exit 3
}
if command -v ldd >/dev/null 2>&1; then
  BETTERWRIGHT_MISSING_LIBS=$(ldd "$CHROMIUM" | awk '/not found/{print $1}')
  [ -z "$BETTERWRIGHT_MISSING_LIBS" ] || {
    echo "BetterChromium host libraries are missing:" >&2
    echo "$BETTERWRIGHT_MISSING_LIBS" >&2
    echo "Install the Chromium runtime packages documented in deploy/betterwright/README.md." >&2
    exit 3
  }
fi
BETTERWRIGHT_ENTRY="$RUNTIME_DIR/betterwright-entry.mjs" \
  "$RUNTIME_DIR/node" --input-type=module -e \
  'await import(process.env.BETTERWRIGHT_ENTRY)'

chown -R root:root "$RUNTIME_DIR"
# Archive extraction can leave the platform directory at 0700. The bot must be
# able to traverse/read the immutable runtime and execute Node/BetterChromium,
# while only root may modify it.
chmod -R a+rX,u+w,go-w "$RUNTIME_DIR"
echo "Installed BetterWright $VERSION in $RUNTIME_DIR"
