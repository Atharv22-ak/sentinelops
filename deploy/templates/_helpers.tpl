{{- define "sentinelops.fullname" -}}
{{- if contains "sentinelops" .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-sentinelops" .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "sentinelops.labels" -}}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
app.kubernetes.io/name: sentinelops
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end -}}

{{- define "sentinelops.selectorLabels" -}}
app.kubernetes.io/name: sentinelops
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
  Image reference for one service. Each service pins its own tag (<service>.image.tag); global.imageTag is only an
  optional fallback. Tags must be immutable (Git SHA / release version) - mutable ones are rejected.
  Single-repo mode (global.imageRepository=sentinelops): registry/sentinelops:<service>-<tag>, e.g. sentinelops:processor-9f2c1ab04d11
  Otherwise one repo per service: registry/sentinelops-<service>:<tag>.
  Call with: dict "root" . "name" <image name> "key" <values key, e.g. ingestApi> "cfg" <service values>
*/}}
{{- define "sentinelops.image" -}}
{{- $img := default dict .cfg.image -}}
{{- $tag := default .root.Values.global.imageTag $img.tag -}}
{{- if not $tag -}}
{{- fail (printf "%s.image.tag is required (or set global.imageTag as a fallback) - use an immutable tag such as the Git SHA" .key) -}}
{{- end -}}
{{- if has $tag (list "latest" "v1" "dev" "stable") -}}
{{- fail (printf "%s.image.tag=%q is a mutable tag; use the Git SHA or a release version" .key $tag) -}}
{{- end -}}
{{- $repo := .root.Values.global.imageRepository -}}
{{- if $repo -}}
{{ .root.Values.global.imageRegistry }}/{{ $repo }}:{{ .name }}-{{ $tag }}
{{- else -}}
{{ .root.Values.global.imageRegistry }}/sentinelops-{{ .name }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{/* imagePullSecrets block (empty when none configured). */}}
{{- define "sentinelops.imagePullSecrets" -}}
{{- $secrets := .Values.global.imagePullSecrets -}}
{{- if and (not $secrets) .Values.ecr.refresh.enabled -}}
{{- $secrets = list (dict "name" .Values.ecr.refresh.secretName) -}}
{{- end -}}
{{- with $secrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* ServiceAccount name for a component; falls back to "default" when serviceAccount.create=false. */}}
{{- define "sentinelops.serviceAccountName" -}}
{{- if .root.Values.serviceAccount.create -}}
{{ include "sentinelops.fullname" .root }}-{{ .component }}
{{- else -}}
default
{{- end -}}
{{- end -}}

{{/* Is this workload's replica count owned by an autoscaler (HPA or KEDA)? */}}
{{- define "sentinelops.autoscaled" -}}
{{- $as := default dict .cfg.autoscaling -}}
{{- if or $as.enabled (and (eq .component "processor") .root.Values.keda.enabled) -}}true{{- end -}}
{{- end -}}

{{- define "sentinelops.host" -}}
{{ printf "%s.%s" .sub .root.Values.gateway.baseDomain }}
{{- end -}}

{{- define "sentinelops.secretName" -}}
{{- default (printf "%s-secrets" (include "sentinelops.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "sentinelops.storageClass" -}}
{{- if .Values.global.storageClass }}
storageClassName: {{ .Values.global.storageClass | quote }}
{{- end }}
{{- end -}}

{{/* Deployment + Service for one Python microservice. */}}
{{- define "sentinelops.workload" -}}
{{- $root := .root -}}
{{- $c := .cfg -}}
{{- $scaled := include "sentinelops.autoscaled" (dict "root" .root "cfg" .cfg "component" .component) -}}
{{- $full := include "sentinelops.fullname" $root -}}
{{- $name := printf "%s-%s" $full .component -}}
{{- $ctx := dict "root" $root "component" .component -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}
  labels:
    {{- include "sentinelops.labels" $ctx | nindent 4 }}
spec:
  {{- if not $scaled }}
  replicas: {{ $c.replicas }}
  {{- end }}
  revisionHistoryLimit: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  selector:
    matchLabels:
      {{- include "sentinelops.selectorLabels" $ctx | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "sentinelops.labels" $ctx | nindent 8 }}
      annotations:
        checksum/config: {{ include (print $root.Template.BasePath "/configmaps/app-config.yaml") $root | sha256sum }}
        checksum/secret: {{ include (print $root.Template.BasePath "/secrets/secret.yaml") $root | sha256sum }}
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: /metrics
    spec:
      serviceAccountName: {{ include "sentinelops.serviceAccountName" (dict "root" $root "component" .component) }}
      automountServiceAccountToken: false
      {{- include "sentinelops.imagePullSecrets" $root | nindent 6 }}
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        seccompProfile:
          type: RuntimeDefault
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              {{- include "sentinelops.selectorLabels" $ctx | nindent 14 }}
      containers:
        - name: app
          image: {{ include "sentinelops.image" (dict "root" $root "name" .image "key" .key "cfg" $c) }}
          imagePullPolicy: {{ $root.Values.global.imagePullPolicy }}
          ports:
            - name: http
              containerPort: 8000
          envFrom:
            - configMapRef:
                name: {{ $full }}-config
            - secretRef:
                name: {{ include "sentinelops.secretName" $root }}
          env:
            - name: SERVICE_NAME
              value: {{ .image | quote }}
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            {{- range $k, $v := $c.env }}
            - name: {{ $k }}
              value: {{ $v | quote }}
            {{- end }}
          # startupProbe gives slow dependency connects (apps retry RabbitMQ/Postgres for ~2 min) room without
          # making liveness aggressive. Readiness reflects real dependency state (/readyz checks Redis/RabbitMQ/Postgres).
          startupProbe:
            httpGet: {path: /healthz, port: http}
            periodSeconds: 5
            failureThreshold: 36
          readinessProbe:
            httpGet: {path: /readyz, port: http}
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 6
          livenessProbe:
            httpGet: {path: /healthz, port: http}
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 6
          resources:
            {{- toYaml $c.resources | nindent 12 }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $name }}
  labels:
    {{- include "sentinelops.labels" $ctx | nindent 4 }}
spec:
  selector:
    {{- include "sentinelops.selectorLabels" $ctx | nindent 4 }}
  ports:
    - name: http
      port: 8000
      targetPort: http
{{- end -}}
