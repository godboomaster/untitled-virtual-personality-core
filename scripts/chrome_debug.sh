#!/bin/bash
# Ручной запуск Chrome пула H (веб-чаты) с CDP-отладкой — запасной путь и
# rescue-режим: решить капчу/перелогиниться руками в профиле бота.
#
# С web_extended браузер бота разделён на пулы (docs/idea-headless-browser-split.md):
#   пул H — headless Chrome, профиль vpc-browser-profile (логины веб-чатов),
#           порт 9223, запускается ботом лениво и держится постоянно;
#   пул V — headed Chrome, профиль vpc-browser-profile-headed (копия H),
#           порт 9222, живёт по требованию (режим управления + idle 20 мин).
# Этот скрипт поднимает профиль пула H ВИДИМЫМ (без --headless) на его порту.
#
# Профильный Chrome должен быть ПОЛНОСТЬЮ закрыт перед запуском (⌘Q в его
# окне), иначе флаг не применится. Бот умеет забирать профиль сам
# (_check_profile_lock/_kill_chrome_on_profile), так что скрипт нужен лишь
# как ручной запасной путь.
#
# Порт слушает только localhost, но любой локальный процесс через него
# управляет браузером — держите отладку включённой на время использования.
#
# ФЛАГИ, ПРОФИЛЬ, ПОРТ И БИНАРЬ НЕ ДУБЛИРУЮТСЯ ЗДЕСЬ: их отдаёт сам код бота
# (app/features/browser_actions.py — CHROME_THRIFT_FLAGS,
# CHROME_NO_THROTTLE_FLAGS, _resolve_executable, _pool_h_profile,
# _pool_h_cdp_url). Своя копия списка неизбежно расходилась с кодом: в скрипте
# годами жил --mute-audio, которого в CHROME_THRIFT_FLAGS давно нет (в этом
# браузере пользователь реально слушает музыку), и не было флагов против
# троттлинга фоновых вкладок. Модуль импортируется на голой стандартной
# библиотеке — сторонних зависимостей для этого не нужно.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"

ARGV_FILE="$(mktemp -t vpc_chrome_argv)"
trap 'rm -f "$ARGV_FILE"' EXIT

if ! PYTHONPATH="$REPO" "$PY" - >"$ARGV_FILE" <<'PYEOF'
import sys
from urllib.parse import urlparse

from app.features.browser_actions import (
    CHROME_NO_THROTTLE_FLAGS, CHROME_THRIFT_FLAGS, _pool_h_cdp_url,
    _pool_h_profile, _resolve_executable)

exe = _resolve_executable()
if not exe:
    sys.stderr.write("не найден ни один Chromium-браузер (Chrome, Edge, Opera, "
                     "Яндекс, Brave, Vivaldi)\n")
    raise SystemExit(1)
port = urlparse(_pool_h_cdp_url()).port or 9223
# Ровно те же флаги, что у бота в _launch_pool_h_chrome, минус headless-маска:
# скрипт поднимает пул H ВИДИМЫМ (для этого его и запускают руками)
argv = [exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={_pool_h_profile()}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        *CHROME_THRIFT_FLAGS, *CHROME_NO_THROTTLE_FLAGS,
        "--window-size=1920,1080", "about:blank"]
sys.stdout.write("\n".join(argv) + "\n")
PYEOF
then
    echo "Не удалось получить флаги запуска из app/features/browser_actions.py." >&2
    echo "Запусти из корня репозитория интерпретатором с доступом к проекту" >&2
    echo "(PYTHON=/usr/local/bin/python3 scripts/chrome_debug.sh)." >&2
    exit 1
fi

ARGS=""
CMD=()
while IFS= read -r line; do
    [ -n "$line" ] || continue
    CMD+=("$line")
    ARGS="$ARGS $line"
done <"$ARGV_FILE"

if [ ${#CMD[@]} -eq 0 ]; then
    echo "Пустой список аргументов запуска — нечего делать." >&2
    exit 1
fi

echo "Запускаю:$ARGS"
"${CMD[@]}" >/dev/null 2>&1 &
disown 2>/dev/null || true
