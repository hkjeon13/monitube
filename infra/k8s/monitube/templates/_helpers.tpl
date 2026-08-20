{{/*
Component resource name, e.g. monitube-web. Matches the names already running
in monitube-prod so an upgrade adopts them instead of creating duplicates.
*/}}
{{- define "monitube.fullname" -}}
{{- printf "%s-%s" .root.Release.Name .component -}}
{{- end -}}

{{- define "monitube.labels" -}}
app.kubernetes.io/name: {{ include "monitube.fullname" . }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end -}}

{{- define "monitube.selectorLabels" -}}
app.kubernetes.io/name: {{ include "monitube.fullname" . }}
{{- end -}}

{{- define "monitube.imagePullSecrets" -}}
{{- with .Values.pullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ .name }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
DATABASE_URL as an explicit env entry. Kubernetes gives an explicit `env` key
precedence over the same key arriving through `envFrom`, so switching
database.useCnpg overrides the value baked into the runtime secret without
anyone having to edit that secret by hand.
*/}}
{{- define "monitube.databaseUrlEnv" -}}
{{- if .Values.database.useCnpg }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.cnpg.name }}-app
      key: uri
{{- end }}
{{- end -}}
