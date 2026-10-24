FROM node:22-bookworm-slim@sha256:3ed98443660ff48413b5af19ecefce3b9d50845a901a43420d47cc63a9fddbe9
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /opt/requirements.txt
RUN python3 -m venv /opt/ytm && /opt/ytm/bin/pip install --no-cache-dir -r /opt/requirements.txt
WORKDIR /app
COPY app ./app
COPY web ./web
COPY icon.png ./icon.png
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
ENV DATA_DIR=/data PORT=9206 YTDLP=/opt/ytm/bin/yt-dlp PATH=/opt/ytm/bin:$PATH
EXPOSE 9206
HEALTHCHECK --interval=20s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9206/healthz',timeout=4)"
CMD ["python3", "/app/app/server.py"]
