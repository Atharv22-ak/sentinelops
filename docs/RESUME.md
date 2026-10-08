# Resume & interview notes

> Fill the `<...>` numbers with **real** results from `make loadtest` and your cluster. Don't claim numbers you haven't measured.

## Resume entry (pick 4-5 bullets)

**SentinelOps - Real-time Observability & Incident Detection Platform** | Python, FastAPI, RabbitMQ, PostgreSQL, Redis, Kubernetes, Helm, ELK, GitHub Actions

* Designed and built an event-driven platform of 5 Python microservices (FastAPI, asyncio) that ingests service telemetry, detects anomalies and routes de-duplicated incident alerts to Microsoft Teams/Slack; load-tested to `<X>` events/s with p95 ingest latency of `<Y>` ms on 2 x t3.medium workers.
* Implemented an adaptive anomaly detector (EWMA mean/variance baselines, z-score + absolute floors, heartbeat monitoring) on Redis sliding windows, with Lua-based leader election so processors scale horizontally without duplicate detections.
* Engineered reliable messaging: RabbitMQ topic exchange with dead-letter queue, idempotent consumers (`ON CONFLICT DO NOTHING`), per-key Redis rate limiting and retry/back-off, giving at-least-once processing without duplicate rows.
* Packaged everything as a production-style Helm chart (StatefulSets with PVCs, PodDisruptionBudgets, NetworkPolicies, non-root read-only containers, Gateway API routes) and deployed to a self-managed kubeadm cluster on AWS EC2.
* Added a feature-flagged ELK stack (`elk.enabled`): Filebeat DaemonSet -> Logstash JSON pipeline -> Elasticsearch with ILM retention and an auto-provisioned Kibana data view via Helm hooks.
* Built CI/CD with GitHub Actions (ruff, 31 unit tests, `helm lint` + kubeconform on ELK on/off renders, matrix Docker builds) and exposed Prometheus metrics, health/readiness probes and structured JSON logging on every service.

Short version (one line each, if space is tight):
* Built an event-driven incident-detection platform (5 Python microservices, RabbitMQ, Postgres, Redis) deployed via Helm on self-managed Kubernetes, with an optional ELK logging stack and Teams alerting.
* Implemented adaptive anomaly detection with Redis-based leader election; achieved `<X>` events/s at p95 `<Y>` ms.

## Interview talking points
**Why RabbitMQ + decoupled ingest?** Ingestion latency shouldn't depend on DB speed; the queue absorbs bursts; DLQ isolates poison messages.
**Exactly-once?** No - at-least-once delivery plus idempotent writes gives effectively-once results for events; incidents are de-duplicated by fingerprint.
**How does the detector avoid false positives?** Needs a minimum event count, thresholds are `max(floor, mean + 3 sigma)` per service, anomalous windows are excluded from the baseline, notifications have a cooldown.
**What if two processors run the detector?** They can't: Redis Lua lock with TTL; even if the lock briefly overlaps, alert-service de-dupes.
**What breaks first at 10x load?** Postgres insert rate (add partitioning/batching or Timescale), RabbitMQ single node (cluster/quorum queues or Kafka), single Redis (replicas/Sentinel).
**Why not liveness on dependencies?** A DB outage would restart every pod and make recovery worse; readiness removes pods from service instead.
**Security?** API keys with constant-time comparison, rate limiting, non-root + read-only FS, default-deny NetworkPolicies, secrets not in images; known gaps: no TLS on the lab gateway, ES security disabled (internal-only).
**What would you add next?** OpenTelemetry tracing, seasonal anomaly detection, Alembic migrations, TLS via cert-manager, ArgoCD, SLO dashboards.
**Azure mapping:** see the end of `ARCHITECTURE.md` (AKS, Service Bus, Azure Monitor, etc.).

## Measure these before the interview
`make loadtest` (events/s, p95) - kill a processor pod during load and note recovery - time from injected fault to Teams alert - ELK memory footprint (`kubectl top pods`).
