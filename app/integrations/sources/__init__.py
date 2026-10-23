"""
Источники событий сервисов. Опрашиваемый источник (PollSource):
configured() — есть ли доступ, poll() — новые события (синхронно, ошибки —
исключениями), check() — проверка доступа для кнопки «Проверить»,
gone(items) — id событий, о которых сообщать уже не нужно (прочитано в
самом сервисе). Первый опрос только запоминает курсор: старые письма и
уведомления не становятся «новыми».

Telegram — не опрос, а постоянное подключение (telegram_user.TelegramWatcher).
"""

from typing import List, Optional

import httpx


USER_AGENT = "virtual-persona-core"
# Тесты подменяют транспорт (httpx.MockTransport): сеть не трогается
_transport: Optional[httpx.BaseTransport] = None


def http(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=_transport, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


class SourceError(Exception):
    """Сбой источника: сеть, ответ сервиса."""


class AuthError(SourceError):
    """Доступ отозван или истёк — нужно подключить заново."""


class PollSource:
    name = ""

    def configured(self) -> bool:
        return False

    def poll(self) -> list:
        return []

    def check(self) -> dict:
        return {"ok": self.configured()}

    def gone(self, items: List[dict]) -> set:
        return set()

    def interval_hint(self) -> Optional[float]:
        """Минимальный интервал, который просит сам сервис (GitHub)."""
        return None


def raise_for(resp: httpx.Response, service: str) -> None:
    if resp.status_code in (401, 403) and service != "github":
        raise AuthError(f"{service}: доступ отклонён ({resp.status_code})")
    if resp.status_code == 401:
        raise AuthError(f"{service}: токен не принят")
    if resp.status_code >= 400:
        detail = ""
        try:
            data = resp.json()
            err = data.get("error") if isinstance(data, dict) else None
            detail = (err.get("message") if isinstance(err, dict) else None) \
                or (data.get("message") if isinstance(data, dict) else "") or ""
        except Exception:
            detail = resp.text[:200]
        raise SourceError(f"{service}: HTTP {resp.status_code} {detail}".strip())
