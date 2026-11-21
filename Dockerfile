FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# فونت وزیر برای زیرنویس فارسی (اگه دانلود نشد، فونت DejaVu استفاده می‌شه)
RUN mkdir -p /app/assets/fonts \
    && (curl -fsSL https://cdn.jsdelivr.net/npm/vazirmatn@33.0.3/fonts/ttf/Vazirmatn-Regular.ttf \
        -o /app/assets/fonts/Vazirmatn-Regular.ttf && [ -s /app/assets/fonts/Vazirmatn-Regular.ttf ]) \
    || (rm -f /app/assets/fonts/Vazirmatn-Regular.ttf; echo "font download skipped")

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
