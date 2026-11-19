#!/bin/sh
# Подпись панели на macOS постоянным локальным ключом.
#
# Зачем: tauri build подписывает приложение «на лету» (adhoc), и macOS узнаёт
# его по хешу самого файла. После каждой сборки хеш другой — для системы это
# новое приложение, и доступ к микрофону и к папке «Документы» она
# спрашивает заново. С постоянным ключом приложение узнаётся по ключу и
# идентификатору: разрешили один раз — и после пересборок не спрашивает.
#
# Ключ создаётся при первом запуске — свой самоподписанный, только для
# подписи этого приложения на этом компьютере (ни Apple, ни другие компьютеры
# ему не доверяют, и не нужно). Лежит в отдельной связке ключей в папке
# настроек панели. codesign ищет ключ только в связках из списка
# пользователя — на время подписи эта связка добавляется туда, потом список
# возвращается как был; связка «Вход» не меняется. Удалить — стереть папку
# SIGN_DIR (тогда macOS спросит разрешения ещё раз).
#
# Запуск: sh scripts/macos-sign.sh [--verify|--check-stopped] [путь к .app]
# npm run build:mac — сборка и подпись; npm run sign:mac — восстановить подпись.
set -eu

MODE=sign
case "${1:-}" in
  --verify) MODE=verify; shift ;;
  --check-stopped) MODE=check-stopped; shift ;;
esac
APP="${1:-src-tauri/target/release/bundle/macos/Virtual Persona.app}"
IDENT="dev.virtualpersona.panel"
NAME="Virtual Persona Local Signing"
SIGN_DIR="$HOME/Library/Application Support/$IDENT/signing"
KC="$SIGN_DIR/signing.keychain-db"
PASS_FILE="$SIGN_DIR/keychain-password"

if [ ! -d "$APP" ]; then
  # Первая сборка: приложения ещё нет, останавливать нечего.
  [ "$MODE" = check-stopped ] && exit 0
  echo "Нет приложения: $APP" >&2
  exit 1
fi
APP=$(cd "$APP" && pwd -P)
BUNDLE_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")
[ "$BUNDLE_ID" = "$IDENT" ] || { echo "Чужой идентификатор приложения: $BUNDLE_ID" >&2; exit 1; }

if [ "$MODE" != verify ]; then
  # У запущенного процесса остаётся прежняя подпись в памяти. Перезапись
  # бинаря/подписи под работающей панелью снова ломает проверку TCC.
  EXECUTABLE=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Contents/Info.plist")
  PROCESSES=$(ps -axo comm=)
  if printf '%s\n' "$PROCESSES" | grep -Fqx "$APP/Contents/MacOS/$EXECUTABLE"; then
    echo 'Сначала закройте Virtual Persona через «Выход» в меню трея, затем повторите команду.' >&2
    exit 1
  fi
fi
[ "$MODE" != check-stopped ] || exit 0

certificate_hash() {
  security find-certificate -c "$NAME" -Z "$KC" | awk '/^SHA-1 hash:/ {print $3; exit}'
}
verify_signature() {
  # Обычная --verify принимает и adhoc-подпись. Проверяем также тот самый
  # сертификат и идентификатор, к которым привязаны выданные разрешения.
  codesign --verify --strict --verbose=2 \
    -R="identifier \"$IDENT\" and certificate leaf = H\"$HASH\"" "$APP"
}
if [ "$MODE" = verify ]; then
  HASH=$(certificate_hash)
  [ -n "$HASH" ] || { echo "В $KC нет ключа «${NAME}»" >&2; exit 1; }
  verify_signature
  exit 0
fi

# Список связок пользователя: запомнить и вернуть. set_list [связка] — тот
# же список, с этой связкой первой (пути — с пробелами, поэтому построчно)
LIST_BEFORE=$(security list-keychains -d user)
set_list() {
  first="${1:-}"
  set --
  [ -n "$first" ] && set -- "$first"
  while IFS= read -r line; do
    set -- "$@" "$(printf '%s' "$line" | sed 's/^ *"//; s/"$//')"
  done <<EOF
$LIST_BEFORE
EOF
  security list-keychains -d user -s "$@"
}
restore_list() {
  [ "$(security list-keychains -d user)" = "$LIST_BEFORE" ] || set_list
}

if [ ! -f "$KC" ]; then
  echo "Создаю локальный ключ подписи (один раз): $SIGN_DIR"
  mkdir -p "$SIGN_DIR"
  chmod 700 "$SIGN_DIR"
  PASS=$(openssl rand -hex 24)
  printf '%s' "$PASS" > "$PASS_FILE"
  chmod 600 "$PASS_FILE"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"; restore_list' EXIT
  openssl req -x509 -newkey rsa:2048 -nodes -days 7300 -subj "/CN=$NAME" \
    -addext "basicConstraints=critical,CA:false" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=critical,codeSigning" \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
  # -legacy: связки macOS не читают PKCS#12 с шифрами OpenSSL 3 по умолчанию
  openssl pkcs12 -export -legacy -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
    -name "$NAME" -out "$TMP/id.p12" -passout "pass:$PASS"
  security create-keychain -p "$PASS" "$KC"
  security set-keychain-settings "$KC"
  security import "$TMP/id.p12" -k "$KC" -P "$PASS" -T /usr/bin/codesign >/dev/null
  # Ключом пользуется codesign без окна «разрешить доступ к ключу»
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$PASS" "$KC" >/dev/null
  rm -rf "$TMP"
fi

trap restore_list EXIT
HASH=$(certificate_hash)
[ -n "$HASH" ] || { echo "В $KC нет ключа «${NAME}»" >&2; exit 1; }
security unlock-keychain -p "$(cat "$PASS_FILE")" "$KC"
set_list "$KC"
codesign --force --keychain "$KC" --sign "$HASH" --identifier "$IDENT" "$APP"
restore_list
verify_signature
echo "Подписано: $APP"
codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => /Узнаётся по: /p'
