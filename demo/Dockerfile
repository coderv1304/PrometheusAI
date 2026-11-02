# syntax=docker/dockerfile:1.7
#
# Tiny stdlib-only demo service used to inject faults into the demo namespace.
FROM python:3.12-slim

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin app

WORKDIR /app
COPY --chown=10001:10001 app.py /app/app.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    FAULT=none

USER 10001:10001
EXPOSE 8080

CMD ["python", "/app/app.py"]
