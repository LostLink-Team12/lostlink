{{- define "lostlink.resources" -}}
{{- $root := . -}}
{{- range $mode, $process := .Values.processes }}
{{- $name := printf "%s-%s" $root.Values.service $mode }}
{{- if $process.model }}
{{- range $kind := list "requests" "limits" }}
{{- $memory := toString (get (get $process.resources $kind) "memory") }}
{{- if not (regexMatch "^[0-9]+(Mi|Gi)$" $memory) }}
{{- fail (printf "%s memory %s must use integer Mi or Gi" $name $kind) }}
{{- end }}
{{- $amount := int (regexFind "^[0-9]+" $memory) }}
{{- if hasSuffix "Gi" $memory }}{{- $amount = mul $amount 1024 }}{{- end }}
{{- if lt (int $amount) 4096 }}
{{- fail (printf "%s memory %s must be at least 4Gi for the pinned CLIP model" $name $kind) }}
{{- end }}
{{- end }}
{{- end }}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ $name }}
  annotations:
    eks.amazonaws.com/role-arn: {{ required (printf "%s IRSA role required" $name) $process.roleArn | quote }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}
  labels: {app.kubernetes.io/part-of: lostlink, app.kubernetes.io/name: {{ $name }}}
spec:
  {{- if and (ne $mode "api") (not $process.keda) }}
  replicas: {{ dig "replicas" 1 $process }}
  {{- end }}
  revisionHistoryLimit: 2
  strategy:
    type: RollingUpdate
    rollingUpdate: {maxSurge: 0, maxUnavailable: 1}
  selector:
    matchLabels: {app.kubernetes.io/name: {{ $name }}}
  template:
    metadata:
      labels: {app.kubernetes.io/part-of: lostlink, app.kubernetes.io/name: {{ $name }}}
      annotations:
        lostlink/runtime-revision: {{ $root.Values.runtimeRevision | quote }}
        {{- if eq $mode "api" }}
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        {{- end }}
    spec:
      serviceAccountName: {{ $name }}
      terminationGracePeriodSeconds: 120
      securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: app
          image: {{ required "immutable service image required" $root.Values.image | quote }}
          imagePullPolicy: IfNotPresent
          command: [python, /deploy/runtime.py]
          args: {{ $process.command | toJson }}
          env:
            - {name: PYTHONDONTWRITEBYTECODE, value: "1"}
            - {name: PYTHONUNBUFFERED, value: "1"}
            - {name: SSL_CERT_FILE, value: /trust/combined.pem}
            - {name: OMP_NUM_THREADS, value: "1"}
            - {name: MKL_NUM_THREADS, value: "1"}
            {{- if ne $mode "api" }}
            - {name: WORKER_HEARTBEAT, value: "true"}
            {{- end }}
            {{- range $key, $value := merge (deepCopy (default dict $process.env)) (deepCopy $root.Values.env) }}
            - name: {{ $key }}
              value: {{ $value | quote }}
            {{- end }}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          resources: {{ $process.resources | toJson }}
          {{- if eq $mode "api" }}
          ports: [{name: http, containerPort: 8000}]
          startupProbe: {httpGet: {path: /ready, port: http}, periodSeconds: 10, failureThreshold: 90}
          readinessProbe: {httpGet: {path: /ready, port: http}, periodSeconds: 15, timeoutSeconds: 5}
          livenessProbe: {httpGet: {path: /health, port: http}, periodSeconds: 20, timeoutSeconds: 5}
          {{- else }}
          startupProbe:
            exec: {command: [python, /deploy/worker-probe.py]}
            periodSeconds: 10
            failureThreshold: 90
          readinessProbe:
            exec: {command: [python, /deploy/worker-probe.py]}
            periodSeconds: 30
          livenessProbe:
            exec: {command: [python, /deploy/worker-probe.py]}
            periodSeconds: 30
            failureThreshold: 4
          {{- end }}
          volumeMounts:
            - {name: runtime, mountPath: /deploy, readOnly: true}
            - {name: trust, mountPath: /trust, readOnly: true}
            - {name: tmp, mountPath: /tmp}
            {{- if $process.model }}
            - {name: model, mountPath: /models, readOnly: true}
            {{- end }}
        {{- if eq $mode "api" }}
        - name: tls
          image: {{ $root.Values.proxyImage | quote }}
          ports: [{name: https, containerPort: 8443}]
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          resources: {requests: {cpu: 10m, memory: 24Mi}, limits: {cpu: 100m, memory: 64Mi}}
          readinessProbe: {httpGet: {path: /health, port: https, scheme: HTTPS}, periodSeconds: 15}
          volumeMounts:
            - {name: proxy, mountPath: /etc/nginx/conf.d, readOnly: true}
            - {name: tls, mountPath: /tls, readOnly: true}
            - {name: nginx-tmp, mountPath: /tmp}
        {{- end }}
      {{- if $process.model }}
      initContainers:
        - name: model
          image: {{ $root.Values.image | quote }}
          command: [python, /deploy/prepare_clip_model.py, --destination, /models/ViT-B-32.pt]
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 500m, memory: 256Mi}}
          volumeMounts:
            - {name: runtime, mountPath: /deploy, readOnly: true}
            - {name: model, mountPath: /models}
            - {name: tmp, mountPath: /tmp}
      {{- end }}
      volumes:
        - {name: runtime, configMap: {name: lostlink-runtime}}
        - {name: trust, configMap: {name: lostlink-trust}}
        - {name: tmp, emptyDir: {sizeLimit: 128Mi}}
        {{- if $process.model }}
        - {name: model, emptyDir: {sizeLimit: 512Mi}}
        {{- end }}
        {{- if eq $mode "api" }}
        - {name: tls, secret: {secretName: lostlink-internal-tls}}
        - {name: proxy, configMap: {name: lostlink-api-tls}}
        - {name: nginx-tmp, emptyDir: {sizeLimit: 32Mi}}
        {{- end }}
{{- if eq $mode "api" }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $name }}
spec:
  selector: {app.kubernetes.io/name: {{ $name }}}
  ports:
    - {name: https, port: 443, targetPort: https}
    - {name: metrics, port: 8000, targetPort: http}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ $name }}
  labels: {app.kubernetes.io/part-of: lostlink, app.kubernetes.io/name: {{ $name }}}
