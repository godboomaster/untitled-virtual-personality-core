"""Import a user-selected audio path on the core computer into the web player."""

import stat
from typing import Literal
from pathlib import Path

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.security import from_this_machine
from app.features.music_browser import MusicBrowserError, music_browser

MAX_AUDIO_BYTES = 100 * 1024 * 1024
MAX_BODY_BYTES = 16 * 1024
AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".weba": "audio/webm", ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}


class AudioPathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class BrowserPlayRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    headless: bool = True
    volume: float = Field(default=.5, ge=0, le=1, allow_inf_nan=False)


class BrowserControlRequest(BaseModel):
    command: Literal["pause", "resume", "stop", "volume", "volume_up", "volume_down", "seek", "next", "previous"]
    value: float | None = Field(default=None, ge=0, le=864000, allow_inf_nan=False)


class PlayerSettingsRequest(BaseModel):
    provider: Literal["youtube", "youtube_music"]


class SongRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    provider: Literal["youtube", "youtube_music"] | None = None
    headless: bool = True
    volume: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    local_first: bool = True


class LibraryStatus(BaseModel):
    state: Literal["playing", "paused", "buffering", "error", "stopped"]
    title: str = Field(max_length=300)
    position: float = Field(ge=0, le=864000, allow_inf_nan=False)
    duration: float = Field(ge=0, le=864000, allow_inf_nan=False)
    volume: float = Field(ge=0, le=1, allow_inf_nan=False)


class LibraryUpdate(BaseModel):
    client: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    status: LibraryStatus
    activate: bool = False
    ack: str | None = Field(default=None, max_length=64)
    error: str = Field(default="", max_length=300)


class SavedSongRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(default="", max_length=300)
    playlist_id: str | None = Field(default=None, max_length=64)


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class PlaylistUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    track_ids: list[str] | None = Field(default=None, max_length=1000)
    add_track_id: str | None = Field(default=None, max_length=64)
    remove_track_id: str | None = Field(default=None, max_length=64)


class PlaylistPlay(BaseModel):
    playlist_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=100)
    track_id: str | None = Field(default=None, max_length=64)
    headless: bool = True
    volume: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


def _audio_header(data: bytes, suffix: str) -> bool:
    if suffix == ".mp3":
        return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if suffix == ".aac":
        return len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0
    if suffix == ".wav":
        return data[:4] in (b"RIFF", b"RF64") and data[8:12] == b"WAVE"
    if suffix in (".aif", ".aiff"):
        return data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC")
    if suffix == ".flac":
        return data.startswith(b"fLaC")
    if suffix in (".ogg", ".opus"):
        return data.startswith(b"OggS")
    if suffix == ".m4a":
        return data[4:8] == b"ftyp"
    if suffix == ".weba":
        return data.startswith(b"\x1a\x45\xdf\xa3")
    return False


