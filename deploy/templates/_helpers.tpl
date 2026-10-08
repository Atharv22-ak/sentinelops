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

{{- define "sentinelops.image" -}}
{{ .root.Values.global.imageRegistry }}/sentinelops-{{ .name }}:{{ .root.Values.global.imageTag }}
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
{{- $as := default dict $c.autoscaling -}}
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
  {{- if not $as.enabled }}
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
        checksum/config: {{ include (print $root.Template.BasePath "/configmap.yaml") $root | sha256sum }}
        checksum/secret: {{ include (print $root.Template.BasePath "/secret.yaml") $root | sha256sum }}
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: /metrics
    spec:
      automountServiceAccountToken: false
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
          image: {{ include "sentinelops.image" (dict "root" $root "name" .image) }}
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
          readinessProbe:
            httpGet: {path: /readyz, port: http}
            initialDelaySeconds: 5
            periodSeconds: 10
            failureThreshold: 6
          livenessProbe:
            httpGet: {path: /healthz, port: http}
            initialDelaySeconds: 20
            periodSeconds: 15
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
