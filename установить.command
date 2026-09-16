#!/bin/zsh
# minimalma — первая настройка и ярлык в один двойной клик.

set -e
ROOT="${0:A:h}"
APP="$ROOT/minimalma.app"
DESKTOP="$HOME/Desktop"
LINK="$DESKTOP/minimalma.app"

echo "minimalma — установка"
echo "  папка: $ROOT"
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
fi

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then
  "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true
fi

mkdir -p "$DESKTOP"
if [ -L "$LINK" ] || [ ! -e "$LINK" ]; then
  ln -sfn "$APP" "$LINK"
  echo "ярлык: $LINK"
else
  echo "ярлык: уже существует $LINK"
fi
echo
echo "готово. запускаю…"
open "$APP"
