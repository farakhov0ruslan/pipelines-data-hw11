#!/usr/bin/env bash
# Обновляет newTag в Kustomize overlay для указанного сервиса.
# Аргументы: <service-name> <overlay> <new-tag>
set -euo pipefail

SERVICE="${1:?service name required}"
OVERLAY="${2:?overlay required (dev|staging|prod)}"
NEW_TAG="${3:?new tag required}"

FILE="services/${SERVICE}/overlays/${OVERLAY}/kustomization.yaml"

yq e -i "
  .images[] |= select(.name == \"${SERVICE}\").newTag = \"${NEW_TAG}\"
" "${FILE}"

echo "[update-image-tag] ${FILE}: ${SERVICE} → ${NEW_TAG}"
