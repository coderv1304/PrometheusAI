# syntax=docker/dockerfile:1.7
#
# Multi-stage build with a hash-verified kubectl. Tags are shown here for
# readability; CI resolves and signs the resulting image digest. Pin the base
# images by digest in a release build (see docs/SECURITY.md).
ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.5.14

FROM python:${PYTHON_VERSION}-slim AS builder
ARG UV_VERSION
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION} /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra graph --extra postgres

FROM python:${PYTHON_VERSION}-slim AS runtime
ARG KUBECTL_VERSION=v1.31.4
ARG TARGETARCH=amd64

# kubectl is the only runtime binary the policy service needs. The checksum is
# fetched from the same release and verified before install; a corrupted or
# truncated download fails the build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
    && cd /tmp && echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c - \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && rm -f /tmp/kubectl /tmp/kubectl.sha256 \
    && apt-get remove -y curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 docs /app/docs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

USER 10001:10001
EXPOSE 8000 8081

ENTRYPOINT ["prometheus-ai"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
