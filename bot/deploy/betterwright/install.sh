#!/bin/sh
# Atomically install the reviewed browser and visual-rendering runtime.
set -eu

VERSION=1.10.2
MERMAID_VERSION=11.17.2
EXPECTED_RUNTIME_DIR=/opt/kimi/betterwright
RUNTIME_INPUT=${1:-$EXPECTED_RUNTIME_DIR}
NODE_BIN=${NODE_BIN:-/usr/bin/node}
NPM_BIN=${NPM_BIN:-/usr/bin/npm}
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
[ "$#" -le 1 ] || {
  echo "usage: $0 [/opt/kimi/betterwright]" >&2
  exit 2
}
RUNTIME_DIR=$(realpath -m -- "$RUNTIME_INPUT") || {
  echo "could not resolve runtime directory: $RUNTIME_INPUT" >&2
  exit 2
}
[ "$RUNTIME_DIR" = "$EXPECTED_RUNTIME_DIR" ] || {
  echo "refusing runtime directory outside $EXPECTED_RUNTIME_DIR: $RUNTIME_INPUT" >&2
  exit 2
}
PARENT_DIR=$(dirname -- "$RUNTIME_DIR")
RUNTIME_NAME=$(basename -- "$RUNTIME_DIR")
STAGING_DIR="$PARENT_DIR/.${RUNTIME_NAME}.staging.$$"
BACKUP_DIR="$PARENT_DIR/.${RUNTIME_NAME}.backup.$$"

[ "$(id -u)" = 0 ] || { echo "run this installer as root" >&2; exit 2; }
[ -x "$NODE_BIN" ] || { echo "Node is missing: $NODE_BIN" >&2; exit 2; }
[ -x "$NPM_BIN" ] || { echo "npm is missing: $NPM_BIN" >&2; exit 2; }
"$NODE_BIN" -e '
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 18)) process.exit(1);
' || { echo "BetterWright requires Node >=22.18.0" >&2; exit 2; }

cleanup() {
  rm -rf -- "$STAGING_DIR"
  if [ -e "$BACKUP_DIR" ] && [ ! -e "$RUNTIME_DIR" ]; then
    mv -- "$BACKUP_DIR" "$RUNTIME_DIR"
  fi
}
trap cleanup 0 1 2 15

install -d -m 0755 -o root -g root "$PARENT_DIR"
rm -rf -- "$STAGING_DIR" "$BACKUP_DIR"
install -d -m 0755 -o root -g root "$STAGING_DIR"
install -m 0644 -o root -g root \
  "$SCRIPT_DIR/package.json" "$SCRIPT_DIR/package-lock.json" "$STAGING_DIR/"
"$NPM_BIN" ci \
  --prefix "$STAGING_DIR" \
  --omit=dev --omit=optional --ignore-scripts

PACKAGE_DIR="$STAGING_DIR/node_modules/betterwright"
MERMAID_PACKAGE_DIR="$STAGING_DIR/node_modules/mermaid"
MERMAID_BUNDLE="$STAGING_DIR/node_modules/mermaid/dist/mermaid.min.js"
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
[ -f "$MERMAID_BUNDLE" ] || {
  echo "installed Mermaid package is incomplete: missing $MERMAID_BUNDLE" >&2
  exit 3
}
INSTALLED_MERMAID_VERSION=$(
  "$NODE_BIN" -e \
    'const p=require(process.argv[1]); process.stdout.write(p.version)' \
    "$MERMAID_PACKAGE_DIR/package.json"
)
[ "$INSTALLED_MERMAID_VERSION" = "$MERMAID_VERSION" ] || {
  echo "expected Mermaid $MERMAID_VERSION, got $INSTALLED_MERMAID_VERSION" >&2
  exit 3
}

cp "$NODE_BIN" "$STAGING_DIR/node"
chmod 0755 "$STAGING_DIR/node"
ln -s "node_modules/betterwright/dist/src/index.js" \
  "$STAGING_DIR/betterwright-entry.mjs"
# Chromium-fork installation resolves from the operating-system home while the
# rest of BetterWright resolves BETTERWRIGHT_HOME, so point both at the staging
# root. The completed tree is renamed into place only after every probe passes.
HOME="$STAGING_DIR" BETTERWRIGHT_HOME="$STAGING_DIR" \
  "$STAGING_DIR/node" "$CLI" setup

CHROMIUM="$STAGING_DIR/.betterwright/chromium/linux-x64/betterchromium"
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
BETTERWRIGHT_ENTRY="$STAGING_DIR/betterwright-entry.mjs" \
  "$STAGING_DIR/node" --input-type=module -e \
  'await import(process.env.BETTERWRIGHT_ENTRY)'

chown -R root:root "$STAGING_DIR"
# Archive extraction can leave the platform directory at 0700. The bot must be
# able to traverse/read the immutable runtime and execute Node/BetterChromium,
# while only root may modify it.
chmod -R a+rX,u+w,go-w "$STAGING_DIR"

if [ -e "$RUNTIME_DIR" ]; then
  mv -- "$RUNTIME_DIR" "$BACKUP_DIR"
fi
mv -- "$STAGING_DIR" "$RUNTIME_DIR"
rm -rf -- "$BACKUP_DIR"
trap - 0 1 2 15

echo "Installed BetterWright $VERSION and Mermaid $MERMAID_VERSION in $RUNTIME_DIR"
