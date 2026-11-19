"""Song requests shared by chat and the PC player. No LLM in the playback path."""

import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.core.atomic_io import atomic_write_json
from app.core.paths import data_dir

PROVIDERS = {"youtube": "YouTube", "youtube_music": "YouTube Music"}
_lock = threading.RLock()
_play_lock = threading.Lock()
IS_API_PROCESS = False


def settings():
    with _lock:
        try:
            value = json.loads((data_dir() / "player" / "settings.json").read_text(encoding="utf-8"))
            provider = value.get("provider")
        except (OSError, ValueError, AttributeError):
            provider = None
        return {"provider": provider if provider in PROVIDERS else "youtube_music"}


def set_provider(provider):
    if provider not in PROVIDERS:
        raise ValueError("Unsupported music provider")
    with _lock:
        atomic_write_json(data_dir() / "player" / "settings.json", {"provider": provider})
    return settings()


@dataclass(frozen=True)
class MusicRequest:
    query: str
    provider: str | None = None
    local_first: bool = True


def parse_control(text):
    """Exact commands only; explicit music context may override a task dialog."""
    clean = (text or "").strip().casefold().strip(".!?…")
    explicit = bool(re.search(r"(?:музык\w*|плеер\w*|трек\w*|песн\w*|воспроизведение|music|player|track)", clean))
    clean = re.sub(r"\s+(?:(?:в|на)\s+)?(?:плеере?|музыку|музыки|музыке|воспроизведение|music|player)$", "", clean)
    patterns = {
        "pause": r"(?:пауза|на паузу|поставь(?:те)?(?: музыку)? на паузу|приостанови|pause)",
        "resume": r"(?:продолжи(?:те)?|продолжай(?:те)?|возобнови(?:те)?|resume|continue)",
        "next": r"(?:следующ(?:ий|ую)(?: (?:трек|песню|композицию))?|переключи(?: трек)?|next(?: track)?)",
        "previous": r"(?:предыдущ(?:ий|ую)(?: (?:трек|песню|композицию))?|прошлый трек|previous(?: track)?)",
        "volume_up": r"(?:громче|сделай(?: музыку)? громче|прибавь(?: громкость| звук)?|volume up|louder)",
        "volume_down": r"(?:тише|сделай(?: музыку)? тише|убавь(?: громкость| звук)?|volume down|quieter)",
    }
    for command, pattern in patterns.items():
        if re.fullmatch(pattern, clean):
            return command, explicit
    return None


_SITE = r"(?:youtube\s*music|ют[ую]б(?:е|а)?\s*(?:музык[аеуи]?|мьюзик|мюзик)|youtube|ют[ую]б(?:е|а)?)"
_SITE_TAIL = re.compile(rf"\s+(?:на|в|on|in)\s+({_SITE})\s*[.!?…]*$", re.I)
_COMMAND = re.compile(r"^(включи(?:те)?|включить|поставь(?:те)?|проиграй(?:те)?|сыграй(?:те)?|play|put\s+on|открой(?:те)?|открыть|open)\s+(.+)$", re.I)
_MEDIA_NOUN = re.compile(r"^(?:песню|песнюшку|песня|трек|композицию|музыку|видео|ролик|клип|song|track|music|video)\s+", re.I)
_RESERVED = re.compile(
    r"^(?:звук\w*|громк\w*|субтитр\w*|режим\w*|управлен\w*|свет\w*|камер\w*|микрофон\w*|"
    r"уведомлен\w*|блюту[зс]\w*|вайфай|интернет|экран\w*|полноэкран\w*|полн\w*\s+экран\w*|"
    r"настройк\w*|комментар\w*|браузер\w*|телефон\w*|компьютер\w*|будильник\w*|"
    r"таймер\w*|напоминан\w*|фильм\w*|сериал\w*|плейлист\w*|шоу|"
    r"запись\w*|файл\w*|документ\w*|впн|vpn|bluetooth|wi-?fi|chrome|firefox|edge|safari|"
    r"sound|subtitles|captions|settings|comments|control\s+mode|full\s*screen)(?:\s|$)", re.I)


