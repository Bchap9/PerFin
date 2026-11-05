{{/* SPDX-License-Identifier: AGPL-3.0-or-later */}}
{{/*
Shared helpers. Names follow the Helm conventions; every PerFin pod carries
app.kubernetes.io/component = api | worker | mcp | migrate | db, which the
NetworkPolicies select on.
*/}}

{{- define "perfin.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "perfin.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "perfin.selectorLabels" -}}
app.kubernetes.io/name: {{ include "perfin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "perfin.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "perfin.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: perfin
{{- end -}}

{{/* Selector labels of one component: (dict "ctx" $ "component" "api") */}}
{{- define "perfin.componentSelector" -}}
{{ include "perfin.selectorLabels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "perfin.componentLabels" -}}
{{ include "perfin.labels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "perfin.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "perfin.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "perfin.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if eq $tag "latest" -}}
{{- fail "image.tag must name a release (or set image.digest), not latest" -}}
{{- end -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "perfin.postgresImage" -}}
{{- $img := .Values.postgresql.image -}}
{{- if $img.digest -}}
{{- printf "%s@%s" $img.repository $img.digest -}}
{{- else -}}
{{- printf "%s:%s" $img.repository $img.tag -}}
{{- end -}}
{{- end -}}

{{- define "perfin.secretName" -}}
{{- required "secrets.existingSecret is required: create a Secret with PERFIN_DATABASE_URL, PERFIN_MIGRATION_DATABASE_URL, PERFIN_MASTER_KEY and PERFIN_SECRET_KEY" .Values.secrets.existingSecret -}}
{{- end -}}

{{/* One env entry from the application Secret: (dict "ctx" $ "key" "X" "optional" false) */}}
{{- define "perfin.secretEnv" -}}
- name: {{ .key }}
  valueFrom:
    secretKeyRef:
      name: {{ include "perfin.secretName" .ctx }}
      key: {{ .key }}
      optional: {{ .optional }}
{{- end -}}

{{/*
Validation of values that would otherwise produce an insecure or broken
deployment. Rendered once from the ConfigMap template.
*/}}
{{- define "perfin.validate" -}}
{{- $_ := required "config.PERFIN_PUBLIC_URL is required (the https URL users reach PerFin at)" .Values.config.PERFIN_PUBLIC_URL -}}
{{- if not (hasPrefix "https://" .Values.config.PERFIN_PUBLIC_URL) -}}
{{- fail "config.PERFIN_PUBLIC_URL must be an https:// URL" -}}
{{- end -}}
{{- range $key, $value := .Values.config -}}
{{- if regexMatch "(PASSWORD|SECRET|_KEY$|DATABASE_URL|_TOKEN$)" $key -}}
{{- fail (printf "config.%s looks like a secret: put it in the Secret named by secrets.existingSecret, not in the ConfigMap" $key) -}}
{{- end -}}
{{- if regexMatch "^PERFIN_(ROLE|MCP_.*)$" $key -}}
{{- fail (printf "config.%s is set by the chart per role" $key) -}}
{{- end -}}
{{- end -}}
{{- $proxies := toString (default "" .Values.config.PERFIN_TRUSTED_PROXIES) -}}
{{- if or (contains "0.0.0.0/0" $proxies) (contains "::/0" $proxies) -}}
{{- fail "config.PERFIN_TRUSTED_PROXIES must not contain 0.0.0.0/0 or ::/0" -}}
{{- end -}}
{{- if and .Values.ingress.enabled (not $proxies) -}}
{{- fail "config.PERFIN_TRUSTED_PROXIES is required with ingress.enabled (the ingress controller's pod CIDR)" -}}
{{- end -}}
{{- if and .Values.ingress.enabled (not .Values.ingress.tlsSecretName) -}}
{{- fail "ingress.tlsSecretName is required: PerFin is served over TLS only" -}}
{{- end -}}
{{- /*
An empty or null peer list in a NetworkPolicy rule matches every source or
destination, so a rule rendered from an empty list would fail open
(P7-SR-R1-01). Every peer list the chart renders must therefore hold at least
one peer, and every peer must be a non-empty map.
*/ -}}
{{- if .Values.networkPolicy.enabled -}}
{{- include "perfin.checkPeers" (dict "name" "networkPolicy.ingressFrom" "peers" .Values.networkPolicy.ingressFrom "why" "your ingress controller; an empty list would admit every pod in the cluster") -}}
{{- include "perfin.checkPeers" (dict "name" "networkPolicy.dns.peers" "peers" .Values.networkPolicy.dns.peers "why" "the cluster DNS; an empty list would open port 53 to every destination") -}}
{{- range $role, $rules := .Values.networkPolicy.egressAllowlist -}}
{{- range $i, $rule := $rules -}}
{{- if not (kindIs "map" $rule) -}}
{{- fail (printf "networkPolicy.egressAllowlist.%s[%d] must be a NetworkPolicy egress rule ({to: [...], ports: [...]})" $role $i) -}}
{{- end -}}
{{- include "perfin.checkPeers" (dict "name" (printf "networkPolicy.egressAllowlist.%s[%d].to" $role $i) "peers" (get $rule "to") "why" "a rule without peers allows every destination") -}}
{{- end -}}
{{- end -}}
{{- if and (not .Values.postgresql.enabled) .Values.externalDatabase.networkPeers -}}
{{- include "perfin.checkPeers" (dict "name" "externalDatabase.networkPeers" "peers" .Values.externalDatabase.networkPeers "why" "where the database is") -}}
{{- end -}}
{{- end -}}
{{- if and .Values.networkPolicy.enabled (not .Values.postgresql.enabled) (not .Values.externalDatabase.networkPeers) -}}
{{- fail "externalDatabase.networkPeers is required when postgresql.enabled is false (where the database is, for the NetworkPolicies)" -}}
{{- end -}}
{{- if .Values.postgresql.enabled -}}
{{- $_ := required "postgresql.existingSecret is required when postgresql.enabled" .Values.postgresql.existingSecret -}}
{{- end -}}
{{- if .Values.mcp.enabled -}}
{{- $_ := required "mcp.existingSecret is required when mcp.enabled (PERFIN_MCP_TOKEN, PERFIN_MCP_HTTP_TOKEN)" .Values.mcp.existingSecret -}}
{{- $_ := required "mcp.allowedOrigins is required when mcp.enabled (explicit https origins)" .Values.mcp.allowedOrigins -}}
{{- end -}}
{{- if and .Values.ingress.routeMcp (not .Values.mcp.enabled) -}}
{{- fail "ingress.routeMcp needs mcp.enabled" -}}
{{- end -}}
{{- if and .Values.dropfolder.enabled (or (not .Values.dropfolder.inboxClaim) (not .Values.dropfolder.processedClaim)) -}}
{{- fail "dropfolder.enabled needs dropfolder.inboxClaim and dropfolder.processedClaim" -}}
{{- end -}}
{{- end -}}

{{/*
Refuses a NetworkPolicy peer list that is empty, null or holds an entry that
is not a non-empty map: (dict "name" "values.path" "peers" $list "why" "...").
*/}}
{{- define "perfin.checkPeers" -}}
{{- if or (not .peers) (not (kindIs "slice" .peers)) -}}
{{- fail (printf "%s must list at least one NetworkPolicy peer (%s)" .name .why) -}}
{{- end -}}
{{- range $i, $peer := .peers -}}
{{- if or (not (kindIs "map" $peer)) (not $peer) -}}
{{- fail (printf "%s[%d] must be a NetworkPolicy peer (podSelector, namespaceSelector or ipBlock), not empty" $.name $i) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Pod-level settings shared by every PerFin pod. */}}
{{- define "perfin.podSecurity" -}}
automountServiceAccountToken: false
enableServiceLinks: false
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "perfin.claimName" -}}
{{- if .vol.existingClaim -}}
{{- .vol.existingClaim -}}
{{- else -}}
{{- printf "%s-%s" (include "perfin.fullname" .ctx) .name -}}
{{- end -}}
{{- end -}}

{{/* The volumes and mounts api and worker share (documents, quarantine). */}}
{{- define "perfin.dataVolumes" -}}
- name: documents
  persistentVolumeClaim:
    claimName: {{ include "perfin.claimName" (dict "ctx" . "vol" .Values.persistence.documents "name" "documents") }}
- name: quarantine
  persistentVolumeClaim:
    claimName: {{ include "perfin.claimName" (dict "ctx" . "vol" .Values.persistence.quarantine "name" "quarantine") }}
{{- end -}}

{{- define "perfin.dataMounts" -}}
- name: documents
  mountPath: /var/lib/perfin/documents
- name: quarantine
  mountPath: /var/lib/perfin/quarantine
{{- end -}}

{{/* NetworkPolicy egress rule to the database. */}}
{{- define "perfin.dbEgress" -}}
- to:
{{- if .Values.postgresql.enabled }}
    - podSelector:
        matchLabels:
          {{- include "perfin.componentSelector" (dict "ctx" . "component" "db") | nindent 10 }}
  ports:
    - protocol: TCP
      port: 5432
{{- else }}
    {{- toYaml .Values.externalDatabase.networkPeers | nindent 4 }}
  ports:
    - protocol: TCP
      port: {{ .Values.externalDatabase.port }}
{{- end }}
{{- end -}}

{{- define "perfin.dnsEgress" -}}
- to:
    {{- toYaml .Values.networkPolicy.dns.peers | nindent 4 }}
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- end -}}
