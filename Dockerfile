FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends nginx curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG CORE_VERSION=v25.10.15
RUN curl -fsSL -o /tmp/c.zip https://github.com/XTLS/Xray-core/releases/download/${CORE_VERSION}/Xray-linux-64.zip \
    && unzip -p /tmp/c.zip xray > /usr/local/bin/core && chmod +x /usr/local/bin/core && rm /tmp/c.zip
RUN pip install --no-cache-dir fastapi==0.115.0 uvicorn==0.30.6 bcrypt==4.2.0 "qrcode[pil]==7.4.2"
WORKDIR /app
COPY main.py panel.html ./
ENV DATA_DIR=/app/data
CMD ["python", "main.py"]
