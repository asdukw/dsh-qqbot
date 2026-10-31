# syntax=docker/dockerfile:1
FROM node:22.14.0-bookworm-slim AS admin-build
WORKDIR /src/admin
COPY admin/package.json admin/package-lock.json ./
RUN npm ci
COPY admin/ ./
RUN npm run build

FROM python:3.12.9-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/var/lib/qqbot
# DSH plugins use node:sqlite, so keep the same Node 22 runtime used to build the UI.
COPY --from=admin-build /usr/local/ /usr/local/
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 999 qqbot && useradd --system --uid 999 --gid qqbot --home-dir /var/lib/qqbot --shell /usr/sbin/nologin qqbot
WORKDIR /opt/qqbot
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY bot/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt
COPY bot/ ./bot/
COPY cards/ ./cards/
COPY dsh/ ./dsh/
COPY dsh/*.mjs ./dsh/plugins/
COPY --from=admin-build /src/admin/dist ./admin/dist
COPY deploy/container-entrypoint.sh /usr/local/bin/container-entrypoint
COPY deploy/init-napcat.py /opt/qqbot/deploy/init-napcat.py
COPY deploy/container_bootstrap.py /opt/qqbot/deploy/container_bootstrap.py
RUN chmod 755 /usr/local/bin/container-entrypoint && mkdir -p /etc/qqbot /var/lib/qqbot /var/lib/qqbot/video-media/outbox && chown -R qqbot:qqbot /var/lib/qqbot /etc/qqbot
USER qqbot
RUN python -c "import deepseek_harness_runtime as r; print(r.resolve_bundled_launch_args())" \
 && DSH_HOME=/tmp/dsh-build-check dsh --version \
 && node -e 'require("node:sqlite")'
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/container-entrypoint"]
