#!/usr/bin/env bash
# Сборка AbuHH в .app и .dmg
#
# Подпись здесь не формальность: macOS не показывает уведомления от
# неподписанных программ. Ad-hoc подписи (бесплатной) для личного
# использования достаточно.
set -euo pipefail

cd "$(dirname "$0")"
VENV="${VENV:-.venv}"
APP="dist/AbuHH.app"
DMG="dist/AbuHH.dmg"
VERSION="$(grep -m1 CFBundleShortVersionString HHAgent.spec | sed 's/[^0-9.]//g')"

echo "==> Иконка"
"$VENV/bin/python" make_icon.py

echo "==> Очистка прошлой сборки"
rm -rf build dist

echo "==> PyInstaller"
"$VENV/bin/pyinstaller" --noconfirm --clean HHAgent.spec

if [ ! -d "$APP" ]; then
  echo "❌ Приложение не собралось: $APP не найден"; exit 1
fi

echo "==> Подпись (ad-hoc)"
# --deep нужен, чтобы подписались вложенные бинарники (Python, драйвер Playwright)
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 | tail -3

echo "==> Проверка"
SIZE=$(du -sh "$APP" | cut -f1)
echo "    Размер: $SIZE"

echo "==> DMG"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "AbuHH $VERSION" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

# Промежуточные файлы PyInstaller удаляем: внутри build/ лежит AbuHH.pkg —
# это внутренний формат архива PyInstaller, а НЕ установщик. macOS пытается
# открыть его «Установщиком» и выдаёт ошибку -1, что сбивает с толку.
echo "==> Уборка промежуточных файлов"
rm -rf build

# Под Linux сборки нет — там запускают из исходников. Готовим папку целиком,
# чтобы «передать другу» было одним действием: архив + инструкция, которая
# ни на что не ссылается наружу.
LINUXDIR="for linux"
SRC="$LINUXDIR/abuhh-src.tar.gz"
if git rev-parse --git-dir >/dev/null 2>&1; then
  echo "==> Папка для Linux"
  rm -rf "$LINUXDIR"; mkdir -p "$LINUXDIR"
  git archive --format=tar.gz -o "$SRC" HEAD
  cp LINUX.md "$LINUXDIR/ЧИТАЙ МЕНЯ.md"
fi

echo
echo "✅ Готово"
echo "   Приложение: $APP"
echo "   Установщик: $DMG ($(du -sh "$DMG" | cut -f1))"
[ -f "$SRC" ] && echo "   Для Linux:  папка «${LINUXDIR}» ($(du -sh "$LINUXDIR" | cut -f1)) — передавать целиком"
echo
echo "   Устанавливать нужно ИМЕННО из $DMG"
echo
echo "Важно для тех, кому передаёте DMG: сборка подписана ad-hoc, а не"
echo "нотаризована Apple. При первом открытии macOS скажет, что приложение"
echo "нельзя проверить. С macOS 15 правый клик → «Открыть» этот запрет уже"
echo "не обходит — нужно Системные настройки → Конфиденциальность и"
echo "безопасность → «Открыть всё равно». Запасной вариант:"
echo "   xattr -dr com.apple.quarantine \"/Applications/AbuHH.app\""
echo
echo "Сборка только под Apple Silicon. Для Linux передавайте папку «${LINUXDIR}»."
