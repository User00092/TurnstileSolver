FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-liberation \
    fonts-noto-color-emoji \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r appuser && useradd -r -g appuser -m -d /home/appuser appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN Xvfb :99 -screen 0 1920x1080x24 & export DISPLAY=:99

COPY . .
RUN chown -R appuser:appuser /app && chmod +x /app/entrypoint.sh

ENV BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV HEADLESS=false
ENV DISPLAY=:99
ENV HOST=0.0.0.0
ENV PORT=8191
ENV LOG_LEVEL=info
ENV MAX_BROWSERS=10
ENV MAX_TIMEOUT=60000

USER appuser

EXPOSE 8191

CMD ["/app/entrypoint.sh"]
