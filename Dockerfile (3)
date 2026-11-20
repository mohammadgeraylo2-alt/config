FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends nginx curl unzip gettext-base ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG XRAY_REFRESH=2026-11
RUN echo "xray $XRAY_REFRESH" && curl -fL -o /tmp/x.zip https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip \
    && unzip /tmp/x.zip xray -d /usr/local/bin && chmod +x /usr/local/bin/xray && rm /tmp/x.zip && xray version
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY nginx.conf.template start.sh ./
RUN chmod +x start.sh
ENV XRAY_BIN=/usr/local/bin/xray
EXPOSE 8080
CMD ["./start.sh"]
