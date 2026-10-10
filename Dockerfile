# syntax=docker/dockerfile:1.7
FROM jlesage/makemkv:v26.07.2@sha256:0b81851803e805cb1ac1428c790f18813697417d507a93db6945c1ac30378bc3 AS makemkv

FROM python:3.14-alpine3.22
LABEL org.opencontainers.image.title="OpenRipper"
LABEL org.opencontainers.image.description="Self-hosted automatic DVD and Blu-ray ripping"
LABEL org.opencontainers.image.version="0.1.0"

COPY --from=makemkv /opt/makemkv /opt/makemkv

RUN apk add --no-cache \
      eudev-libs \
      openjdk21-jre-headless \
      util-linux-misc \
    && ln -s /usr/lib/libudev.so.1 /usr/lib/libudev.so \
    && mkdir -p /opt/makemkv/appdata \
    && tar -xf /opt/makemkv/share/MakeMKV/appdata.tar -C /opt/makemkv/appdata \
    && ln -s "$(find /opt/makemkv/appdata -name 'sdf_*.bin' -print -quit)" \
      /opt/makemkv/appdata/sdf.bin

WORKDIR /app
COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN pip install --no-cache-dir . \
    && mkdir -p /config /media/library \
    && chmod 0775 /config /media/library

COPY docker/entrypoint.sh /usr/local/bin/openripper-entrypoint
RUN chmod +x /usr/local/bin/openripper-entrypoint

ENV HOME=/config \
    PATH="/opt/makemkv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    OPENRIPPER_DATABASE_URL=postgresql+psycopg://openripper:openripper@postgres:5432/openripper \
    OPENRIPPER_LIBRARY_ROOT=/media/library \
    OPENRIPPER_MAKEMKV_BIN=/opt/makemkv/bin/makemkvcon \
    OPENRIPPER_SDF_PATH=/opt/makemkv/appdata/sdf.bin

VOLUME ["/config", "/media/library"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["openripper-entrypoint"]
CMD ["openripper"]
