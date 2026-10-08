REGISTRY   ?= rwxatharv
TAG        ?= v1
PLATFORM   ?= linux/amd64
NAMESPACE  ?= sentinelops
RELEASE    ?= sentinelops
CHART      := deploy/helm/sentinelops
SERVICES   := ingest_api processor alert_service query_api loadgen
img         = $(REGISTRY)/sentinelops-$(subst _,-,$(1)):$(TAG)

# secrets for `make deploy` (export them or pass on the command line)
PG_PASSWORD ?=
MQ_PASSWORD ?=
API_KEY     ?=
WEBHOOK_URL ?=

.PHONY: help install lint test build push deploy deploy-elk elk-on elk-off helm-check undeploy \
        pf-kibana pf-rabbit logs loadtest up down

help:            ## show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n",$$1,$$2}'

install:         ## dev dependencies
	pip install -r requirements-dev.txt

lint:            ## ruff
	ruff check services tests scripts

test:            ## unit tests
	python -m pytest -q

build:           ## build all images (linux/amd64 for the EC2 nodes)
	@for s in $(SERVICES); do \
	  echo "==> $$s"; \
	  docker build --platform $(PLATFORM) -f services/Dockerfile --build-arg SERVICE=$$s -t $(call img,$$s) . || exit 1; \
	done

push:            ## push all images
	@for s in $(SERVICES); do docker push $(REGISTRY)/sentinelops-$$(echo $$s | tr _ -):$(TAG) || exit 1; done

helm-check:      ## lint + render with and without ELK
	helm lint $(CHART)
	helm template $(RELEASE) $(CHART) -n $(NAMESPACE) > /dev/null
	helm template $(RELEASE) $(CHART) -n $(NAMESPACE) --set elk.enabled=true > /dev/null
	@echo "chart renders OK"

deploy:          ## install/upgrade WITHOUT ELK  (needs PG_PASSWORD MQ_PASSWORD API_KEY)
	@test -n "$(PG_PASSWORD)" -a -n "$(MQ_PASSWORD)" -a -n "$(API_KEY)" || (echo "set PG_PASSWORD, MQ_PASSWORD, API_KEY"; exit 1)
	helm upgrade --install $(RELEASE) $(CHART) -n $(NAMESPACE) --create-namespace \
	  -f $(CHART)/values-cluster.yaml --set global.imageTag=$(TAG) --set global.imageRegistry=$(REGISTRY) \
	  --set secrets.postgresPassword=$(PG_PASSWORD) --set secrets.rabbitmqPassword=$(MQ_PASSWORD) \
	  --set secrets.apiKeys=$(API_KEY) --set-string secrets.webhookUrl='$(WEBHOOK_URL)' \
	  --set elk.enabled=false --wait --timeout 10m

deploy-elk:      ## install/upgrade WITH ELK
	@test -n "$(PG_PASSWORD)" -a -n "$(MQ_PASSWORD)" -a -n "$(API_KEY)" || (echo "set PG_PASSWORD, MQ_PASSWORD, API_KEY"; exit 1)
	helm upgrade --install $(RELEASE) $(CHART) -n $(NAMESPACE) --create-namespace \
	  -f $(CHART)/values-cluster.yaml --set global.imageTag=$(TAG) --set global.imageRegistry=$(REGISTRY) \
	  --set secrets.postgresPassword=$(PG_PASSWORD) --set secrets.rabbitmqPassword=$(MQ_PASSWORD) \
	  --set secrets.apiKeys=$(API_KEY) --set-string secrets.webhookUrl='$(WEBHOOK_URL)' \
	  --set elk.enabled=true --wait --timeout 15m

elk-on:          ## flip ELK on for a running release (keeps all other values)
	helm upgrade $(RELEASE) $(CHART) -n $(NAMESPACE) --reuse-values --set elk.enabled=true --wait --timeout 15m

elk-off:         ## flip ELK off (PVC with indices is kept)
	helm upgrade $(RELEASE) $(CHART) -n $(NAMESPACE) --reuse-values --set elk.enabled=false

undeploy:        ## remove the release (PVCs are kept)
	helm uninstall $(RELEASE) -n $(NAMESPACE)

pf-kibana:       ## http://localhost:5601
	kubectl -n $(NAMESPACE) port-forward svc/$(RELEASE)-kibana 5601:5601

pf-rabbit:       ## http://localhost:15672 (user: sentinel)
	kubectl -n $(NAMESPACE) port-forward svc/$(RELEASE)-rabbitmq 15672:15672

logs:            ## tail processor logs
	kubectl -n $(NAMESPACE) logs -f deploy/$(RELEASE)-processor

loadtest:        ## URL=http://... API_KEY=... make loadtest
	python scripts/loadtest.py --url $(URL) --api-key $(API_KEY) --duration 30 --concurrency 20 --batch 100

up:              ## local stack with docker compose
	docker compose up --build -d
down:
	docker compose down -v
