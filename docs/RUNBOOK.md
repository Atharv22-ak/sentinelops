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
make build push TAG=v1
export PG_PASSWORD=... MQ_PASSWORD=... API_KEY=...       # URL-safe characters only (hex is fine)
make deploy TAG=v1 WEBHOOK_URL='https://...'              # webhook optional; WEBHOOK_KIND defaults to teams
kubectl -n sentinelops get pods -w
```

## Enable ELK
```bash
make elk-on                      # waits for ES/Kibana, runs the bootstrap job (ILM + index template + data view)
kubectl -n sentinelops get pods  # elasticsearch-0, logstash, kibana, filebeat (1 per node)
make pf-kibana                   # http://localhost:5601 -> Discover -> data view "SentinelOps Logs"
```
Useful Kibana queries: `app_log.level : "WARNING"` · `app_log.kind : "error_rate_spike"` · `kubernetes.container.name : "processor"`.

If pods go `Pending`/`OOMKilled`: `kubectl describe node | grep -A8 "Allocated resources"`, then reduce replicas
(`--set ingestApi.replicas=1 --set processor.replicas=1`), disable loadgen, or scale ShopMesh down while ELK is on.

## Demo script (5 minutes)
1. Open the dashboard: 5 simulated services, all healthy.
2. loadgen injects a fault every 5 min (`FAULT_EVERY_SEC`). Within ~1 min the affected service turns red and an incident appears.
3. Show the Teams/Slack message, ack the incident in the dashboard.
4. `make elk-on`, open Kibana, filter on the incident's service to see the related structured logs.
5. `kubectl delete pod -l app.kubernetes.io/component=processor` -> events keep flowing (queue buffers, other replica takes the leader lock).
6. `make loadtest URL=... API_KEY=...` -> report events/s and p95 latency.

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
| HPA shows `<unknown>` | metrics-server is not installed on this cluster (HPA is off by default) |
| Data lost after pod moved nodes | `local-path` PVCs are node-bound (known limitation of this cluster) |

## Rollback / removal
`helm history sentinelops -n sentinelops` · `helm rollback sentinelops <rev> -n sentinelops` · `make undeploy` (PVCs stay; delete them to wipe data).
