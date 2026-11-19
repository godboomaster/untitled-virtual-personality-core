"""
stdout/stderr без читателя. Ядро, запущенное треем или панелью, пишет вывод
в их трубу; запускавший ушёл (упал, перезапущен), а ядро живёт дальше —
следующая запись в трубу кидает BrokenPipeError (Python игнорирует SIGPIPE).
Тогда любой print или полоса tqdm (sentence-transformers «Batches» при
эмбеддинге реплики; tqdm сам глушит только errno 5) роняет ход посреди
ответа, и даже реплика-ошибка не записывается в STM.

После первого EPIPE дескриптор потока переводится на /dev/null: туда же
уходит недописанный буфер, вывод C-кода (sherpa-onnx) и детей, запущенных
позже. Лог остаётся в кольцевом буфере /api/logs.
"""

import os
import sys


class _GuardedStream:
    """Обёртка stdout/stderr: запись в трубу без читателя молча пропадает"""

    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        try:
            return self._stream.write(s)
        except BrokenPipeError:
            self._drop()
            return len(s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        try:
            self._stream.flush()
        except BrokenPipeError:
            self._drop()

    def _drop(self):
        try:
            fd = self._stream.fileno()
        except (OSError, ValueError):
            fd = None
        if fd is not None:
            # Сам поток не трогаем: закрыть его — закрыть fd 1/2, и номер
            # займёт первый же открытый файл или сокет
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, fd)
            finally:
                os.close(null)
        else:
            self._stream = open(os.devnull, "w", encoding="utf-8")

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install() -> None:
    """Обернуть sys.stdout/sys.stderr (идемпотентно). Звать до настройки
    logging: хендлеры берут поток, который в sys.* в момент создания."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, _GuardedStream):
            setattr(sys, name, _GuardedStream(stream))
