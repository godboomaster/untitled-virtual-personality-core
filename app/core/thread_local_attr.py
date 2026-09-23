"""Атрибут экземпляра с потоко-локальным значением (дескриптор поверх
threading.local).

Зачем: объект один на персону (ModelRouter, WebChatLLM), а вызовы идут из
нескольких потоков разом — ответ пользователю, инициатива, досье, LTM. Поле
вида «кто ответил на ПОСЛЕДНИЙ вызов» (router._last_provider,
WebChatLLM.last_call_lock_miss) при обычном атрибуте перезаписывается чужим
потоком между «вызвал» и «прочитал»: метка провайдера в ответе API и решение
о догенерации брали значение фоновой задачи. Дескриптор оставляет синтаксис
как был (`self._last_provider = x`, `obj.attr`), а значение хранит на поток.

shared_fallback=True — поток, который сам поле ещё не писал, читает
последнее значение ЛЮБОГО потока (как раньше обычный атрибут): начальное
значение из __init__ и читатели «со стороны» (статус, лог) не ломаются.
shared_fallback=False — такой поток получает default (флаги промаха, где
чужое значение — ложный сигнал).

Нет ни своего, ни общего значения и default не задан — AttributeError, как
у обычного незаданного атрибута: getattr(obj, name, default) работает
по-прежнему. Хранилище создаётся лениво — объекты, собранные через
Class.__new__ без __init__ (тестовые заглушки), тоже работают.
"""

import threading

_MISSING = object()
_INIT_LOCK = threading.Lock()
_TLS_SLOT = "_thread_local_attrs"


class ThreadLocalAttr:
    def __init__(self, default=_MISSING, shared_fallback: bool = True):
        self.default = default
        self.shared_fallback = shared_fallback
        self.name = None
        self.shared_key = None

    def __set_name__(self, owner, name):
        self.name = name
        self.shared_key = f"_tla_shared_{name}"

    @staticmethod
    def _tls(obj) -> threading.local:
        d = obj.__dict__
        tls = d.get(_TLS_SLOT)
        if tls is None:
            with _INIT_LOCK:
                tls = d.get(_TLS_SLOT)
                if tls is None:
                    tls = threading.local()
                    d[_TLS_SLOT] = tls
        return tls

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        try:
            return getattr(self._tls(obj), self.name)
        except AttributeError:
            pass
        if self.shared_fallback and self.shared_key in obj.__dict__:
            return obj.__dict__[self.shared_key]
        if self.default is not _MISSING:
            return self.default
        raise AttributeError(self.name)

    def set_local(self, obj, value):
        """Записать значение ТОЛЬКО для текущего потока, общий fallback не
        трогая (сброс «прошлого вызова» в начале нового: поток пула иначе
        отдал бы значение своего прошлого запроса)."""
        setattr(self._tls(obj), self.name, value)

    def __set__(self, obj, value):
        setattr(self._tls(obj), self.name, value)
        if self.shared_fallback:
            obj.__dict__[self.shared_key] = value
