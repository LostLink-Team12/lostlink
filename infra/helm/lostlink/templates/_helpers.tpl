{{/* Common labels. Args: dict "root" $ "service" <name> "process" <name-mode> */}}
{{- define "lostlink.labels" -}}
app.kubernetes.io/part-of: lostlink
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{ include "lostlink.selector" . }}
{{- end }}

{{- define "lostlink.selector" -}}
app.kubernetes.io/name: {{ .service }}
app.kubernetes.io/component: {{ .process }}
{{- end }}

{{/* Fully qualified internal HTTPS URL of a service's API. Args: list $ <service> */}}
{{- define "lostlink.serviceUrl" -}}
{{- $root := index . 0 -}}
{{- printf "https://%s-service.%s.svc.%s" (index . 1) $root.Release.Namespace $root.Values.tls.clusterDomain -}}
{{- end }}

{{/* Image reference. Args: list $ <service values> */}}
{{- define "lostlink.image" -}}
{{- $root := index . 0 -}}
{{- printf "%s/%s:%s" $root.Values.image.registry (index . 1).image (required "image.tag must be the published commit SHA" $root.Values.image.tag) -}}
{{- end }}

{{/* Resolve a client reference (owner, operator, machine.<service>) to its Cognito ID. */}}
{{- define "lostlink.clientId" -}}
{{- $root := index . 0 -}}
{{- $ref := index . 1 -}}
{{- if hasPrefix "machine." $ref -}}
{{- required (printf "cognito.machineClients.%s is required" (trimPrefix "machine." $ref)) (get $root.Values.cognito.machineClients (trimPrefix "machine." $ref)) -}}
{{- else -}}
{{- required (printf "cognito.clients.%s is required" $ref) (get $root.Values.cognito.clients $ref) -}}
{{- end -}}
{{- end }}

{{/* IRSA role for a process service account. Args: list $ <process> */}}
{{- define "lostlink.roleArn" -}}
{{- $root := index . 0 -}}
{{- $process := index . 1 -}}
{{- default (printf "%s%s" $root.Values.aws.roleArnPrefix $process) (get $root.Values.aws.roleArns $process) -}}
{{- end }}

{{/*
Container environment for one process.
Args: dict "root" $ "name" <service> "svc" <service values> "mode" <api|worker mode> "p" <process values>
*/}}
{{- define "lostlink.env" -}}
{{- $root := .root -}}
{{- $p := .p -}}
- name: ENVIRONMENT
  value: {{ $root.Values.environment | quote }}
- name: SERVICE_NAME
  value: {{ .svc.image | quote }}
- name: LOG_LEVEL
  value: {{ $root.Values.logLevel | quote }}
- name: AWS_REGION
  value: {{ $root.Values.aws.region | quote }}
- name: AWS_STS_REGIONAL_ENDPOINTS
  value: regional
- name: DB_POOL_SIZE
  value: {{ $root.Values.database.poolSize | quote }}
- name: DB_MAX_OVERFLOW
  value: {{ $root.Values.database.maxOverflow | quote }}
- name: DATABASE_SSLROOTCERT
  value: /etc/lostlink/rds/global-bundle.pem
- name: SSL_CERT_FILE
  value: /etc/lostlink/trust/ca-bundle.pem
- name: XDG_CACHE_HOME
  value: /tmp/.cache
{{- if eq .mode "api" }}
{{- $all := list }}
{{- range $envName := keys (default dict $p.clients) | sortAlpha }}
{{- $ids := list }}
{{- range $ref := get $p.clients $envName }}
{{- $ids = append $ids (include "lostlink.clientId" (list $root $ref)) }}
{{- end }}
{{- $all = concat $all $ids }}
- name: {{ $envName }}
  value: {{ toJson $ids | quote }}
{{- end }}
{{- if $all }}
- name: COGNITO_ISSUER
  value: {{ required "cognito.issuer is required" $root.Values.cognito.issuer | quote }}
- name: COGNITO_CLIENT_IDS
  value: {{ toJson ($all | uniq | sortAlpha) | quote }}
{{- end }}
{{- end }}
{{- range $envName := keys (default dict $p.services) | sortAlpha }}
- name: {{ $envName }}
  value: {{ include "lostlink.serviceUrl" (list $root (get $p.services $envName)) | quote }}
{{- end }}
{{- range $envName := keys (default dict $p.env) | sortAlpha }}
{{- $value := tpl (toString (get $p.env $envName)) $root }}
{{- if $value }}
- name: {{ $envName }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}
{{- if $p.model }}
- name: CLIP_CHECKPOINT
  value: {{ $root.Values.matching.model.path | quote }}
- name: CLIP_SHA256
  value: {{ $root.Values.matching.model.sha256 | quote }}
- name: CLIP_DEVICE
  value: cpu
{{- end }}
{{- if $p.secretEnv }}
{{- $mapping := dict }}
{{- range $envName := keys $p.secretEnv | sortAlpha }}
{{- $spec := get $p.secretEnv $envName }}
{{- $entry := dict "secret" (printf "%s/%s" $root.Values.aws.secretPrefix $spec.secret) }}
{{- if $spec.format }}{{- $_ := set $entry "format" $spec.format }}{{- end }}
{{- if $spec.key }}{{- $_ := set $entry "key" $spec.key }}{{- end }}
{{- $_ := set $mapping $envName $entry }}
{{- end }}
- name: LOSTLINK_SECRET_ENV
  value: {{ toJson $mapping | quote }}
{{- end }}
{{- end }}

{{- define "lostlink.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: {drop: [ALL]}
{{- end }}
