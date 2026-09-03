#!/bin/bash
# Chrome с CDP-отладкой для браузерных рецептов (computer_control, этап 3b).
#
# Запускается ВЫДЕЛЕННЫЙ automation-профиль (тот же, что поднимает сам бот,
# см. _DEFAULT_PROFILES в browser_actions.py) — иначе на порту 9222 может
# оказаться основной профиль без учёток веб-чатов, и бот подключится к нему.
#
# Этот профильный Chrome должен быть ПОЛНОСТЬЮ закрыт перед запуском (⌘Q в
# его окне), иначе флаг не применится — новое окно откроется в уже запущенном
# процессе без отладки. Впрочем, бот умеет забирать такой профиль сам
# (_try_reclaim_profile), так что скрипт нужен лишь как ручной запасной путь.
#
# Порт слушает только localhost, но любой локальный процесс через него
# управляет браузером — держите отладку включённой на время использования.
PROFILE="$HOME/Library/Application Support/vpc-browser-profile"
# Флаги экономии ресурсов — те же, что в browser_actions.CHROME_THRIFT_FLAGS
# (фоновая сеть/синк/переводчик/каст в автоматизационном профиле не нужны).
open -na "Google Chrome" --args --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
  --disable-background-networking --disable-component-update --disable-sync \
  --metrics-recording-only --mute-audio \
  --disable-features=Translate,MediaRouter,OptimizationHints \
  --force-color-profile=srgb
