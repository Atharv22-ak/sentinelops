# One parameterised Dockerfile for every service. Build context = repo root.
#   docker build -f services/Dockerfile --build-arg SERVICE=ingest_api -t sentinelops-ingest-api .
FROM python:3.12-slim
ARG SERVICE
ENV SERVICE=${SERVICE} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000
WORKDIR /app
COPY services/common/requirements.txt /tmp/common-requirements.txt
RUN pip install -r /tmp/common-requirements.txt
COPY services/common/sentinel_common /app/sentinel_common
COPY services/${SERVICE}/${SERVICE} /app/${SERVICE}
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001
EXPOSE 8000
CMD ["sh", "-c", "exec python -m ${SERVICE}.main"]
