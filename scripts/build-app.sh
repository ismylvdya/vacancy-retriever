#!/bin/bash
# Собирает «Vacancy Retriever.app» — обычное приложение macOS для Launchpad и «Объектов входа».
# Приложение ничего не содержит само: оно запускает scripts/start.sh этой папки проекта
# (сервер + иконка в меню-баре). Если всё уже запущено — открывает ленту в браузере.
#   scripts/build-app.sh            — собрать в build/ и скопировать в ~/Applications
#   scripts/build-app.sh --no-install — только собрать в build/
# Если папку проекта переместить — пересобрать (путь к проекту зашит в приложение).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="Vacancy Retriever"
APP="$ROOT/build/$NAME.app"
ICON_SRC="$ROOT/packaging/macos/icon.png"
VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$ROOT/src/vacancy_retriever/app_info.py")"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- иконка: PNG 1024×1024 → AppIcon.icns (встроенные sips и iconutil) ---
ICONSET="$ROOT/build/AppIcon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$ICONSET"

# --- Info.plist: LSUIElement — без значка в Dock, как у обычных утилит меню-бара ---
cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>com.ismylvdya.vacancy-retriever</string>
  <key>CFBundleVersion</key><string>${VERSION:-0}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION:-0}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
  <key>LSArchitecturePriority</key><array><string>arm64</string><string>x86_64</string></array>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict>
</plist>
PLIST

# --- запускалка: чистое окружение (без ANTHROPIC_*/CLAUDE_*), вывод — в logs/launcher.log ---
cat >"$APP/Contents/MacOS/launcher" <<LAUNCHER
#!/bin/bash
ROOT="$ROOT"
PORT=18765
LOG="\$ROOT/logs/launcher.log"
mkdir -p "\$ROOT/logs"
alive() { [[ -s "\$1" ]] && kill -0 "\$(cat "\$1")" 2>/dev/null; }
# Скрипт без собственного бинарника macOS может запустить под Rosetta (x86_64), а .venv собран под arm64.
ARCH=()
[[ "\$(sysctl -n hw.optional.arm64 2>/dev/null)" == 1 ]] && ARCH=(/usr/bin/arch -arm64)
if alive "\$ROOT/data/server.pid" && alive "\$ROOT/data/menubar.pid"; then
  # Уже запущено — открыть ленту (браузер и шорткат перед открытием — config/app.yaml).
  cd "\$ROOT" && "\${ARCH[@]}" "\$ROOT/.venv/bin/vacancy-retriever" open-feed --port "\$PORT" >>"\$LOG" 2>&1 \
    || open "http://127.0.0.1:\$PORT/"
  exit 0
fi
echo "--- \$(date '+%Y-%m-%d %H:%M:%S') launch" >>"\$LOG"
if ! "\${ARCH[@]}" env -i HOME="\$HOME" USER="\$USER" LOGNAME="\$USER" SHELL=/bin/zsh LANG=en_US.UTF-8 TMPDIR="\$TMPDIR" \\
    PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \\
    /bin/bash "\$ROOT/scripts/start.sh" --no-open >>"\$LOG" 2>&1; then
  osascript -e 'display alert "Vacancy Retriever could not start" message "Details: logs/launcher.log in the project folder."' >/dev/null
  exit 1
fi
LAUNCHER
chmod +x "$APP/Contents/MacOS/launcher"
echo "Собрано: $APP"

if [[ "${1:-}" != "--no-install" ]]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/$NAME.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "Установлено: ~/Applications/$NAME.app (появится в Launchpad)"
fi
