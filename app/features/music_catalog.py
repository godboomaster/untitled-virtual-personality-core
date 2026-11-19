"""Persistent song links and named playlists, shared by UI and bot processes."""
import json
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from app.core.atomic_io import atomic_write_json, file_lock
from app.core.paths import data_dir
from app.features.music_browser import MusicBrowserError, youtube_url

MAX_TRACKS = 5000
MAX_PLAYLISTS = 200
MAX_PLAYLIST_TRACKS = 1000
_lock = threading.RLock()


class CatalogError(MusicBrowserError):
    pass


def song_url(raw):
    url = youtube_url(str(raw))
    parsed = urlsplit(url)
    video = (parse_qs(parsed.query).get('v') or [''])[0]
    if not video:
        raise CatalogError('Добавьте ссылку на отдельную песню или видео')
    return video, f'https://{parsed.hostname}/watch?v={video}'


def _read(path):
    if not path.exists():
        return {'version': 1, 'revision': 0, 'tracks': [], 'playlists': []}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        assert isinstance(data, dict) and data['version'] == 1
        assert isinstance(data.get('revision'), int) and data['revision'] >= 0
        assert isinstance(data['tracks'], list) and len(data['tracks']) <= MAX_TRACKS
        assert isinstance(data['playlists'], list) and len(data['playlists']) <= MAX_PLAYLISTS
        ids = set()
        for row in data['tracks']:
            video, _ = song_url(row['url'])
            assert row['id'] == 'yt_' + video and row['id'] not in ids
            assert isinstance(row['title'], str) and isinstance(row['created'], (float, int))
            ids.add(row['id'])
        playlist_ids = set()
        for row in data['playlists']:
            assert isinstance(row['id'], str) and row['id'] not in playlist_ids
            assert isinstance(row['name'], str) and isinstance(row['track_ids'], list)
            assert len(row['track_ids']) <= MAX_PLAYLIST_TRACKS
            assert len(set(row['track_ids'])) == len(row['track_ids']) and set(row['track_ids']) <= ids
            playlist_ids.add(row['id'])
        return data
    except (OSError, ValueError, KeyError, TypeError, AssertionError, MusicBrowserError):
        raise CatalogError('Не удалось прочитать сохранённые песни. Проверьте data/player/catalog.json; файл не перезаписан.') from None


def _transaction(change=None):
    path = data_dir() / 'player' / 'catalog.json'
    try:
        with _lock, file_lock(path):
            data = _read(path)
            result = change(data) if change else None
            if change:
                data['revision'] = int(data.get('revision', 0)) + 1
                atomic_write_json(path, data)
            return {**data, **(result or {})}
    except OSError:
        raise CatalogError('Не удалось сохранить библиотеку песен на ядре') from None


def snapshot():
    return _transaction()


def _playlist(data, playlist_id):
    row = next((p for p in data['playlists'] if p['id'] == playlist_id), None)
    if row is None:
        raise CatalogError('Плейлист не найден. Обновите библиотеку.')
    return row


def _upsert(data, url, title='', origin='manual', custom_title=False):
    video, url = song_url(url)
    key = 'yt_' + video
    title = ' '.join(str(title or '').split())[:300]
    row = next((r for r in data['tracks'] if r['id'] == key), None)
    source = 'youtube_music' if urlsplit(url).hostname == 'music.youtube.com' else 'youtube'
    if row is None:
        if len(data['tracks']) >= MAX_TRACKS:
            raise CatalogError('Библиотека заполнена: удалите ненужные ссылки')
        row = {'id': key, 'url': url, 'title': title or video, 'source': source,
               'origin': origin, 'custom_title': bool(custom_title and title), 'created': time.time()}
        data['tracks'].append(row)
    else:
        row.update(url=url, source=source)
        if title and (custom_title or not row.get('custom_title')):
            row['title'] = title
            row['custom_title'] = bool(custom_title)
    return row


