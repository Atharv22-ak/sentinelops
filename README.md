# SentinelOps

Real-time **event ingestion, anomaly detection and incident alerting** platform built from Python microservices,
packaged with a Helm chart, and designed to run on a self-managed Kubernetes cluster (kubeadm on EC2).
An optional **ELK stack** (Filebeat -> Logstash -> Elasticsearch -> Kibana) is toggled with one flag: `elk.enabled=true|false`.

Monitored services push log/telemetry events to the ingest API. SentinelOps stores them, learns what "normal" looks like per
service, detects error-rate spikes, latency degradation and missing heartbeats, de-duplicates the resulting incidents and
notifies Microsoft Teams / Slack / any webhook. A live dashboard shows service health and incidents.

```mermaid
flowchart LR
    C[Monitored services / loadgen] -- "POST /v1/events (X-API-Key)" --> I[ingest-api<br/>FastAPI x2]
    I -- rate limit --> R[(Redis)]
    I -- "publish batch" --> MQ{{RabbitMQ<br/>topic exchange + DLQ}}
    MQ -- events.raw --> P[processor x2<br/>consume + detect]
    P -- bulk insert, idempotent --> PG[(PostgreSQL)]
    P -- "10s buckets, baselines, leader lock" --> R
    P -- incident.detected --> MQ
    MQ -- incidents.detected --> A[alert-service]
    A -- "dedupe + persist" --> PG
    A -- "Adaptive Card / Slack / JSON" --> W[Teams / Slack webhook]
    Q[query-api + dashboard] --> PG
    Q --> R
    U[User] --> Q
    subgraph optional ELK  elk.enabled=true
      FB[Filebeat DaemonSet] --> LS[Logstash] --> ES[(Elasticsearch)] --> K[Kibana]
    end
    I -. JSON logs .-> FB
    P -. JSON logs .-> FB
    A -. JSON logs .-> FB
```

## What is in the repo

| Path | What |
|---|---|
| `services/ingest_api` | Auth (API keys, constant-time compare), per-key rate limiting, validation, publish to RabbitMQ |
| `services/processor` | Consumes batches, idempotent bulk insert, Redis sliding windows, EWMA anomaly detector, Redis leader election, retention sweeper |
| `services/alert_service` | Incident de-duplication, cooldown/reminders, auto-resolve, Teams/Slack/generic notifier |
| `services/query_api` | REST API + single-page dashboard (events, services, incidents, time series) |
| `services/loadgen` | Simulated microservice fleet with periodic injected faults (demo + testing) |
| `services/common` | Shared config, JSON logging, Prometheus middleware, RabbitMQ topology, DB schema |
| `deploy/helm/sentinelops` | Helm chart: apps, Postgres, Redis, RabbitMQ, HTTPRoutes (Gateway API), NetworkPolicies, PDBs, optional HPA, **optional ELK** |
| `.github/workflows/ci.yml` | Lint + tests, `helm lint`/template/kubeconform (ELK on+off), multi-image build & push |
| `tests/` | 31 unit tests (detector maths, models, API auth/rate-limit, notifier formats) |
| `scripts/loadtest.py` | Throughput / latency load test for the ingest API |
| `docs/` | Architecture decisions, runbook, resume & interview notes |

## Quick start (local)

```bash
make install && make test
docker compose up --build        # dashboard http://localhost:8001  (loadgen injects a fault every ~90s)
```

## Deploy on the Kubernetes cluster

```bash
# 1) build + push images (Docker Hub user is the REGISTRY)
make build push REGISTRY=rwxatharv TAG=v1

# 2) deploy WITHOUT ELK
export PG_PASSWORD=$(openssl rand -hex 12) MQ_PASSWORD=$(openssl rand -hex 12) API_KEY=$(openssl rand -hex 16)
make deploy TAG=v1

# 3) flip ELK on / off any time (data is kept in PVCs)
make elk-on        # = helm upgrade --reuse-values --set elk.enabled=true
make elk-off
```

URLs (Envoy Gateway on the Elastic IP, NodePort 32136):

* Dashboard `http://sentinel.3.108.88.140.nip.io:32136`
* Ingest API `http://sentinel-ingest.3.108.88.140.nip.io:32136/v1/events`
* Kibana (only when ELK is on): `make pf-kibana` -> http://localhost:5601

See `docs/RUNBOOK.md` for the full procedure, troubleshooting and a demo script, and `docs/ARCHITECTURE.md` for design decisions.

## API

```bash
curl -X POST $INGEST/v1/events -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"events":[{"service":"checkout","level":"error","status_code":503,"latency_ms":900,"message":"db timeout"}]}'
```

`GET /v1/services` · `GET /v1/events?service=&errors_only=true` · `GET /v1/incidents?status=open` ·
`POST /v1/incidents/{id}/ack|resolve` · `GET /v1/stats/timeseries` · every service exposes `/healthz /readyz /metrics`.
