# Runbook

## Prerequisites
* kubectl access (laptop kubeconfig already points at the Elastic IP), Helm 3.14+, Docker with push access to Docker Hub.
* The `main-gateway` Gateway in `default` must allow routes from other namespaces
  (`kubectl get gateway main-gateway -o yaml` -> `allowedRoutes.namespaces.from: All`). ShopMesh already relies on this.
* Security group: NodePort 32136 open (already true for ShopMesh).
* Building on Apple Silicon? The Makefile already passes `--platform linux/amd64`.

## First deploy
```bash
make test helm-check
make build push                                           # TAG defaults to the short Git SHA (immutable; v1/latest are rejected)
export PG_PASSWORD=... MQ_PASSWORD=... API_KEY=...       # URL-safe characters only (hex is fine)
make secret WEBHOOK_URL='https://...'                     # webhook optional; creates <release>-secrets outside Git
make deploy SECRET_NAME=sentinelops-secrets               # WEBHOOK_KIND defaults to teams
kubectl -n sentinelops get pods -w
```
GitOps instead: set `secrets.existingSecret` or `secrets.externalSecret` in `deploy/values-cluster.yaml`, then
`kubectl apply -n argocd -f argocd/application.yaml`; CI pins the image tag after each green build.

## Enable ELK
```bash
make elk-on                      # waits for ES/Kibana, runs the bootstrap job (ILM + index template + data view)
kubectl -n sentinelops get pods  # elasticsearch-0, logstash, kibana, filebeat (1 per node)
make pf-kibana                   # http://localhost:5601 -> Discover -> data view "SentinelOps Logs"
```
Useful Kibana queries: `app_log.level : "WARNING"` · `app_log.kind : "error_rate_spike"` · `kubernetes.container.name : "processor"`.

If pods go `Pending`/`OOMKilled`: `kubectl describe node | grep -A8 "Allocated resources"`, then reduce replicas
(`--set ingestApi.replicas=1 --set processor.replicas=1`), disable loadgen, or scale ShopMesh down while ELK is on.

## Autoscaling, monitoring, load tests
```bash
make metrics-server                       # once; then `kubectl top pods -n sentinelops` and HPAs show real numbers
kubectl -n sentinelops get hpa            # ingest-api / processor (CPU 70 %), behaviour: scale up fast, down slowly
helm upgrade sentinelops deploy -n sentinelops --reuse-values --set keda.enabled=true   # queue-depth scaling for the processor
make monitoring-on                        # needs kube-prometheus-stack; dashboard "SentinelOps - Overview", alerts prefixed SentinelOps*
```
Load testing (ingest API). Raise the guard rails first or you will measure the rate limiter (HTTP 429 at 100 events/s/key) and the batch cap (413 above 500):
```bash
make loadtest-prep                                        # RATE_LIMIT_PER_MINUTE=1e8, MAX_BATCH_SIZE=1000
LT_URL=http://sentinel-ingest.3.108.88.140.nip.io:32136 API_KEY=... make loadtest-smoke   # 30s, 10 workers
LT_URL=... API_KEY=... make loadtest-ramp                 # 10/25/50/100 workers x batch 100/500, stops at first failing stage
LT_URL=... API_KEY=... make loadtest-soak                 # 5 minutes - only after the above pass
make loadtest-restore                                     # always put the limits back
```
Pass/fail defaults: success >= 99 %, 5xx+errors <= 1 %, p95 <= 500 ms, p99 <= 1000 ms (override with `--max-p95-ms` etc.). The exit code is 1 on failure.
Watch Grafana (CPU/memory/restarts/queue depth) while the ramp runs; the first failing stage tells you which resource to look at.

## OOMKilled: investigate before raising the limit
The 256Mi processor limit was deliberately exceeded: exit code 137, `OOMKilled`, restart count incremented - self-healing works.
```bash
kubectl -n sentinelops get pod -l app.kubernetes.io/component=processor \
  -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.containerStatuses[0].lastState.terminated.reason}{"  restarts="}{.status.containerStatuses[0].restartCount}{"\n"}{end}'
kubectl -n sentinelops top pod -l app.kubernetes.io/component=processor        # needs metrics-server
```
Sizing: run `make loadtest-ramp` and read the *working set* panel (or `max_over_time(container_memory_working_set_bytes{container="app"}[1h])`).
Set `requests` ~ typical usage, `limits` ~ peak + 25-30 % headroom. Growth that does not flatten under constant load is a leak - fix it, do not just raise the limit.
(`SentinelOpsContainerOOMKilled` alerts on this when monitoring is on.)

