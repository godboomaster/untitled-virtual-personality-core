#!/usr/bin/env bash
# Голосовая активация в приложении Android («Коннор, какая погода?»):
# качает распознаватель sherpa-onnx (AAR) и малую русскую потоковую модель.
# В git они не попадают (размер репозитория) — после клона запустить один раз
# перед `npm run apk`. Повторный запуск ничего не качает заново.
#
#   scripts/fetch_wake_model.sh
#
# Куда: web/android/app/libs/sherpa-onnx-<версия>.aar (~50 МБ, все ABI; в APK
# попадают только arm64-v8a и x86_64) и web/android/app/src/main/assets/wake-ru/
# (int8, ~28 МБ — едет внутри APK).
set -euo pipefail

SHERPA_VERSION="1.13.8"
MODEL_REPO="https://huggingface.co/alphacep/vosk-model-small-streaming-ru/resolve/main"
AAR_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/v${SHERPA_VERSION}/sherpa-onnx-${SHERPA_VERSION}.aar"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIBS="$ROOT/web/android/app/libs"
ASSETS="$ROOT/web/android/app/src/main/assets/wake-ru"
mkdir -p "$LIBS" "$ASSETS"

# Скачать url в dest, если файла ещё нет. Сначала во временный .part: обрыв не
# оставит битый файл, который следующий запуск принял бы за готовый
fetch() {
  local url="$1" dest="$2" min_bytes="$3"
  if [[ -s "$dest" ]] && (( $(wc -c <"$dest") >= min_bytes )); then
    echo "есть: ${dest#$ROOT/}"
    return
  fi
  echo "качаю: ${dest#$ROOT/}"
  curl -fL --retry 3 --connect-timeout 20 -o "$dest.part" "$url"
  if (( $(wc -c <"$dest.part") < min_bytes )); then
    echo "слишком маленький файл: $url" >&2
    rm -f "$dest.part"
    exit 1
  fi
  mv "$dest.part" "$dest"
}

fetch "$AAR_URL" "$LIBS/sherpa-onnx-${SHERPA_VERSION}.aar" 10000000
# Модель: энкодер/декодер/джойнер (int8), токены и словарь BPE для горячих слов
fetch "$MODEL_REPO/am-onnx/encoder.int8.onnx" "$ASSETS/encoder.int8.onnx" 10000000
fetch "$MODEL_REPO/am-onnx/decoder.int8.onnx" "$ASSETS/decoder.int8.onnx" 100000
fetch "$MODEL_REPO/am-onnx/joiner.int8.onnx" "$ASSETS/joiner.int8.onnx" 100000
fetch "$MODEL_REPO/lang/tokens.txt" "$ASSETS/tokens.txt" 1000
fetch "$MODEL_REPO/lang/unigram_500.vocab" "$ASSETS/unigram_500.vocab" 1000

echo "готово: модель голосовой активации и sherpa-onnx на месте"
