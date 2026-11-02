FROM python:3.12-slim-bookworm AS core
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 APP_DATA=/data TZ=Asia/Shanghai
WORKDIR /app
RUN groupadd --gid 10001 shopdock && useradd --uid 10001 --gid shopdock --create-home shopdock \
    && mkdir /data && chown shopdock:shopdock /data
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=shopdock:shopdock app ./app
COPY --chown=shopdock:shopdock scripts ./scripts
USER shopdock
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]

FROM core AS full
USER root
ARG MPAU_REF=4c8b6465c2473aac3cfe064c8c6dfbadd87b2238
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    xvfb x11-utils x11vnc novnc openbox fonts-noto-cjk tini ffmpeg \
    && cp -aL /usr/share/novnc /opt/novnc \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/cjccd/multi-platform-auto-upload.git /opt/mpau \
    && cd /opt/mpau && git checkout "$MPAU_REF" \
    && python -m venv /opt/mpau-venv \
    && /opt/mpau-venv/bin/pip install --no-cache-dir -e /opt/mpau \
    && /opt/mpau-venv/bin/python -m patchright install-deps chromium \
    && /opt/mpau-venv/bin/python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers DISPLAY=:99 PATH=/opt/mpau-venv/bin:$PATH MPAU_HEADLESS=false MPAU_DEBUG=false
RUN /opt/mpau-venv/bin/python -m patchright install chromium \
    && /opt/mpau-venv/bin/python -m playwright install chromium \
    && mkdir -p /data/mpau/cookies \
    && ln -s /data/mpau/cookies /opt/mpau/cookies \
    && chown -R shopdock:shopdock /opt/mpau /opt/browsers /data
# v0.2.1 fix: MPAU hardcodes playwright channel="chrome" in every uploader, so the
# bundled Chromium alone is not enough -- launching failed with
# "Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome".
# Install real Google Chrome (system package, not /opt/browsers) as root here.
RUN /opt/mpau-venv/bin/python -m playwright install chrome \
    && google-chrome --version
USER shopdock
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "/app/scripts/entrypoint.sh"]
