# syntax=docker/dockerfile:1

# ---- build stage: resolve and install pinned dependencies with uv -------------------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src
# Set to "true" to include boto3 for SECRETS_BACKEND=aws_ssm
ARG WITH_AWS=false

# Dependencies first, so this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project $([ "$WITH_AWS" = "true" ] && echo "--extra aws")

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable $([ "$WITH_AWS" = "true" ] && echo "--extra aws")

# ---- runtime stage ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root user. No data, .env or secrets are baked into the image: config, resume, profile
# and the database all live on the /data volume, and secrets arrive as environment variables.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data && chown app:app /data

COPY --from=build /src/.venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/jobs.db \
    CONFIG_PATH=/data/config.yaml

# Relative paths in config.yaml (data/resume.md, data/profile.md) resolve against /,
# which is exactly the /data volume.
WORKDIR /
USER 10001:10001
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "job_hunter", "healthcheck"]

ENTRYPOINT ["python", "-m", "job_hunter"]
CMD ["run"]
