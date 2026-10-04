FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# requirements.txt — lock-файл с точными версиями; torch из него — CPU-сборка
# (+cpu) с download.pytorch.org, без CUDA-пакетов
COPY requirements.txt .
RUN pip install -r requirements.txt

# Модель эмбеддингов памяти — в образ (иначе её скачивает первый запуск).
# Модели книжного поиска Арродеса качаются при первом обращении — в кэш
# Hugging Face (в docker-compose.yml это том hf_cache)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY . .

# Ядро и аддон Арродеса — пакетами: персоны и аддоны аддона находятся через
# entry points, без установки персоны arrodes нет
RUN pip install -e . && pip install -e addons/arrodes

# В контейнере API слушает все интерфейсы контейнера; наружу его выпускает
# только проброс порта (в docker-compose.yml — на 127.0.0.1 хоста).
# Healthcheck — в docker-compose.yml у сервиса api: у Telegram-ботов HTTP-порта нет
ENV API_HOST=0.0.0.0
EXPOSE 8000

# Цель — аргументом: api (по умолчанию), all — все Telegram-боты с токенами,
# <персона> — один бот
CMD ["python", "-m", "app.main", "api"]
