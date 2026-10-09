#!/usr/bin/env bash
# Controlled failure & recovery tests for SentinelOps.  NON-PRODUCTION ONLY.
#
#   CONFIRM=yes scripts/chaos.sh pod-delete [component]     delete a pod, verify automatic replacement (default: processor)
#   CONFIRM=yes scripts/chaos.sh oom                        exhaust the processor memory limit, verify OOMKilled + restart
#   CONFIRM=yes scripts/chaos.sh dep-down <dependency>      rabbitmq | redis | postgres | elasticsearch outage + recovery
#   CONFIRM=yes scripts/chaos.sh all                        everything above, one after the other
#
# Env: NAMESPACE (sentinelops)  RELEASE (sentinelops)  HOLD=90 (seconds a dependency stays down)  TIMEOUT=300
#      ALERTMANAGER_URL=http://localhost:9093 (e.g. kubectl -n monitoring port-forward svc/alertmanager-operated 9093)
#      ALERT_WAIT=420 (seconds to wait for the expected alert; alerts have 'for:' delays, so HOLD must be >= ~360 to see them)
#
# If Argo CD manages the release with selfHeal, pause auto-sync first, otherwise it undoes the outage you just created:
#   argocd app set sentinelops --sync-policy none
set -euo pipefail

NS=${NAMESPACE:-sentinelops}
REL=${RELEASE:-sentinelops}
HOLD=${HOLD:-90}
TIMEOUT=${TIMEOUT:-300}
ALERT_WAIT=${ALERT_WAIT:-420}
case "$REL" in *sentinelops*) FULL=$REL ;; *) FULL="$REL-sentinelops" ;; esac
APPS=(ingest-api processor alert-service query-api)

k() { kubectl -n "$NS" "$@"; }
log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILED=1; }
FAILED=0

[ "${CONFIRM:-}" = "yes" ] || die "this deliberately breaks things in namespace '$NS' (context: $(kubectl config current-context)). Re-run with CONFIRM=yes - never against production."

wait_apps_available() {
  for c in "${APPS[@]}"; do k rollout status "deploy/$FULL-$c" --timeout="${TIMEOUT}s" >/dev/null || return 1; done
}

pods_of() { k get pods -l "app.kubernetes.io/instance=$REL,app.kubernetes.io/component=$1" -o jsonpath='{.items[*].metadata.name}'; }
ready_count() { k get pods -l "app.kubernetes.io/instance=$REL,app.kubernetes.io/component=$1" \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].ready}{"\n"}{end}' | grep -c true || true; }

check_alert() {  # $1 = alert name; only when ALERTMANAGER_URL is set
  [ -n "${ALERTMANAGER_URL:-}" ] || { echo "(alert check skipped - set ALERTMANAGER_URL to verify alerts fire)"; return 0; }
  local deadline=$((SECONDS + ALERT_WAIT))
  while [ $SECONDS -lt $deadline ]; do
    if curl -fsS "$ALERTMANAGER_URL/api/v2/alerts?active=true" | grep -q "\"alertname\":\"$1\""; then pass "alert $1 fired"; return 0; fi
    sleep 15
  done
  fail "alert $1 did not fire within ${ALERT_WAIT}s (is monitoring enabled? is HOLD long enough for the rule's 'for:'?)"
}

pod_delete() {
  local c=${1:-processor} victim t0
  victim=$(pods_of "$c" | awk '{print $1}')
  [ -n "$victim" ] || die "no pod found for component $c"
  log "pod-delete: deleting $victim"
  t0=$SECONDS
  k delete pod "$victim" --wait=false >/dev/null
  k rollout status "deploy/$FULL-$c" --timeout="${TIMEOUT}s" >/dev/null
  if pods_of "$c" | grep -qw "$victim"; then fail "$victim still exists"; else pass "$victim replaced; deployment healthy again after $((SECONDS - t0))s"; fi
}

