"""Тест голосового режима в приложении Android:

  A. движки голосового режима (web/src/voice/speechEngines.ts) под Node —
     браузерный (Web Speech API, поведение как было в VoiceChat) и плагин
     NativeSpeech с поддельным плагином: разрешение на микрофон, номера
     сессий, ошибки, озвучка (scripts/app_voice_node.mjs);
  B. сборка Android: плагин NativeSpeech (методы, события, разрешение
     microphone, главный поток, освобождение в handleOnDestroy), регистрация
     в MainActivity до super.onCreate, манифест (RECORD_AUDIO, <queries>);
  C. веб: VoiceChat говорит только через адаптер, кнопка голосового режима
     на телефоне не спрятана, строки подсказок на двух языках, README.

Без сети, без ядра и без эмулятора. Без Node часть A пропускается.

Запуск: PYTHONPATH=. python3 scripts/test_app_voice.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
WEB = ROOT / "web"
JAVA = WEB / "android" / "app" / "src" / "main" / "java" / "io" / "vpcore" / "app"
MANIFEST = WEB / "android" / "app" / "src" / "main" / "AndroidManifest.xml"

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ════════════ A. Движки под Node ════════════

def test_engines_node():
    section("A. Движки голосового режима под Node")
    if not shutil.which("node"):
        print("  (пропущено — нет Node)")
        return
    res = subprocess.run(
        ["node", str(ROOT / "scripts" / "app_voice_node.mjs"), str(WEB / "src" / "voice" / "speechEngines.ts")],
        capture_output=True, text=True, timeout=60, cwd=WEB,
    )
    lines = [json.loads(x) for x in res.stdout.splitlines() if x.startswith("{")]
    check("Node выполнил проверки движков", res.returncode == 0 and len(lines) >= 20 or print(res.stderr[-800:]))
    for x in lines:
        check(x["name"], x["ok"] or print(f"      {x.get('extra')}"))


# ════════════ B. Android ════════════

def test_android():
    section("B. Плагин NativeSpeech, MainActivity, манифест")
    plugin = read(JAVA / "NativeSpeechPlugin.java")
    check("плагин назван NativeSpeech", '@CapacitorPlugin(' in plugin and 'name = "NativeSpeech"' in plugin)
    check("разрешение через Capacitor: alias microphone → RECORD_AUDIO",
          re.search(r'@Permission\(\s*alias\s*=\s*"microphone"\s*,\s*strings\s*=\s*\{\s*Manifest\.permission\.RECORD_AUDIO', plugin) is not None)
    for m in ("isRecognitionAvailable", "startListening", "stopListening", "speak", "stopSpeaking", "isTtsAvailable"):
        check(f"метод {m} доступен вебу", re.search(r"@PluginMethod\s+public void " + m + r"\(PluginCall", plugin) is not None)
    for ev in ("partial", "final", "error", "end"):
        check(f"событие {ev}", f'notifyListeners("{ev}"' in plugin)
    check("распознаватель создаётся в главном потоке",
          "Looper.getMainLooper()" in plugin and re.search(r"main\.post\(\(\) -> \{[^}]*createSpeechRecognizer", plugin, re.S) is not None)
    check("без разрешения startListening отказывает с not-allowed",
          'getPermissionState("microphone")' in plugin and '"not-allowed"' in plugin)
    check("«не расслышал» и тишина — мягкие коды, не падение",
          "ERROR_NO_MATCH" in plugin and '"no-match"' in plugin and "ERROR_SPEECH_TIMEOUT" in plugin and '"no-speech"' in plugin)
    m = re.search(r"public void stopListening\(PluginCall call\)\s*\{(.*?)\n    \}\n", plugin, re.S)
    stop_body = m.group(1) if m else ""
    check("после stopListening — страховочный таймаут: молчащий движок не держит сессию",
          "postDelayed" in stop_body and "closeSession(null, null)" in stop_body and "seq == sessionSeq" in stop_body)
    check("озвучка ждёт конца фразы (UtteranceProgressListener)", "UtteranceProgressListener" in plugin and "onDone" in plugin)
    check("вызовы до готовности TTS ждут её", "whenTtsReady" in plugin and "ttsWaiting" in plugin)
    m = re.search(r"protected void handleOnDestroy\(\)\s*\{(.*?)\n    \}\n", plugin, re.S)
    body = m.group(1) if m else ""
    check("handleOnDestroy освобождает распознаватель и TTS", "recognizer.destroy()" in body and "shutdown()" in body)

    main = read(JAVA / "MainActivity.java")
    reg = main.find("registerPlugin(NativeSpeechPlugin.class)")
    sup = main.find("super.onCreate(")
    check("MainActivity регистрирует плагин до super.onCreate", 0 <= reg < sup)

    man = read(MANIFEST)
    check("манифест: RECORD_AUDIO", 'android:name="android.permission.RECORD_AUDIO"' in man)
    q = re.search(r"<queries>(.*?)</queries>", man, re.S)
    qb = q.group(1) if q else ""
    check("манифест: <queries> — служба распознавания", 'android.speech.RecognitionService' in qb)
    check("манифест: <queries> — служба озвучки", 'android.intent.action.TTS_SERVICE' in qb)


# ════════════ C. Веб и документация ════════════

def _keys(path: Path) -> set[str]:
    return set(re.findall(r"^\s*'([\w.]+)':", read(path), re.M))


def test_web():
    section("C. VoiceChat, телефонная вёрстка, строки, README")
    vc = read(WEB / "src" / "components" / "VoiceChat.tsx")
    check("VoiceChat не зовёт Web Speech API напрямую",
          "window.speechSynthesis" not in vc and "SpeechRecognition" not in vc.split("*/", 1)[1])
    check("VoiceChat берёт движок из voice/speechIO", "getSpeechEngine" in vc and "../voice/speechIO" in vc)
    io = read(WEB / "src" / "voice" / "speechIO.ts")
    check("адаптер: в приложении — плагин NativeSpeech, иначе браузер",
          "isNativeApp()" in io and "registerPlugin<NativeSpeechPlugin>('NativeSpeech')" in io and "createBrowserEngine" in io)

    css = read(WEB / "src" / "mobile.css")
    hidden = re.findall(r"([^{}]+)\{\s*display:\s*none;?\s*\}", css)
    voice_hidden = [s for s in hidden if "voice" in s or "chat-header-actions .btn" in s]
    check("телефон: кнопку голосового режима в шапке не прячем", not voice_hidden)
    chat = read(WEB / "src" / "sections" / "Chat.tsx")
    check("кнопка голосового режима в шапке без условия «не в приложении»",
          "setChatMode('voice')" in chat and "isNativeApp" not in chat[max(0, chat.find("chat.modeVoiceTitle") - 300):chat.find("chat.modeVoiceTitle")])

    check("в браузере подсказок о сбоях микрофона и озвучки нет (как было)",
          "engine.kind === 'native' && MIC_ERROR_HINTS[code]" in vc
          and "status === 'unavailable' && engine.kind === 'native'" in vc)

    ru, en = _keys(WEB / "src" / "i18n" / "ru.ts"), _keys(WEB / "src" / "i18n" / "en.ts")
    used = set(re.findall(r"'(chat\.(?:mic|tts)\w*)'", vc))
    check("строки подсказок голосового режима есть", {"chat.micUnsupportedApp", "chat.micDenied", "chat.ttsUnavailable"} <= used)
    check("все строки VoiceChat — и в ru.ts, и в en.ts", used <= ru and used <= en or print(f"      нет: {used - (ru & en)}"))

    rme, rru = read(ROOT / "README.md"), read(ROOT / "README.ru.md")
    check("README.md: нет «voice mode» в списке того, чего нет", "and voice mode (the Android WebView" not in rme)
    check("README.ru.md: нет «голосового режима» в списке того, чего нет", "и голосового режима (в WebView" not in rru)
    check("README: голос в приложении — распознавание/озвучка Android",
          "speech recognition" in rme and "распознавание речи" in rru and "Google" in rme and "Google" in rru)


def main():
    test_engines_node()
    test_android()
    test_web()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
