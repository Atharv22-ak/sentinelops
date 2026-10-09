REGISTRY   ?= 116261338703.dkr.ecr.ap-south-1.amazonaws.com
# one ECR repo for every service: image = $(REGISTRY)/$(REPOSITORY):<service>-<tag>. Empty REPOSITORY = one repo per service.
REPOSITORY ?= sentinelops
# Immutable tag: the Git commit. Never deploy v1/latest (the chart rejects them). CI pushes the full SHA; locally the short one.
TAG        ?= $(shell git rev-parse --short=12 HEAD 2>/dev/null)
PLATFORM   ?= linux/amd64
NAMESPACE  ?= sentinelops
RELEASE    ?= sentinelops
CHART      := deploy
SERVICES   := ingest_api processor alert_service query_api loadgen
img         = $(if $(REPOSITORY),$(REGISTRY)/$(REPOSITORY):$(subst _,-,$(1))-$(TAG),$(REGISTRY)/sentinelops-$(subst _,-,$(1)):$(TAG))

# --- secrets for `make deploy` -------------------------------------------------------------------------------
# Preferred: SECRET_NAME=<existing k8s Secret> (create it with `make secret`, or let External Secrets manage it).
# Dev only : PG_PASSWORD / MQ_PASSWORD / API_KEY (end up in the Helm release object in the cluster).
SECRET_NAME ?=
PG_PASSWORD ?=
MQ_PASSWORD ?=
API_KEY     ?=
WEBHOOK_URL ?=
ifneq ($(SECRET_NAME),)
SECRET_ARGS = --set secrets.existingSecret=$(SECRET_NAME)
else
SECRET_ARGS = --set secrets.postgresPassword=$(PG_PASSWORD) --set secrets.rabbitmqPassword=$(MQ_PASSWORD) \
              --set secrets.apiKeys=$(API_KEY) --set-string secrets.webhookUrl='$(WEBHOOK_URL)'
endif
# Every service gets its own tag. TAG is the default for all of them; override one, e.g.  make deploy PROCESSOR_TAG=abc123def456
INGEST_TAG    ?= $(TAG)
PROCESSOR_TAG ?= $(TAG)
ALERT_TAG     ?= $(TAG)
QUERY_TAG     ?= $(TAG)
LOADGEN_TAG   ?= $(TAG)
TAG_ARGS = --set global.imageTag=$(TAG) --set ingestApi.image.tag=$(INGEST_TAG) --set processor.image.tag=$(PROCESSOR_TAG) \
           --set alertService.image.tag=$(ALERT_TAG) --set queryApi.image.tag=$(QUERY_TAG) --set loadgen.image.tag=$(LOADGEN_TAG)
HELM_COMMON = -f $(CHART)/values-cluster.yaml $(TAG_ARGS) --set global.imageRegistry=$(REGISTRY) --set global.imageRepository=$(REPOSITORY) $(SECRET_ARGS)

# --- ops tooling -------------------------------------------------------------------------------------------
METRICS_SERVER_VERSION ?= v0.7.2
# kubeadm clusters usually need --kubelet-insecure-tls (kubelet certs lack IP SANs); set to false if yours has them
METRICS_SERVER_INSECURE_TLS ?= true
# load-test target URL (URL= still works for backwards compatibility)
LT_URL      ?= $(URL)
LT_DURATION ?= 60

AWS_REGION ?= ap-south-1
ECR_HOST   ?= 116261338703.dkr.ecr.$(AWS_REGION).amazonaws.com

.PHONY: ecr-login ecr-secret help install lint test build push deploy deploy-elk elk-on elk-off helm-check secret undeploy \
        metrics-server monitoring-on monitoring-off pf-kibana pf-rabbit pf-grafana logs \
        loadtest loadtest-smoke loadtest-ramp loadtest-soak loadtest-prep loadtest-restore chaos up down

help:            ## show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n",$$1,$$2}'

ecr-login:       ## docker login to ECR (needs aws cli + credentials)
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(ECR_HOST)

ecr-secret:      ## create the initial `regcred` pull secret (the in-cluster CronJob refreshes it afterwards)
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n $(NAMESPACE) create secret docker-registry regcred --docker-server=$(ECR_HOST) --docker-username=AWS \
	  --docker-password="$$(aws ecr get-login-password --region $(AWS_REGION))" --dry-run=client -o yaml | kubectl apply -f -

install:         ## dev dependencies
	pip install -r requirements-dev.txt

lint:            ## ruff
	ruff check services tests scripts