oom() {
  local pod before after reason t0=$SECONDS
  pod=$(pods_of processor | awk '{print $1}')
  [ -n "$pod" ] || die "no processor pod"
  before=$(k get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')
  log "oom: allocating memory inside $pod (limit: $(k get pod "$pod" -o jsonpath='{.spec.containers[0].resources.limits.memory}'), restarts so far: $before)"
  k exec "$pod" -- python -c "
b=[]
while True: b.append(bytearray(b'x')*(16*1024*1024))" >/dev/null 2>&1 || true
  while [ $((SECONDS - t0)) -lt 120 ]; do
    after=$(k get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}')
    [ "$after" -gt "$before" ] && break
    sleep 5
  done
  reason=$(k get pod "$pod" -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}/{.status.containerStatuses[0].lastState.terminated.exitCode}')
  if [ "${after:-$before}" -gt "$before" ]; then pass "container restarted (lastState: $reason)"; else
    fail "no container restart (the OOM killer may have taken only the exec'd allocator, not PID 1); lastState: $reason"; fi
  k rollout status "deploy/$FULL-processor" --timeout="${TIMEOUT}s" >/dev/null && pass "processor ready again"
  check_alert SentinelOpsContainerOOMKilled
}

dep_down() {
  local dep=${1:-} kind obj expect_apps_down=1 alert=""
  case "$dep" in
    rabbitmq)      kind=statefulset; obj=$FULL-rabbitmq; alert=SentinelOpsNoQueueConsumers ;;
    redis)         kind=deployment;  obj=$FULL-redis ;;
    postgres)      kind=statefulset; obj=$FULL-postgres ;;
    elasticsearch) kind=statefulset; obj=$FULL-elasticsearch; expect_apps_down=0; alert=ElasticsearchClusterRed ;;
    *) die "dependency must be one of: rabbitmq redis postgres elasticsearch" ;;
  esac
  k get "$kind/$obj" >/dev/null 2>&1 || die "$kind/$obj not found (ELK disabled?)"
  local replicas t0
  replicas=$(k get "$kind/$obj" -o jsonpath='{.spec.replicas}')
  log "dep-down: scaling $kind/$obj to 0 for ${HOLD}s"
  k scale "$kind/$obj" --replicas=0 >/dev/null
  sleep "$HOLD"
  local ready_ing; ready_ing=$(ready_count ingest-api)
  if [ "$expect_apps_down" = 1 ]; then
    [ "$ready_ing" -eq 0 ] && pass "ingest-api went NotReady (readiness reflects the outage, traffic is not routed to broken pods)" \
      || echo "NOTE: $ready_ing ingest-api pod(s) still Ready - readiness probes need up to 60s to flip; raise HOLD"
  else
    [ "$ready_ing" -gt 0 ] && pass "core pipeline unaffected by a log-stack outage" || fail "ingest-api went NotReady during an Elasticsearch outage"
  fi
  [ -z "$alert" ] || check_alert "$alert"
  log "dep-down: restoring $kind/$obj to $replicas replica(s) - no manual steps beyond this"
  t0=$SECONDS
  k scale "$kind/$obj" --replicas="$replicas" >/dev/null
  k rollout status "$kind/$obj" --timeout="${TIMEOUT}s" >/dev/null
  if wait_apps_available; then pass "all services available again $((SECONDS - t0))s after restoring $dep"; else fail "services did not recover within ${TIMEOUT}s"; fi
}

case "${1:-}" in
  pod-delete) pod_delete "${2:-processor}" ;;
  oom)        oom ;;
  dep-down)   dep_down "${2:-}" ;;
  all)
    for c in "${APPS[@]}"; do pod_delete "$c"; done
    oom
    for d in redis rabbitmq postgres; do dep_down "$d"; done
    k get "statefulset/$FULL-elasticsearch" >/dev/null 2>&1 && dep_down elasticsearch || echo "(ELK disabled - skipping elasticsearch outage)"
    ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac

echo; [ "$FAILED" = 0 ] && echo "ALL CHECKS PASSED" || { echo "SOME CHECKS FAILED"; exit 1; }