spec:
  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: {{ $name }}}
  minReplicas: 1
  maxReplicas: {{ $root.Values.hpa.maxReplicas }}
  behavior:
    scaleDown: {stabilizationWindowSeconds: 180}
  metrics:
    - type: ContainerResource
      containerResource:
        name: cpu
        container: app
        target: {type: Utilization, averageUtilization: 60}
{{- end }}
{{- if $process.keda }}
---
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: {{ $name }}
  labels: {app.kubernetes.io/part-of: lostlink, app.kubernetes.io/name: {{ $name }}}
spec:
  scaleTargetRef: {name: {{ $name }}}
  pollingInterval: 15
  cooldownPeriod: 180
  minReplicaCount: 1
  maxReplicaCount: {{ $root.Values.keda.maxReplicas }}
  fallback: {failureThreshold: 3, replicas: 1}
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleDown: {stabilizationWindowSeconds: 180}
  triggers:
    - type: postgresql
      metadata:
        query: "SELECT count(*) FROM matching.work_jobs WHERE state = 'pending' AND kind = 'embed' AND available_at <= now()"
        targetQueryValue: "10"
        activationTargetQueryValue: "0"
      authenticationRef: {name: lostlink-backlog}
{{- end }}
{{- end }}
{{- if .Values.migration.enabled }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .Values.service }}-migration
  annotations:
    helm.sh/hook: pre-install,pre-upgrade
    helm.sh/hook-weight: "-5"
    helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 600
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: migrate
          image: {{ .Values.image | quote }}
          command: [alembic, upgrade, head]
          envFrom:
            - secretRef:
                name: {{ required "operator-provisioned schema-specific migration secret required" .Values.migration.existingSecret }}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {cpu: 500m, memory: 512Mi}}
          volumeMounts:
            - {name: runtime, mountPath: /deploy, readOnly: true}
            - {name: trust, mountPath: /trust, readOnly: true}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - {name: runtime, configMap: {name: lostlink-runtime}}
        - {name: trust, configMap: {name: lostlink-trust}}
        - {name: tmp, emptyDir: {sizeLimit: 64Mi}}
{{- end }}
{{- end }}
