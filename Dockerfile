FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Предзагрузка SentenceTransformer модели в образ
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY . .

# Healthcheck намеренно НЕ задаётся на уровне образа: у Telegram-ботов нет
# HTTP-порта для проверки — они были бы вечно unhealthy.

CMD ["python", "-m", "app.main"]
