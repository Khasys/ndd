# Précalcul du score de base de tous les noms suivis : une fois à la construction, en natif même
# quand l'image cible est ARM. Le résultat est un fichier de données, le même pour toutes les cibles.
FROM --platform=$BUILDPLATFORM python:3.12-alpine AS precompute
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN python -m app.precompute

FROM python:3.12-alpine

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apk add --no-cache su-exec
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --from=precompute /app/app/data/scores.bin ./app/data/scores.bin
COPY entrypoint.sh /entrypoint.sh
RUN adduser -D -H -u 1000 app && mkdir -p /data && chown app:app /data

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
