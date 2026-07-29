FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Предзагрузка SentenceTransformer модели в образ
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY . .

# Healthcheck намеренно НЕ задаётся на уровне образа: он привязан к порту Gradio,
# а этот же образ запускает Telegram-ботов — они были бы вечно unhealthy.
# Healthcheck для Gradio определён в docker-compose.yml.

CMD ["python", "-m", "app.main"]