def parse_request(text, reserved=()):
    """Only playback commands; never steal settings, UI commands or known apps."""
    if not text or len(text) > 500 or "\n" in text:
        return None
    match = _COMMAND.fullmatch(text.strip().strip(".!?…"))
    if not match:
        return None
    verb, query = match.groups()
    query = re.sub(r"^(?:пожалуйста|please)[,\s]+", "", query, flags=re.I)
    provider = None
    site = _SITE_TAIL.search(query)
    if site:
        name = site.group(1).casefold()
        provider = "youtube_music" if re.search(r"music|м(?:[ую]|ью)з", name) else "youtube"
        query = query[:site.start()].strip()
    else:
        other = re.search(r"\s+(?:на|в|on|in)\s+(\S+)\s*$", query, re.I)
        sites = {*map(str.casefold, map(str, reserved)), "spotify", "спотифай", "яндекс", "кинопоиск", "netflix", "twitch"}
        if other and other.group(1).casefold().rstrip("еуа") in {s.rstrip("еуа") for s in sites}:
            return None
    explicit_media = bool(_MEDIA_NOUN.match(query))
    query = _MEDIA_NOUN.sub("", query).strip(' «»"\'.,!?…')
    # Opening an unspecified app/site stays with computer control.
    if verb.lower().startswith(("откр", "open")) and not provider and not explicit_media:
        return None
    if not verb.lower().startswith(("включ", "play", "put")) and not provider and not explicit_media:
        return None
    if not query or _RESERVED.match(query) and not explicit_media:
        return None
    if query.casefold() in {str(x).casefold() for x in reserved} and not explicit_media:
        return None
    if re.fullmatch(_SITE, query, re.I) or query.casefold() in {"музыку", "музыка", "music", "песню", "трек"}:
        return None
    return MusicRequest(query, provider,
                        not (provider and verb.lower().startswith(("откр", "open"))))


def _words(text):
    text = unicodedata.normalize("NFKD", text.casefold().replace("ё", "е"))
    return re.findall(r"[^\W_]+", "".join(c for c in text if not unicodedata.combining(c)))


def find_mp3(query, budget=1.2):
    """Bounded name-only OS-index lookup, then standard user folders. No contents."""
    from app.features import file_finder as ff
    deadline = time.monotonic() + budget
    terms = [word for word in _words(query) if word != "mp3"][:12]
    if not terms:
        return None
    candidates = set()
    req = ff.FileRequest("open", query, terms, exts={"mp3"}, strict_ext=True)
    timeout = max(.1, budget * .6)
    if sys.platform == "darwin" and shutil.which("mdfind"):
        q = ff._mdfind_queries(req)[0][1] + ' && (kMDItemFSName == "*.mp3"cd)'
        candidates.update(ff._read_lines(["mdfind", "-0", q], sep="\0", timeout=timeout))
    elif sys.platform == "win32":
        sql = ff._ps_sql(req)[0][1] + " AND System.ItemType = '.mp3'"
        candidates.update(ff._read_lines(["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
            env=dict(os.environ, VPC_FIND_SQL=sql), stdin_text=ff._PS_SCRIPT, timeout=timeout))
    elif exe := shutil.which("plocate") or shutil.which("locate"):
        candidates.update(ff._read_lines([exe, "-i", "-A", "--basename", "-l", "400", *[f"*{t}*" for t in terms], "*.mp3"], timeout=timeout))
    def choose(paths):
        matches = []
        for candidate in paths:
            path = Path(candidate)
            if (path.suffix.lower() == ".mp3" and ff.path_allowed(str(path))
                    and path.is_file() and all(t in _words(path.stem) for t in terms)):
                words = _words(path.stem)
                matches.append((len(words) - len(terms), str(path)))
        return Path(min(matches)[1]) if matches else None
    indexed = choose(candidates)
    if indexed:
        return indexed
    # The index may be unavailable/unpopulated; keep the fallback deliberately
    # short rather than delaying every online song request by a whole disk scan.
    for folder in ("Music", "Музыка", "Downloads", "Desktop", "Documents"):
        root = Path.home() / folder
        if time.monotonic() >= deadline:
            break
        for directory, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if ff.path_allowed_dir(str(Path(directory) / d))]
            for name in files:
                if time.monotonic() >= deadline:
                    break
                if name.lower().endswith(".mp3") and all(t in _words(name[:-4]) for t in terms):
                    candidates.add(str(Path(directory) / name))
            if time.monotonic() >= deadline:
                break
    return choose(candidates)


