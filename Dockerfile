# Stage 1: Web UI のビルド
FROM node:22-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Stage 2: 実行イメージ(python + ffmpeg + ビルド済み web — 1コンテナ完結 §9)
FROM python:3.10-slim
LABEL org.opencontainers.image.title="podalign" \
      org.opencontainers.image.description="±1ms offset / ±1ppm drift auto-sync and mastering pipeline for double-ender podcast recordings" \
      org.opencontainers.image.source="https://github.com/Neru-Neru/podalign" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
# Debian の ffmpeg は librubberband 有効ビルド(ドリフト補正に必須)。
# 開発環境は 4.4.2 で検証済み。メジャーバージョン差異があれば §3 の方針どおり実測で確認すること
# NOTE: librubberband 入り ffmpeg は GPL — 同梱表記とソース入手方法は NOTICE 参照
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN ffmpeg -filters 2>/dev/null | grep -q rubberband || \
    (echo "ffmpeg に librubberband がありません" && exit 1)

WORKDIR /srv
COPY pyproject.toml LICENSE NOTICE ./
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" numpy
COPY app/ app/
COPY presets/ presets/
COPY --from=web /build/dist web/dist

ENV PODCAST_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
