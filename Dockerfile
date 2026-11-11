FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# Chrome for Testing 157.0.8103.50: fixed build for CVE-2026-125350
ADD --checksum=sha256:d17167bc092eb495a3b768c7467f27e35f625aa03834233a1663af67dcb668c9 https://storage.googleapis.com/chrome-for-testing-public/157.0.8103.50/linux64/chrome-linux64.zip /tmp/chrome-linux64.zip

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/chrome-for-testing/chrome-linux64/chrome

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git unzip \
    && rm -rf /var/lib/apt/lists/* \
    && unzip -q /tmp/chrome-linux64.zip -d /opt/chrome-for-testing \
    && rm -f /tmp/chrome-linux64.zip \
    && find /opt/chrome-for-testing/chrome-linux64 -maxdepth 1 -type f -name "chrome*" -exec chmod 755 {} +

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[douyin]'

COPY config.example.yml ./config.yml

CMD ["weibo-tg", "--config", "/app/config.yml", "run"]