def play(query, provider=None, *, local_first=True, headless=True, volume=None):
    from app.features.music_browser import music_browser, youtube_url, MusicBrowserError
    query = str(query).strip()
    if not query or len(query) > 300:
        raise MusicBrowserError("Укажите название песни или видео (до 300 символов)")
    provider = provider or settings()["provider"]
    if provider not in PROVIDERS:
        raise MusicBrowserError("Неизвестный музыкальный сервис")
    with _play_lock:
        args = {"headless": headless}
        if volume is not None:
            args["volume"] = volume
        if re.match(r"https?://|(?:www\.)?(?:music\.)?youtube\.com/|youtu\.be/", query):
            return music_browser.execute_sync("open", url=youtube_url(query), **args)
        if local_first:
            path = find_mp3(query)
            if path:
                return music_browser.execute_sync("local", path=str(path), **args)
        return music_browser.execute_sync("search", query=query, provider=provider, **args)


def play_playlist(playlist_id=None, name=None, track_id=None, headless=True, volume=None):
    from app.features.music_browser import music_browser
    with _play_lock:
        return music_browser.execute_sync('playlist', playlist_id=playlist_id, name=name,
            track_id=track_id, headless=headless, volume=volume)


def parse_playlist_request(text):
    match = re.fullmatch(r'(?:включи(?:те)?|поставь(?:те)?|play)\s+(?:плейлист|playlist)\s+(.+)', (text or '').strip(), re.I)
    return match.group(1).strip(' «»"\'.!?…') if match else None


def playlist_from_chat(name):
    from app.features.music_catalog import snapshot
    name = ' '.join(name.split())
    if not any(p['name'].casefold() == name.casefold() for p in snapshot()['playlists']):
        return None
    result = _chat_api('/api/player/playlists/play', {'name': name})
    return result if result is not None else play_playlist(name=name)


def _chat_api(path, payload=None):
    """Telegram may be a separate process: send its request to this core's API
    so the web player owns the same Chrome. Without an API, standalone bots can
    still play locally. Never redirect to another core or a network proxy.
    """
    if not IS_API_PROCESS:
        import httpx
        from app.features.music_browser import MusicBrowserError
        try:
            port = int(os.getenv("API_PORT", "8000"))
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                              follow_redirects=False, timeout=75) as client:
                health = client.get("/api/health", timeout=.5)
                expected = (data_dir() / "link" / "core_id").read_text(encoding="utf-8").strip()
                if health.status_code == 200 and expected and health.json().get("id") == expected:
                    token = os.getenv("API_TOKEN", "").strip()
                    try:
                        headers = {"Authorization": f"Bearer {token}"} if token else {}
                        response = (client.get(path, headers=headers) if payload is None
                                    else client.post(path, json=payload, headers=headers))
                    except httpx.HTTPError:
                        # It might already be playing: no duplicate local retry.
                        raise MusicBrowserError("Не удалось получить ответ плеера ядра. Проверьте плеер в веб-интерфейсе.") from None
                    if response.status_code == 404:
                        raise MusicBrowserError("Перезапустите ядро, чтобы включить поиск музыки в плеере")
                    try:
                        payload = response.json()
                    except ValueError:
                        raise MusicBrowserError("Плеер ядра вернул некорректный ответ. Проверьте веб-интерфейс.") from None
                    if not response.is_success:
                        detail = payload.get("detail", "Плеер ядра отклонил запрос") if isinstance(payload, dict) else "Плеер ядра отклонил запрос"
                        raise MusicBrowserError(str(detail))
                    if not isinstance(payload, dict) or "state" not in payload:
                        raise MusicBrowserError("Плеер ядра вернул некорректный статус")
                    return payload
        except (httpx.HTTPError, OSError, ValueError):
            pass  # no matching API: standalone mode
    return None


