# Build pgvector against the same PostgreSQL distribution used at runtime.
FROM postgres:16.4-alpine AS builder

ARG PGVECTOR_VERSION=v0.8.1

RUN apk add --no-cache --virtual .build-deps build-base git \
    && git clone --depth 1 --branch "${PGVECTOR_VERSION}" \
       https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && make -C /tmp/pgvector with_llvm=no \
    && make -C /tmp/pgvector with_llvm=no install

FROM postgres:16.4-alpine

COPY --from=builder /usr/local/lib/postgresql/vector.so /usr/local/lib/postgresql/vector.so
COPY --from=builder /usr/local/share/postgresql/extension/ /usr/local/share/postgresql/extension/