def import_audio(raw: str) -> FileResponse:
    """No directory scans or URLs; only a named regular audio file, <=100 MiB.

    The caller must enforce API authentication AND same-machine access. This
    endpoint deliberately grants no filesystem access to paired remote devices.
    """
    try:
        path = Path(raw.strip().strip('"')).expanduser()
        if not path.is_absolute():
            raise HTTPException(400, "Укажите полный путь к аудиофайлу на компьютере ядра")
        path = path.resolve(strict=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in AUDIO_TYPES:
            raise HTTPException(415, "Выберите аудиофайл: MP3, WAV, FLAC, OGG, OPUS, M4A, AAC, WEBA или AIFF")
        if info.st_size == 0:
            raise HTTPException(400, "Аудиофайл пуст")
        if info.st_size > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Размер аудиофайла превышает 100 МБ")
        with path.open("rb") as source:
            header = source.read(32)
        if not _audio_header(header, path.suffix.lower()):
            raise HTTPException(415, "Содержимое файла не соответствует аудиоформату")
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(400, "Не удалось открыть аудиофайл: проверьте путь и права доступа") from None
    return FileResponse(path, media_type=AUDIO_TYPES[path.suffix.lower()],
                        filename=path.name, headers={"Cache-Control": "no-store"})


def create_router(require_auth, trusted_origins) -> APIRouter:
    router = APIRouter(prefix="/api/player", dependencies=[Depends(require_auth)])

    async def require_local(request: Request):
        if not from_this_machine(request, trusted_origins):
            raise HTTPException(403, detail="Плеер на ПК доступен только с компьютера ядра. На другом устройстве используйте выбор файлов.")

    @router.post("/import-path")
    async def import_path(req: AudioPathRequest, request: Request):
        if not from_this_machine(request, trusted_origins):
            raise HTTPException(403, detail="Импорт по пути доступен только на компьютере ядра. Используйте выбор файлов.")
        return await asyncio.to_thread(import_audio, req.path)

    async def browser_call(action, **kwargs):
        try:
            return await music_browser.execute(action, **kwargs)
        except MusicBrowserError as e:
            raise HTTPException(409, detail=str(e)) from None

    async def catalog_call(method, *args, **kwargs):
        from app.features import music_catalog
        try:
            return await asyncio.to_thread(getattr(music_catalog, method), *args, **kwargs)
        except MusicBrowserError as e:
            raise HTTPException(409, detail=str(e)) from None

    @router.get('/saved', dependencies=[Depends(require_local)])
    async def saved_songs():
        return await catalog_call('snapshot')

    @router.post('/saved', dependencies=[Depends(require_local)])
    async def save_song(req: SavedSongRequest):
        return await catalog_call('save_song', **req.model_dump(), custom_title=bool(req.title.strip()))

    @router.delete('/saved/{track_id}', dependencies=[Depends(require_local)])
    async def delete_song(track_id: str):
        return await catalog_call('delete_song', track_id)

    @router.post('/playlists', dependencies=[Depends(require_local)])
    async def create_playlist(req: PlaylistCreate):
        return await catalog_call('create_playlist', req.name)

    @router.put('/playlists/{playlist_id}', dependencies=[Depends(require_local)])
    async def update_playlist(playlist_id: str, req: PlaylistUpdate):
        return await catalog_call('update_playlist', playlist_id, **req.model_dump())

    @router.delete('/playlists/{playlist_id}', dependencies=[Depends(require_local)])
    async def delete_playlist(playlist_id: str):
        return await catalog_call('delete_playlist', playlist_id)

    @router.post('/playlists/play', dependencies=[Depends(require_local)])
    async def play_playlist(req: PlaylistPlay):
        from app.features.music_player import play_playlist
        try:
            return await asyncio.to_thread(play_playlist, **req.model_dump())
        except MusicBrowserError as e:
            raise HTTPException(409, detail=str(e)) from None

    @router.get("/settings", dependencies=[Depends(require_local)])
    async def player_settings():
        from app.features.music_player import settings
        return await asyncio.to_thread(settings)

    @router.put("/settings", dependencies=[Depends(require_local)])
    async def update_player_settings(req: PlayerSettingsRequest):
        from app.features.music_player import set_provider
        return await asyncio.to_thread(set_provider, req.provider)

    @router.post("/play", dependencies=[Depends(require_local)])
    async def play_song(req: SongRequest):
        from app.features.music_player import play
        try:
            return await asyncio.to_thread(play, **req.model_dump())
        except MusicBrowserError as e:
            raise HTTPException(409, detail=str(e)) from None

    @router.get("/browser", dependencies=[Depends(require_local)])
    async def browser_status():
        from app.features.music_player import player_status
        try:
            return await asyncio.to_thread(player_status)
        except MusicBrowserError as e:
            raise HTTPException(409, detail=str(e)) from None

    @router.post("/library", dependencies=[Depends(require_local)])
    async def library_update(req: LibraryUpdate):
        from app.features.music_player import update_library
        try:
            return await asyncio.to_thread(update_library, **req.model_dump())
        except MusicBrowserError as e:
            raise HTTPException(409, detail=str(e)) from None

    @router.post("/browser/play", dependencies=[Depends(require_local)])
    async def browser_play(req: BrowserPlayRequest):
        return await browser_call("open", **req.model_dump())

    @router.post("/browser/control", dependencies=[Depends(require_local)])
    async def browser_control(req: BrowserControlRequest):
        if req.command in ("volume", "seek") and req.value is None:
            raise HTTPException(422, detail="Укажите значение")
        if req.command == "volume" and req.value > 1:
            raise HTTPException(422, detail="Громкость: от 0 до 1")
        from app.features.music_player import control
        try:
            return await asyncio.to_thread(control, **req.model_dump())
        except MusicBrowserError as e:
            raise HTTPException(409, detail=str(e)) from None

    return router