def play_from_chat(query, provider=None, *, local_first=True):
    result = _chat_api("/api/player/play", {"query": query, "provider": provider, "local_first": local_first})
    return result if result is not None else play(query, provider, local_first=local_first)


def player_status():
    from app.features.player_library import library_player
    from app.features.music_browser import music_browser
    return library_player.status() or music_browser.execute_sync("status")


def control(command, value=None):
    from app.features.player_library import library_player
    from app.features.music_browser import music_browser
    with _play_lock:
        if library_player.status():
            result = library_player.control(command, value)
            if command == "stop":
                # A paused YouTube window must not reappear after closing a file.
                music_browser.execute_sync("stop")
            return result
        status = music_browser.execute_sync("status")
        if not status["running"]:
            return status
        return music_browser.execute_sync("control", command=command, value=value)


def control_from_chat(command):
    result = _chat_api("/api/player/browser/control", {"command": command})
    return result if result is not None else control(command)


def update_library(client, status, *, activate=False, ack=None, error=""):
    from app.features.player_library import library_player
    from app.features.music_browser import music_browser
    if activate and status.get('state') != 'stopped':
        with _play_lock:
            if music_browser.execute_sync("status")["running"]:
                music_browser.execute_sync('suspend')
            return library_player.update(client, status, activate=True, ack=ack, error=error)
    result = library_player.update(client, status, ack=ack, error=error)
    if result.pop('finished', False):
        # The worker checks whether this is still the browser suspended for
        # the file. A late end notification cannot close a newly started song.
        music_browser.execute_sync('close_suspended')
    return result


def control_reply(command, status, lang=None):
    if not status.get("running"):
        return "The player is empty. Tell me which song to play." if lang == "en" else "Плеер пуст. Скажи, какую песню включить."
    if status.get("state") == "error":
        return reply(status, lang)
    if command in ("volume_up", "volume_down"):
        n = round(status["volume"] * 100)
        return f"Volume: {n}%." if lang == "en" else f"Громкость: {n}%."
    if command == "pause":
        return "Paused." if lang == "en" else "Поставил на паузу."
    if command == "resume":
        if status.get("state") == "paused":
            return "The player is still paused. Open the player and press Play." if lang == "en" else "Плеер остался на паузе. Открой плеер и нажми Play."
        return "Resumed playback." if lang == "en" else "Продолжил воспроизведение."
    return reply(status, lang)


def reply(status, lang=None):
    result = _playback_reply(status, lang)
    if status.get('playlist_name'):
        name = status['playlist_name']
        index, count = (status.get('queue_index') or 0) + 1, status.get('queue_length') or 0
        result += f' Playlist “{name}” · {index}/{count}.' if lang == 'en' else f' Плейлист «{name}» · {index}/{count}.'
    if status.get('save_error'):
        result += (' Could not save the song link: ' if lang == 'en' else ' Не удалось сохранить ссылку на песню: ') + status['save_error']
    return result


def _playback_reply(status, lang=None):
    english = lang == "en"
    if status["state"] == "error":
        return ("Could not play: " if english else "Не удалось воспроизвести: ") + status["error"]
    title = status.get("title") or ""
    source = ({"local": "local MP3" if english else "MP3 на ПК",
               "library": "player library" if english else "библиотека плеера"}
              .get(status.get("source")) or PROVIDERS.get(status.get("source"), "YouTube"))
    if status["state"] == "playing":
        return f'Playing “{title}” on the PC · {source}.' if english else f'Включил «{title}» на ПК · {source}.'
    return f'Opened “{title}” on the PC · {source}; waiting for playback.' if english else f'Открыл «{title}» на ПК · {source}, ожидаю начала воспроизведения.'