test:            ## unit tests
	python -m pytest -q

build:           ## build all images tagged with the Git SHA (linux/amd64 for the EC2 nodes)
	@test -n "$(TAG)" || (echo "TAG is empty - not inside a git checkout? pass TAG=<git sha>"; exit 1)
	@for s in $(SERVICES); do \
	  n=$$(echo $$s | tr _ -); echo "==> $$s"; \
	  docker build --platform $(PLATFORM) -f services/Dockerfile --build-arg SERVICE=$$s \
	    --label org.opencontainers.image.revision=$(TAG) -t $(call img,$$n) . || exit 1; \
	done

push:            ## push all images
	@for s in $(SERVICES); do n=$$(echo $$s | tr _ -); docker push $(call img,$$n) || exit 1; done

helm-check:      ## lint + render (ELK off / on / monitoring+keda+externalsecret on) with throw-away values
	helm lint $(CHART) --set global.imageTag=$(TAG) --set secrets.postgresPassword=lint0only --set secrets.rabbitmqPassword=lint0only --set secrets.apiKeys=lint0only
	helm template $(RELEASE) $(CHART) -n $(NAMESPACE) --set global.imageTag=$(TAG) --set secrets.postgresPassword=lint0only --set secrets.rabbitmqPassword=lint0only --set secrets.apiKeys=lint0only > /dev/null
	helm template $(RELEASE) $(CHART) -n $(NAMESPACE) --set global.imageTag=$(TAG) --set secrets.postgresPassword=lint0only --set secrets.rabbitmqPassword=lint0only --set secrets.apiKeys=lint0only --set elk.enabled=true > /dev/null
	helm template $(RELEASE) $(CHART) -n $(NAMESPACE) --set global.imageTag=$(TAG) --set elk.enabled=true --set monitoring.enabled=true --set keda.enabled=true \
	  --set secrets.externalSecret.enabled=true --set secrets.externalSecret.secretStoreRef.name=store > /dev/null
	@echo "chart renders OK"

secret:          ## create/update the app Secret out-of-band (needs PG_PASSWORD MQ_PASSWORD API_KEY)
	@test -n "$(PG_PASSWORD)" -a -n "$(MQ_PASSWORD)" -a -n "$(API_KEY)" || (echo "set PG_PASSWORD, MQ_PASSWORD, API_KEY"; exit 1)
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n $(NAMESPACE) create secret generic $(RELEASE)-secrets \
	  --from-literal=POSTGRES_PASSWORD='$(PG_PASSWORD)' \
	  --from-literal=POSTGRES_DSN='postgresql://sentinel:$(PG_PASSWORD)@$(RELEASE)-postgres:5432/sentinel' \
	  --from-literal=RABBITMQ_DEFAULT_PASS='$(MQ_PASSWORD)' \
	  --from-literal=AMQP_URL='amqp://sentinel:$(MQ_PASSWORD)@$(RELEASE)-rabbitmq:5672/' \
	  --from-literal=API_KEYS='$(API_KEY)' --from-literal=INGEST_API_KEY='$(firstword $(subst $(comma), ,$(API_KEY)))' \
	  --from-literal=WEBHOOK_URL='$(WEBHOOK_URL)' --dry-run=client -o yaml | kubectl apply -f -
	@echo "now:  make deploy SECRET_NAME=$(RELEASE)-secrets"
comma := ,

deploy:          ## install/upgrade WITHOUT ELK  (SECRET_NAME=... or PG_PASSWORD MQ_PASSWORD API_KEY)
	@test -n "$(SECRET_NAME)" -o \( -n "$(PG_PASSWORD)" -a -n "$(MQ_PASSWORD)" -a -n "$(API_KEY)" \) || (echo "set SECRET_NAME (preferred) or PG_PASSWORD, MQ_PASSWORD, API_KEY"; exit 1)
	helm upgrade --install $(RELEASE) $(CHART) -n $(NAMESPACE) --create-namespace $(HELM_COMMON) \
	  --set elk.enabled=false --wait --timeout 10m

deploy-elk:      ## install/upgrade WITH ELK
	@test -n "$(SECRET_NAME)" -o \( -n "$(PG_PASSWORD)" -a -n "$(MQ_PASSWORD)" -a -n "$(API_KEY)" \) || (echo "set SECRET_NAME (preferred) or PG_PASSWORD, MQ_PASSWORD, API_KEY"; exit 1)
	helm upgrade --install $(RELEASE) $(CHART) -n $(NAMESPACE) --create-namespace $(HELM_COMMON) \
	  --set elk.enabled=true --wait --timeout 15m

