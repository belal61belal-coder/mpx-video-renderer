FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       curl \
       fonts-noto-core \
       fonts-noto-extra \
       libraqm0 \
       libfribidi0 \
       libharfbuzz0b \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py
COPY logo.png /app/logo.png
COPY base_template.png /app/base_template.png

ENV DATA_DIR=/data
ENV LOGO_PATH=/app/logo.png
ENV TEMPLATE_PATH=/app/base_template.png

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
