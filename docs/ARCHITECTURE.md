# Architecture & design decisions

## Data flow
1. **ingest-api** authenticates (`X-API-Key`), enforces a per-key *events/minute* limit (Redis `INCRBY` fixed window),
   validates with Pydantic, stamps each event with a UUID and publishes **one persistent message per batch** to a topic exchange.
   It never touches Postgres, so ingestion latency is independent of storage.
2. **processor** consumes `events.raw` (prefetch 50), bulk-inserts with `ON CONFLICT (event_id) DO NOTHING`
   (redelivery-safe, i.e. at-least-once delivery + idempotent consumer), and rolls events into 10-second buckets in Redis.
3. A **detector loop** (only on the elected leader) evaluates the last 6 completed buckets per service and publishes `incident.detected`.
4. **alert-service** de-duplicates by fingerprint (`service:kind`), persists, applies a notification cooldown, sends
   Teams (Adaptive Card) / Slack / generic webhooks with retry + backoff, and auto-resolves incidents that stop firing.
5. **query-api** serves the dashboard and REST API, with a 5 s Redis read-through cache.

## Decisions and trade-offs
| Decision | Why | Trade-off / what I'd change at larger scale |
|---|---|---|
| RabbitMQ topic exchange + DLX | Simple routing, per-queue durability, poison messages go to `sentinel.dead` (`requeue=False`) | Kafka/Event Hubs for replay and much higher throughput |
| Batch per message | Amortises broker overhead | Larger blast radius per poison message |
| Idempotent inserts (`event_id` unique) | Broker redelivery must not duplicate data | Unique index cost on a hot table; partitioning by day at scale |
| Detector = EWMA mean/variance + absolute floors | Adapts per service (noisy services don't page), no ML dependency, explainable | Not seasonal; would add STL/Prophet or rate-of-change rules |
| Anomalous windows are *not* learned | An outage must not become the new "normal" | Slow to adapt to a genuine permanent shift |
| Leader election via Redis Lua lock (`SET NX EX` + renew) | Lets processors scale horizontally while only one runs detection | Not a fencing-safe lock; fine because actions are idempotent (alert-service de-dupes) |
| Redis holds only rebuildable state, `volatile-lru`, no persistence | Baselines/buckets self-heal after restart (short warm-up) | Brief detection blind spot after a Redis restart |
| Schema migration at start-up behind `pg_advisory_lock` | No race between replicas, no extra job | Use Alembic once the schema starts evolving |
| Structured JSON logs to stdout | 12-factor; Filebeat/Logstash parse them with zero regex | - |
| Gateway API HTTPRoutes | Reuses the cluster's Envoy Gateway; same pattern as other workloads | NodePort + Elastic IP instead of a cloud LB (no cloud controller on kubeadm) |
| NetworkPolicy default-deny + allow-list | Only Envoy can reach ingest/query; data stores are internal-only | Needs a CNI that enforces policies (Calico does) |

## Reliability
* Readiness probes check dependencies (`/readyz`), liveness is dependency-free (`/healthz`) so a DB outage does not restart-loop pods.
* Connection retry loops with back-off for Postgres/RabbitMQ at start-up, `connect_robust` for automatic RabbitMQ reconnects.
* `maxUnavailable: 0` rolling updates, PodDisruptionBudgets for replicated components, topology spread across nodes.
* Pods run non-root, read-only root filesystem, all capabilities dropped, seccomp `RuntimeDefault`.

## Observability of the platform itself
* Prometheus metrics on every service: request rate/latency, events ingested/processed, batch processing time,
  incidents published, notifications sent, detector leader gauge. Pods carry `prometheus.io/*` annotations.
* With `elk.enabled=true`: Filebeat (DaemonSet) -> Logstash (JSON parse, timestamp from the app) -> daily
  `sentinelops-logs-*` indices with an ILM delete policy -> Kibana data view created by a Helm post-install hook.

## ELK toggle
`elk.enabled` gates every ELK template. Turning it off removes the pods but leaves PVCs, so indices survive a later re-enable.
Memory is the constraint on 4 GB workers: ELK requests about 2.1 GiB (Elasticsearch 896Mi + Logstash 512Mi + Kibana 512Mi + Filebeat per node).
Scale the other workloads down first, or move workers to a larger instance type.

## Security notes
* Elasticsearch/Kibana run with `xpack.security.enabled=false` for the lab setup: they are cluster-internal only,
  and Kibana is **not** routed through the public gateway unless `elk.kibana.expose=true` (don't, without auth).
* Secrets live in a Kubernetes Secret created from `--set` values. For anything beyond a lab use `secrets.existingSecret`
  with Sealed Secrets / External Secrets.
* The ingest endpoint is public; protection is API key + rate limit. TLS needs a Gateway listener + cert-manager (not included).

## How this maps to Azure (useful for interviews)
RabbitMQ -> Azure Service Bus / Event Hubs · Postgres -> Azure Database for PostgreSQL · Redis -> Azure Cache for Redis ·
Helm on kubeadm -> AKS · ELK -> Azure Monitor / Log Analytics · Envoy Gateway -> AKS Application Gateway for Containers · Teams webhook as the alert sink.
