#!/usr/bin/env bash
# Генерирует Workflow CRD из шаблона, подставляя IMAGE_TAG и SPARK_IMAGE.
# Запускается из директории data-platform-infra в GitLab CI.
set -euo pipefail

: "${IMAGE_TAG:?IMAGE_TAG is required}"
: "${SPARK_IMAGE:?SPARK_IMAGE is required}"

WORKFLOW_NAME="spark-course-checker-${IMAGE_TAG}"
TEMPLATE="workflows/spark-job-workflow-template.yaml"
OUTPUT="workflows/submitted/${WORKFLOW_NAME}.yaml"

yq e "
  .metadata.name = \"${WORKFLOW_NAME}\" |
  .spec.arguments.parameters[0].value = \"${IMAGE_TAG}\" |
  .spec.arguments.parameters[1].value = \"${SPARK_IMAGE}\"
" "${TEMPLATE}" > "${OUTPUT}"

echo "[generate-spark-workflow] created: ${OUTPUT}"