def save_song(url, title='', *, playlist_id=None, origin='manual', custom_title=False, update_only=False):
    def change(data):
        if update_only:
            video, _ = song_url(url)
            if not any(t['id'] == 'yt_' + video for t in data['tracks']):
                return
        playlist = _playlist(data, playlist_id) if playlist_id else None
        row = _upsert(data, url, title, origin, custom_title)
        if playlist is not None and row['id'] not in playlist['track_ids']:
            if len(playlist['track_ids']) >= MAX_PLAYLIST_TRACKS:
                raise CatalogError('В плейлисте уже 1000 песен')
            playlist['track_ids'].append(row['id'])
        return {'saved_track_id': row['id']}
    return _transaction(change)


def delete_song(track_id):
    def change(data):
        data['tracks'] = [t for t in data['tracks'] if t['id'] != track_id]
        for row in data['playlists']:
            row['track_ids'] = [key for key in row['track_ids'] if key != track_id]
    return _transaction(change)


def _name(data, raw, except_id=None):
    name = ' '.join(str(raw).split())
    if not name or len(name) > 100:
        raise CatalogError('Название плейлиста: от 1 до 100 символов')
    if any(p['name'].casefold() == name.casefold() and p['id'] != except_id for p in data['playlists']):
        raise CatalogError('Плейлист с таким названием уже существует')
    return name


def create_playlist(name):
    def change(data):
        value = _name(data, name)
        if len(data['playlists']) >= MAX_PLAYLISTS:
            raise CatalogError('Можно сохранить до 200 плейлистов')
        key = uuid.uuid4().hex
        data['playlists'].append({'id': key, 'name': value, 'track_ids': [], 'created': time.time()})
        return {'playlist_id': key}
    return _transaction(change)


def update_playlist(playlist_id, *, name=None, track_ids=None, add_track_id=None, remove_track_id=None):
    def change(data):
        row = _playlist(data, playlist_id)
        ids = {t['id'] for t in data['tracks']}
        if sum(value is not None for value in (track_ids, add_track_id, remove_track_id)) > 1:
            raise CatalogError('Передайте одно изменение списка песен')
        if name is not None:
            row['name'] = _name(data, name, playlist_id)
        if track_ids is not None:
            if len(track_ids) > MAX_PLAYLIST_TRACKS or len(set(track_ids)) != len(track_ids) or not set(track_ids) <= ids:
                raise CatalogError('Некорректный список песен')
            row['track_ids'] = list(track_ids)
        if add_track_id is not None:
            if add_track_id not in ids:
                raise CatalogError('Песня не найдена. Обновите библиотеку.')
            if add_track_id not in row['track_ids']:
                if len(row['track_ids']) >= MAX_PLAYLIST_TRACKS:
                    raise CatalogError('В плейлисте уже 1000 песен')
                row['track_ids'].append(add_track_id)
        if remove_track_id is not None:
            row['track_ids'] = [key for key in row['track_ids'] if key != remove_track_id]
    return _transaction(change)


def delete_playlist(playlist_id):
    def change(data):
        _playlist(data, playlist_id)
        data['playlists'] = [p for p in data['playlists'] if p['id'] != playlist_id]
    return _transaction(change)


def playlist_tracks(playlist_id=None, name=None):
    data = snapshot()
    if name is not None:
        found = next((p for p in data['playlists'] if p['name'].casefold() == ' '.join(str(name).split()).casefold()), None)
        if not found:
            raise CatalogError('Плейлист не найден')
        playlist_id = found['id']
    if playlist_id is None:
        return {'id': '__saved__', 'name': '', 'tracks': data['tracks']}
    playlist = _playlist(data, playlist_id)
    tracks = {t['id']: t for t in data['tracks']}
    return {'id': playlist['id'], 'name': playlist['name'], 'tracks': [tracks[key] for key in playlist['track_ids']]}
