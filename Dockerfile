FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV DATA_DIR=/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
