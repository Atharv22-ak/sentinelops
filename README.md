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
| `deploy/` | Helm chart: apps, Postgres, Redis, RabbitMQ, HTTPRoutes (Gateway API), least-privilege NetworkPolicies, PDBs, HPA/KEDA, ExternalSecrets, ServiceMonitors/alerts/Grafana dashboard, **optional ELK** |
| `argocd/` | Argo CD `Application` (GitOps deployment of `deploy/` + `values-cluster.yaml`) |
| `.github/workflows/ci.yml` | Lint + tests, `helm lint`/template/kubeconform, Trivy (manifests + images), build -> scan -> push (Git SHA tag) -> bump `values-cluster.yaml` for Argo CD |
| `tests/` | 37 unit tests (detector maths, models, API auth/rate-limit, notifier formats, load-test thresholds) |
| `scripts/loadtest.py` | Load test for the ingest API: progressive stages, batch sizes, p50/p95/p99, pass/fail thresholds |
| `scripts/chaos.sh` | Pod-delete / OOM / dependency-outage recovery tests (non-prod) |
| `docs/` | Architecture decisions, runbook, **production readiness guide**, resume & interview notes |

## Quick start (local)

```bash
make install && make test
docker compose up --build        # dashboard http://localhost:8001  (loadgen injects a fault every ~90s)
```

## Deploy on the Kubernetes cluster

Images are tagged with the Git commit (immutable). The chart rejects `v1`/`latest` and refuses to render without real credentials
- see [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md).

```bash
# 1) build + push images (tag defaults to the short Git SHA)
make build push REGISTRY=rwxatharv

# 2) credentials live outside Git: create the Secret once (or use External Secrets, see values.yaml)
export PG_PASSWORD=$(openssl rand -hex 12) MQ_PASSWORD=$(openssl rand -hex 12) API_KEY=$(openssl rand -hex 16)
make secret

# 3) deploy WITHOUT ELK
make deploy SECRET_NAME=sentinelops-secrets

# 4) flip ELK on / off any time (data is kept in PVCs)
make elk-on        # = helm upgrade --reuse-values --set elk.enabled=true
make elk-off

# optional cluster add-ons
make metrics-server   # kubectl top + CPU HPAs
make monitoring-on    # ServiceMonitors, alerts, Grafana dashboard (needs kube-prometheus-stack)
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