elk-on:          ## flip ELK on for a running release (keeps all other values)
	helm upgrade $(RELEASE) $(CHART) -n $(NAMESPACE) --reuse-values --set elk.enabled=true --wait --timeout 15m

elk-off:         ## flip ELK off (PVC with indices is kept)
	helm upgrade $(RELEASE) $(CHART) -n $(NAMESPACE) --reuse-values --set elk.enabled=false

undeploy:        ## remove the release (PVCs are kept)
	helm uninstall $(RELEASE) -n $(NAMESPACE)

metrics-server:  ## install metrics-server (kubectl top + CPU/memory HPA)
	kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/$(METRICS_SERVER_VERSION)/components.yaml
	@if [ "$(METRICS_SERVER_INSECURE_TLS)" = "true" ]; then \
	  kubectl -n kube-system patch deployment metrics-server --type=json \
	    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || true; fi
	kubectl -n kube-system rollout status deployment/metrics-server --timeout=3m
	@echo "wait ~1 min, then:  kubectl top pods -n $(NAMESPACE)"

monitoring-on:   ## ServiceMonitors + alert rules + Grafana dashboard + exporters (needs kube-prometheus-stack)
	helm upgrade $(RELEASE) $(CHART) -n $(NAMESPACE) --reuse-values --set monitoring.enabled=true --wait --timeout 10m

monitoring-off:
	helm upgrade $(RELEASE) $(CHART) -n $(NAMESPACE) --reuse-values --set monitoring.enabled=false

pf-kibana:       ## http://localhost:5601
	kubectl -n $(NAMESPACE) port-forward svc/$(RELEASE)-kibana 5601:5601

pf-rabbit:       ## http://localhost:15672 (user: sentinel)
	kubectl -n $(NAMESPACE) port-forward svc/$(RELEASE)-rabbitmq 15672:15672

pf-grafana:      ## http://localhost:3000 (GRAFANA_NS/GRAFANA_SVC default to kube-prometheus-stack)
	kubectl -n $${GRAFANA_NS:-monitoring} port-forward svc/$${GRAFANA_SVC:-kube-prometheus-stack-grafana} 3000:80

logs:            ## tail processor logs
	kubectl -n $(NAMESPACE) logs -f deploy/$(RELEASE)-processor

# ---- load tests: LT_URL=http://... API_KEY=... make loadtest-ramp ----------------------------------------------
loadtest:        ## quick single run (30s, 20 workers, batch 100) - backwards compatible
	python scripts/loadtest.py --url $(LT_URL) --api-key $(API_KEY) --duration 30 --concurrency 20 --batch 100

loadtest-smoke:  ## 30s smoke test with pass/fail thresholds
	python scripts/loadtest.py --url $(LT_URL) --api-key $(API_KEY) --duration 30 --concurrency 10 --batch 100

loadtest-ramp:   ## progressive: concurrency 10,25,50,100 x batch 100,500 (stops at first failing stage)
	python scripts/loadtest.py --url $(LT_URL) --api-key $(API_KEY) --stages 10,25,50,100 --batches 100,500 \
	  --duration $(LT_DURATION) --json loadtest-report.json

loadtest-soak:   ## 5 minute soak - run only after smoke + ramp pass
	python scripts/loadtest.py --url $(LT_URL) --api-key $(API_KEY) --duration 300 --concurrency 25 --batch 100 --json loadtest-soak.json

loadtest-prep:   ## raise the rate limit / batch cap so the test measures capacity (restore afterwards!)
	helm upgrade $(RELEASE) $(CHART) -n $(NAMESPACE) --reuse-values --set config.RATE_LIMIT_PER_MINUTE=100000000 --set config.MAX_BATCH_SIZE=1000 --wait

loadtest-restore: ## back to the chart defaults for RATE_LIMIT_PER_MINUTE / MAX_BATCH_SIZE
	helm upgrade $(RELEASE) $(CHART) -n $(NAMESPACE) --reuse-values --set config.RATE_LIMIT_PER_MINUTE=6000 --set config.MAX_BATCH_SIZE=500 --wait

chaos:           ## NON-PROD only: CONFIRM=yes make chaos  (pod-delete, OOM, dependency outages; see scripts/chaos.sh)
	NAMESPACE=$(NAMESPACE) RELEASE=$(RELEASE) ./scripts/chaos.sh all

up:              ## local stack with docker compose
	docker compose up --build -d
down:
	docker compose down -v
