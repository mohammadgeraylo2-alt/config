FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

RUN mkdir -p /opt/xray /app/data \
 && python -c "import urllib.request;urllib.request.urlretrieve('https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip','/tmp/x.zip')" \
 && python -m zipfile -e /tmp/x.zip /opt/xray \
 && chmod +x /opt/xray/xray && rm -f /tmp/x.zip

RUN pip install "fastapi==0.115.6" "uvicorn[standard]==0.34.0" "websockets==13.1" "psutil==6.1.1"

WORKDIR /app
COPY main.py index.html ./

EXPOSE 8080
CMD ["python", "main.py"]