## Failure & recovery tests (non-production)
```bash
CONFIRM=yes scripts/chaos.sh pod-delete processor      # pod replaced automatically, queue buffers, other replica holds the leader lock
CONFIRM=yes scripts/chaos.sh oom                       # processor memory exhaustion -> OOMKilled -> restart
CONFIRM=yes HOLD=400 ALERTMANAGER_URL=http://localhost:9093 scripts/chaos.sh dep-down rabbitmq   # outage, alert fires, recovery without manual steps
CONFIRM=yes scripts/chaos.sh all
```
Pause Argo CD auto-sync first (`argocd app set sentinelops --sync-policy none`), otherwise selfHeal reverts the outage.

## Demo script (5 minutes)
1. Open the dashboard: 5 simulated services, all healthy.
2. loadgen injects a fault every 5 min (`FAULT_EVERY_SEC`). Within ~1 min the affected service turns red and an incident appears.
3. Show the Teams/Slack message, ack the incident in the dashboard.
4. `make elk-on`, open Kibana, filter on the incident's service to see the related structured logs.
5. `kubectl delete pod -l app.kubernetes.io/component=processor` -> events keep flowing (queue buffers, other replica takes the leader lock).
6. `LT_URL=... API_KEY=... make loadtest-smoke` -> report events/s and p95 latency (pass/fail).

## Troubleshooting
| Symptom | Check |
|---|---|
| Pods `CrashLoopBackOff` right after install | `kubectl logs` - apps retry DB/MQ for ~2 min; persistent failure = wrong password/DSN (special characters in passwords break the DSN) |
| 404 from the gateway | `kubectl -n sentinelops get httproute`; hostname must match `*.nip.io` host exactly, include `:32136` |
| HTTPRoute `Accepted=False` | Gateway listener `allowedRoutes` does not permit this namespace |
| No incidents | `kubectl -n sentinelops logs deploy/sentinelops-processor` -> "anomaly detected"; need >= `MIN_EVENTS` (20) events per 60 s window and ~5 healthy windows of warm-up to adapt thresholds (absolute floors apply meanwhile) |
| Events accepted but dashboard empty | RabbitMQ UI (`make pf-rabbit`) -> queue `events.raw` depth; `sentinel.dead` holds poison messages |
| Elasticsearch stuck / `max virtual memory` error | init container sets `vm.max_map_count` (needs privileged pods allowed) |
| Filebeat sends nothing | `kubectl -n sentinelops logs ds/sentinelops-filebeat`; log path pattern uses the release namespace |
| HPA shows `<unknown>` | metrics-server is not installed (`make metrics-server`); on kubeadm clusters it needs `--kubelet-insecure-tls` (the target adds it) |
| `helm` fails with `<service>.image.tag is required` / `secrets.* is required` | by design: pass an immutable tag and real credentials (`make deploy SECRET_NAME=...`) |
| `ImagePullBackOff` / 401 from ECR | `regcred` expired or missing: `make ecr-secret`; check the refresh CronJob (`kubectl -n sentinelops get cronjob,job`) and see docs/PRODUCTION_READINESS.md |
| Pod cannot reach a backend after an upgrade | the NetworkPolicies are per-pair; add the missing source to `deploy/templates/security/networkpolicy.yaml` (table in docs/PRODUCTION_READINESS.md) |
| Load test shows lots of 429 / 413 | `make loadtest-prep` (rate limit / batch cap), restore with `make loadtest-restore` |
| Data lost after pod moved nodes | `local-path` PVCs are node-bound (known limitation of this cluster) |

## Rollback / removal
`helm history sentinelops -n sentinelops` · `helm rollback sentinelops <rev> -n sentinelops` · `make undeploy` (PVCs stay; delete them to wipe data).
