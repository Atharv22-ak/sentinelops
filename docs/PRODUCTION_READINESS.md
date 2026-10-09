# Production readiness

Implements the *SentinelOps - Production Readiness Changes* review. Everything below is in this repo; items that need
something installed in the cluster or a one-time manual step are listed in [Manual steps](#manual-steps-that-cannot-live-in-git).

| P | Change | Where | How to use it |
|---|---|---|---|
| P0 | Secret / API key management | `deploy/templates/secrets/`, `Makefile` (`secret`) | No defaults any more. `secrets.externalSecret.enabled=true` (AWS Secrets Manager / Azure Key Vault via External Secrets), or `secrets.existingSecret=<name>` (`make secret`). The chart **refuses to render** without credentials or with `change-me*` values. |
| P0 | Immutable / private images | `_helpers.tpl`, `values.yaml` `global.*`, CI, `Makefile` | **Every service has its own tag** (`ingestApi.image.tag`, `processor.image.tag`, ... in `values-cluster.yaml`), so services can be rolled forward/back independently; `global.imageTag` is only an optional fallback. A missing tag fails the render and `v1`/`latest`/`dev`/`stable` are rejected. CI pushes only the Git SHA tag (no `:latest`) with OCI revision labels. Images live in **one repo** (`global.imageRepository: sentinelops`), told apart by tag: `sentinelops:ingest-api-<sha>`, `sentinelops:processor-<sha>`, ... (leave it empty for one repo per service). `global.imageRegistry` can point at ECR/ACR; `global.imagePullSecrets` for registry auth. |
| P0 | Metrics Server + basic monitoring | `make metrics-server`, `templates/monitoring/*`, `dashboards/` | `make metrics-server` then `kubectl top pods`. `make monitoring-on` wires Prometheus/Grafana. |
| P1 | HPA / KEDA | `templates/autoscaling/`, `values.yaml` `autoscaling`/`keda` | CPU HPAs on ingest-api and processor (min/max + stabilisation windows to stop flapping). `--set keda.enabled=true` swaps the processor HPA for RabbitMQ queue-depth scaling. |
| P1 | Prometheus / Grafana | `templates/monitoring/` | ServiceMonitors for the apps, RabbitMQ (`rabbitmq_prometheus`), Redis (exporter sidecar), Elasticsearch (exporter); `PrometheusRule` alerts; 23-panel Grafana dashboard ConfigMap. |
| P1 | Load-test thresholds | `scripts/loadtest.py`, `Makefile` (`loadtest-*`), `tests/test_loadtest.py` | Exit code 1 when a threshold is missed (defaults: success >= 99 %, 5xx <= 1 %, p95 <= 500 ms, p99 <= 1000 ms). Progressive ramp stops at the first failing stage. |
| P1 | SecurityContext hardening | `_helpers.tpl`, `postgres.yaml`, `redis.yaml`, `rabbitmq.yaml`, `elk/*` | Apps were already non-root / read-only FS / no caps. Now also: dedicated ServiceAccount per component, Redis/ES/Kibana/Logstash/bootstrap non-root with caps dropped, Postgres/RabbitMQ `drop: [ALL]` + only the 5 capabilities their entrypoints need, seccomp `RuntimeDefault` everywhere. |
| P1 | NetworkPolicies | `templates/networkpolicy.yaml` | The "all pods may talk to all pods" rule is gone. Default-deny ingress + one policy per backend listing exactly who may connect (see table below). |
| P2 | CI/CD scanning | `.github/workflows/ci.yml` | lint/tests -> helm lint/render/kubeconform + Trivy config scan -> build -> Trivy image scan (blocks fixable HIGH/CRITICAL) -> push SHA -> bump `values-cluster.yaml` -> Argo CD (`argocd/application.yaml`). |
| P2 | Failure / chaos testing | `scripts/chaos.sh`, `make chaos` | pod-delete, OOM, and RabbitMQ/Redis/Postgres/Elasticsearch outages, each with a recovery assertion and optional Alertmanager check. |

Also covered from the review: **resources** (every workload, including the init and exporter containers, has requests/limits; the 256Mi
processor limit is documented as a measured-sizing task, see the RUNBOOK), **health checks** (startupProbe added so liveness can stay
relaxed; readiness already reflects Redis/RabbitMQ/Postgres via `/readyz`), **external access** (Kibana/RabbitMQ/ES/Redis stay ClusterIP-only;
`gateway.scheme` / `gateway.sectionName` attach routes to a TLS listener).

## Who may talk to whom (NetworkPolicy)

| Target | Allowed sources |
|---|---|
| ingest-api :8000 | Envoy Gateway namespace, loadgen, Prometheus |
| query-api :8000 | Envoy Gateway namespace, Prometheus |
| processor / alert-service / loadgen :8000 | Prometheus only (metrics) |
| redis :6379 | ingest-api, processor, alert-service, query-api |
| rabbitmq :5672 | ingest-api, processor, alert-service |
| postgres :5432 | processor, alert-service, query-api |
| elasticsearch :9200 | logstash, kibana, elk-bootstrap, es-exporter |
| kibana :5601 | elk-bootstrap (+ Envoy Gateway only if `elk.kibana.expose=true`) |
| logstash :5044 | filebeat |

Not covered: **egress** policies (alert-service needs the internet for webhooks, everything needs DNS) - a sensible next step.

## Manual steps that cannot live in Git

1. **Rotate credentials.** `change-me-key-1` and anything shared while testing must be replaced. Note that `POSTGRES_PASSWORD` /
   `RABBITMQ_DEFAULT_PASS` are only read when the data volume is first initialised, so on the running cluster also change them *inside*
   the services: `ALTER USER sentinel PASSWORD '...'` (psql) and `rabbitmqctl change_password sentinel '...'`, then update the Secret.
2. **Pick the secret path** in `deploy/values-cluster.yaml` (ExternalSecret or `existingSecret`) - until then the chart intentionally fails to render.
3. **Install cluster add-ons you want to use:** metrics-server (`make metrics-server`), kube-prometheus-stack (Prometheus, Grafana,
   Alertmanager, kube-state-metrics), KEDA (optional), External Secrets Operator (optional), Argo CD (optional).
   Give your Prometheus a selector that matches `monitoring.serviceMonitor.labels` / `monitoring.prometheusRule.labels` (usually `release: <helm release>`).
4. **GitHub:** secrets `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`; optional variables `IMAGE_REGISTRY`, `REGISTRY_HOST` (ECR/ACR).
   The `bump` job pushes to `main` - allow `github-actions[bot]` in branch protection, or change it to open a PR.
5. **TLS:** terminate TLS on the shared Gateway (cert-manager / Envoy Gateway listener), then set `gateway.scheme=https` and `gateway.sectionName=<listener>`.
   Put authentication in front of anything you expose (Kibana has none - keep `elk.kibana.expose=false` and use `make pf-kibana`).
6. **Upgrade window:** the first upgrade changes the Postgres, RabbitMQ and Elasticsearch pod templates, so those StatefulSets restart once.
   If either Postgres or RabbitMQ refuses to start under the reduced capability set, remove `capabilities.add/drop` from that template and open an issue - do this in staging first.
7. **Finalise sizing from data.** HPA targets (70 % CPU), `keda.queueLength`, min/max replicas and the processor memory limit are starting points;
   set them from `make loadtest-ramp` results and the Grafana memory panel (see RUNBOOK -> "Sizing").

## Chart layout

```
deploy/
  Chart.yaml  values.yaml  values-cluster.yaml
  dashboards/sentinelops.json          Grafana dashboard (loaded into a ConfigMap)
  templates/
    _helpers.tpl  NOTES.txt
    apps/          ingest-api.yaml  processor.yaml  alert-service.yaml  query-api.yaml  loadgen.yaml
    configmaps/    app-config.yaml   (the single ConfigMap built from the `config:` values section)
    secrets/       secret.yaml  externalsecret.yaml
    httproutes/    httproutes.yaml   (dashboard / ingest / optional kibana)
    data/          postgres.yaml  redis.yaml  rabbitmq.yaml
    autoscaling/   hpa.yaml  keda.yaml  pdb.yaml
    security/      serviceaccount.yaml  networkpolicy.yaml
    monitoring/    servicemonitor.yaml  prometheusrule.yaml  dashboard.yaml
    elk/           elasticsearch / logstash / kibana / filebeat / es-exporter / bootstrap-job
```

Per-service image tags, e.g. in `values-cluster.yaml`:

```yaml
ingestApi:  {image: {tag: "9f2c1ab04d11"}}   # -> sentinelops:ingest-api-9f2c1ab04d11
processor:  {image: {tag: "3be77c0aa921"}}   # -> sentinelops:processor-3be77c0aa921 (can differ from ingest-api)
```

## ECR on a kubeadm cluster (pull secret refresh)

No EKS here, so nodes cannot log in to ECR by themselves. `ecr.refresh.enabled=true` (already on in `values-cluster.yaml`) adds a CronJob
(`templates/registry/ecr-refresh.yaml`) that rewrites the `regcred` docker-registry Secret every 6 hours; all pods use it automatically.

```bash
# one-time, from a machine with aws cli + kubectl:
make ecr-secret                       # first regcred, so the very first pods can pull
# CronJob authentication - pick one:
#  a) node instance role with AmazonEC2ContainerRegistryReadOnly + IMDS hop limit 2 (no keys in the cluster):
aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2 --http-tokens required
#  b) IAM user with ONLY ecr:GetAuthorizationToken, key stored as a Secret, then --set ecr.refresh.awsCredentialsSecret=ecr-aws-creds:
kubectl -n sentinelops create secret generic ecr-aws-creds --from-literal=AWS_ACCESS_KEY_ID=... --from-literal=AWS_SECRET_ACCESS_KEY=...
```
Check it: `kubectl -n sentinelops create job --from=cronjob/sentinelops-ecr-refresh test-refresh && kubectl -n sentinelops logs job/test-refresh`.
