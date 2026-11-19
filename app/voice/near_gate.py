"""Фильтр «кто ближе к микрофону» для слушателя имени (wake_listener).

Вокруг говорят (толпа, соседи, телевизор), а распознаватель слышит всё
подряд: чужие слова попадают в вашу фразу, а пока говорят на фоне, тишины
нет и фраза не кончается. Вы ближе к микрофону, чем фон, и ваша речь в нём
громче. Фильтр стоит перед распознавателем: звук кадрами по FRAME_SEC,
кадр тише вашего голоса больше чем на margin дБ заменяется тихим шумом
(как в пустой комнате). Начало слова не режется: звук идёт с задержкой
LOOK_SEC, и громкий кадр открывает фильтр и для кадров перед ним; хвост
слова (согласные тише гласных) — HANG_SEC после последнего громкого кадра.
Открывают фильтр только OPEN_FRAMES громких кадров подряд: щелчок или
одинокий громкий слог фона — нет.

Громкость вашей речи (level, дБ от полной шкалы) — высокий перцентиль
громкости речевых кадров (LEVEL_PCT): гласные. Откуда:
  - «Запомнить мой голос» в панели (calibrate): CALIB_SEC вашей речи;
  - фраза с именем (learn): назвал персону, скорее всего, вы — уровень
    последних секунд до имени; дальше по каждой вашей фразе в разговоре
    понемногу (LEARN_RATE), из кадров, что прошли фильтр.
Запоминается на микрофон (data/voice/near.json): у встроенного и гарнитуры
громкость разная.

Строгость: пока ждём имя, фильтр чуть мягче (margin + IDLE_EXTRA_DB) —
имя слышно и с шага-двух дальше, и уровень по нему подстроится, если вы
отошли; в разговоре и после имени — margin. Уровня ещё нет — пропускает
всё; тогда под громким фоном и имя не расслышать (без фильтра толпа
сливается с ним в одну фразу) — для этого «Запомнить мой голос».

Что фильтр не может: фон поверх вашего же слова (одновременно) остаётся —
он убирает фон в паузах между словами, до и после фразы. Фон громче
вашего голоса минус margin (соседи рядом, громкий телевизор) проходит.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

FRAME_SEC = 0.02
LOOK_SEC = 0.25
HANG_SEC = 0.3
OPEN_FRAMES = 2
# Насколько тише вашего голоса — уже фон; ждём имя — мягче на столько
MARGIN_DB = float(os.getenv("WAKE_NEAR_DB", "10") or 10)
# Подобрано на настоящем распознавателе: имя слышно и под толпой на 15 дБ
# тише вас, и когда вы сами на 10 дБ тише записанного (отошли)
IDLE_EXTRA_DB = 3.0
# Речевой кадр — громче шумового пола (низкий перцентиль за HISTORY_SEC) на
# столько; уровень — такой перцентиль речевых кадров
SPEECH_OVER_FLOOR_DB = 10.0
FLOOR_PCT = 15
LEVEL_PCT = 90
HISTORY_SEC = 12.0
# Уровень по имени — за столько секунд до него; по фразе — доля нового
NAME_WINDOW_SEC = 1.5
LEARN_RATE = 0.3
MIN_SPEECH_SEC = 0.25
# «Запомнить мой голос»: столько секунд слушать, речи нужно хотя бы столько
CALIB_SEC = 6.0
CALIB_MIN_SPEECH_SEC = 1.0
# Заглушённое — тихий шум, не нули: так звучит пустая комната
COMFORT = 1e-4
SILENT_DB = -100.0


def enabled_by_env() -> bool:
    return (os.getenv("WAKE_NEAR") or "1").strip().lower() not in ("0", "false", "no", "off")


def levels_path() -> Path:
    from app.core.paths import data_dir
    return data_dir() / "voice" / "near.json"


def frame_db(x: np.ndarray) -> float:
    if not len(x):
        return SILENT_DB
    p = float(np.mean(np.square(x, dtype=np.float64)))
    return 10.0 * math.log10(p) if p > 1e-10 else SILENT_DB


def speech_level(dbs, floor: float) -> Optional[float]:
    """Громкость речи по кадрам: LEVEL_PCT речевых (громче пола); речи
    меньше MIN_SPEECH_SEC — None."""
    speech = [d for d in dbs if d > floor + SPEECH_OVER_FLOOR_DB]
    if len(speech) * FRAME_SEC < MIN_SPEECH_SEC:
        return None
    return float(np.percentile(speech, LEVEL_PCT))


class NearGate:
    """Звук микрофона → звук распознавателю (с задержкой LOOK_SEC), тихое — тишиной."""

    def __init__(self, device: str = "", path: Optional[Path] = None, margin: float = MARGIN_DB, on: bool = True):
        self.device = device
        self.path = path
        self.margin = margin
        self.on = on
        self.level: Optional[float] = None
        # Откуда уровень: panel (записали в панели), name (по фразе с именем)
        self.source = ""
        self._rate = 0
        self._flen = 0
        self._rest = np.zeros(0, np.float32)
        # Задержка: (кадр, открыт ли); громкий кадр открывает и тех, что перед ним
        self._line: deque = deque()
        self._loud_run = 0
        self._hang = 0
        self._gain = 1.0
        # (когда, дБ, прошёл ли) — пол и уровень считаются по истории
        self._hist: deque = deque()
        self._floor = SILENT_DB
        self._floor_at = 0.0
        self._calib_until = 0.0
        self._calib: list[float] = []
        # Последний результат «Запомнить мой голос»: ok, no_speech
        self.calib_result = ""
        self._rng = np.random.default_rng(7)
        self._load()

    # --- уровень ---

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            rec = json.loads(self.path.read_text("utf-8")).get(self.device)
        except (OSError, ValueError, AttributeError):
            return
        if isinstance(rec, dict) and isinstance(rec.get("level"), (int, float)):
            self.level = float(rec["level"])
            self.source = str(rec.get("source") or "")

    def _save(self) -> None:
        if self.path is None or self.level is None:
            return
        try:
            data = json.loads(self.path.read_text("utf-8")) if self.path.is_file() else {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[self.device] = {"level": round(self.level, 1), "source": self.source}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.path)
        except OSError as e:
            logger.warning(f"[Wake] Громкость голоса не сохранилась: {e}")

    def _set(self, level: float, source: str) -> None:
        self.level, self.source = level, source
        self._save()

    def learn_name(self, now: Optional[float] = None) -> Optional[float]:
        """Прозвучало имя: громкость того, кто его сказал, — по последним
        NAME_WINDOW_SEC (до фильтра: если вы отошли и стали тише, уровень
        догонит). Записанный в панели уровень двигается понемногу."""
        now = time.monotonic() if now is None else now
        lv = speech_level([d for at, d, _ in self._hist if at >= now - NAME_WINDOW_SEC], self._floor)
        if lv is None:
            return None
        if self.level is None or self.source != "panel":
            self._set(lv, "name")
        else:
            self._set(self.level + LEARN_RATE * (lv - self.level), "panel")
        return self.level

    def learn_phrase(self, since: float) -> Optional[float]:
        """Ваша фраза ушла персоне: уровень понемногу к её громкости (кадры,
        что прошли фильтр, — ваши, а не фон)."""
        if self.level is None:
            return None
        lv = speech_level([d for at, d, passed in self._hist if at >= since and passed], self._floor)
        if lv is None:
            return None
        self._set(self.level + LEARN_RATE * (lv - self.level), self.source or "name")
        return self.level

    def calibrate(self, now: Optional[float] = None) -> None:
        """«Запомнить мой голос»: следующие CALIB_SEC — ваша речь."""
        now = time.monotonic() if now is None else now
        self._calib_until = now + CALIB_SEC
        self._calib = []
        self.calib_result = ""

    @property
    def calibrating(self) -> bool:
        return self._calib_until > 0

    def calib_left(self, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self._calib_until - now) if self._calib_until else 0.0

    def _calib_done(self) -> None:
        self._calib_until = 0.0
        speech = [d for d in self._calib if d > self._floor + SPEECH_OVER_FLOOR_DB]
        if len(speech) * FRAME_SEC < CALIB_MIN_SPEECH_SEC:
            self.calib_result = "no_speech"
            logger.info("[Wake] «Запомнить мой голос»: речи не услышал")
            return
        self._set(float(np.percentile(speech, LEVEL_PCT)), "panel")
        self.calib_result = "ok"
        logger.info(f"[Wake] Громкость вашего голоса: {self.level:.1f} дБ (записано в панели)")

    def forget(self) -> None:
        self.level, self.source = None, ""
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict) and data.pop(self.device, None) is not None:
                self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except (OSError, ValueError):
            pass

    def threshold(self, strict: bool) -> Optional[float]:
        if not self.on or self.level is None:
            return None
        return self.level - self.margin - (0.0 if strict else IDLE_EXTRA_DB)

    def status(self) -> dict:
        return {"on": self.on, "level": None if self.level is None else round(self.level, 1),
                "source": self.source, "margin": self.margin,
                "calibrating": self.calibrating, "calib_left": round(self.calib_left(), 1),
                "calib_result": self.calib_result}

    # --- звук ---

    def reset(self) -> None:
        """Накопленное в задержке — мимо (после глушения, нового потока)."""
        self._line.clear()
        self._rest = np.zeros(0, np.float32)
        self._loud_run = 0
        self._hang = 0

    def _floor_now(self, now: float) -> float:
        if now - self._floor_at > 0.5 and self._hist:
            self._floor = float(np.percentile([d for _, d, _ in self._hist], FLOOR_PCT))
            self._floor_at = now
        return self._floor

    def feed(self, rate: int, chunk: np.ndarray, at: float, strict: bool) -> np.ndarray:
        """Кусок с микрофона (at — когда записан) → кусок распознавателю той
        же длины, но на LOOK_SEC раньше записанный; тихие кадры — тишиной."""
        if rate != self._rate:
            self._rate, self._flen = rate, max(1, int(rate * FRAME_SEC))
            self.reset()
        flen = self._flen
        look = max(1, round(LOOK_SEC / FRAME_SEC))
        hang = max(1, round(HANG_SEC / FRAME_SEC))
        thr = self.threshold(strict)
        x = np.concatenate([self._rest, np.asarray(chunk, np.float32)])
        n = len(x) // flen
        self._rest = x[n * flen:]
        out = []
        while len(self._hist) and self._hist[0][0] < at - HISTORY_SEC:
            self._hist.popleft()
        for k in range(n):
            fr = x[k * flen:(k + 1) * flen]
            t = at - (n - k) * FRAME_SEC
            d = frame_db(fr)
            if thr is None:
                is_open = True
            else:
                loud = d >= thr
                self._loud_run = self._loud_run + 1 if loud else 0
                if self._loud_run >= OPEN_FRAMES:
                    # Открылся: и кадры перед ним (начало слова, первые громкие)
                    for i in range(max(0, len(self._line) - look), len(self._line)):
                        self._line[i][1] = True
                    self._hang = hang
                is_open = self._hang > 0
                if self._hang > 0 and not loud:
                    self._hang -= 1
            self._line.append([fr, is_open, d, t])
            if self._calib_until:
                self._calib.append(d)
            while len(self._line) > look:
                f, o, fd, ft = self._line.popleft()
                self._hist.append((ft, fd, o))
                out.append(self._shape(f, o))
        self._floor_now(at)
        if self._calib_until and at >= self._calib_until:
            self._calib_done()
        if not out:
            return np.zeros(0, np.float32)
        return np.concatenate(out)

    def _shape(self, f: np.ndarray, is_open: bool) -> np.ndarray:
        """Кадр наружу: открыт — как есть, закрыт — тихий шум; на стыке —
        плавно за кадр (без щелчка, который распознаватель принял бы за звук)."""
        g0, g1 = self._gain, 1.0 if is_open else 0.0
        self._gain = g1
        if g0 == g1 == 1.0:
            return f
        noise = (self._rng.standard_normal(len(f)) * COMFORT).astype(np.float32)
        if g0 == g1 == 0.0:
            return noise
        g = np.linspace(g0, g1, len(f), dtype=np.float32)
        return f * g + noise * (1 - g)
