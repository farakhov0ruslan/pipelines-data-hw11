# Отчёт: Архитектура CI/CD пайплайна для развёртывания разнородных приложений в Kubernetes

**ФИО:** Фарахов Руслан Ильнурович   
**Курс:** Построение пайплайнов данных

## Введение

Задание требует спроектировать репозиторную структуру и описать полный CI/CD пайплайн для развёртывания в Kubernetes трёх классов приложений:

- **Spark-приложения** — пакетная обработка данных, запускаются через Argo Workflows и Spark Operator;
- **Сторонние продукты через Helm** — Argo Workflows, Spark Operator, MinIO, PostgreSQL;
- **Внутренние витрины данных** — React/Node.js сервисы с собственными Dockerfile.

Архитектурное решение строится на принципе GitOps из лекции: "CI собирает артефакт, а GitOps следит, чтобы среда реально пришла к состоянию из репозитория." Ключевая связь с предыдущим домашним заданием (Д3): переиспользуемые `WorkflowTemplate`, созданные в `hw11_example/templates/`, становятся фундаментом CI/CD для Spark-джобов.

Архитектурная диаграмма (`diagram.puml`) приведена ниже в формате Mermaid:

```mermaid
flowchart TD
    Dev["👤 Разработчик"]

    subgraph SRC["📦 data-platform — исходный код + CI"]
        direction TB
        SparkSrc["spark-jobs/course-checker/\nDockerfile · main.py"]
        SvcSrc["services/\ndashboard-api · dashboard-ui"]
        CI[".gitlab-ci.yml\nbuild → publish → update-infra"]
    end

    subgraph HARBOR["🗄️ Harbor Registry (OCI)"]
        SparkImg["spark-course-checker:SHA"]
        SvcImg["dashboard-api:SHA\ndashboard-ui:SHA"]
    end

    subgraph INFRA["⚙️ data-platform-infra — GitOps конфиг"]
        direction TB
        HelmVals["helm-releases/\nargo-wf · minio · spark-op · postgresql"]
        KustOverlays["services/\nbase + overlays/dev|staging|prod"]
        WFTemplates["workflow-templates/\n01-download · 02-validate\n03-spark-job · 04-report"]
        SubmittedWF["workflows/submitted/\nspark-checker-SHA.yaml\n⬅ генерирует GitLab CI"]
    end

    subgraph K8S["☸️ Kubernetes Cluster"]
        ArgoCD["Argo CD\nGitOps controller"]

        subgraph WF["Argo Workflows"]
            WFRun["Workflow: spark-checker-SHA"]
            T1["① download-dataset\ntemplateRef: download-dataset-template"]
            T2["② validate-schema\ntemplateRef: validate-schema-template"]
            T3["③ run-spark-job\ntemplateRef: spark-job-template"]
            T4["④ generate-report\ntemplateRef: quality-report-template"]
        end

        SparkOp["Spark Operator\n(Helm release)"]
        MinIO["MinIO\n(Helm release)"]
        PG["PostgreSQL\n(Helm release)"]
        DashAPI["dashboard-api\n(Kustomize overlays/prod)"]
        DashUI["dashboard-ui\n(Kustomize overlays/prod)"]
    end

    Dev -->|"git push spark-jobs/**"| CI
    CI -->|"docker push :SHA\nстадия: publish"| SparkImg
    CI -->|"docker push :SHA\nстадия: publish"| SvcImg
    CI -->|"git commit spark-checker-SHA.yaml\nyq envsubst\nстадия: update-infra"| SubmittedWF

    INFRA -->|"poll 3 min / webhook"| ArgoCD
    ArgoCD -->|"kubectl apply Workflow CRD"| WFRun
    ArgoCD -->|"helm upgrade"| MinIO
    ArgoCD -->|"helm upgrade"| PG
    ArgoCD -->|"helm upgrade"| SparkOp
    ArgoCD -->|"kubectl apply -k overlays/prod"| DashAPI
    ArgoCD -->|"kubectl apply -k overlays/prod"| DashUI

    WFRun --> T1
    T1 -->|"artifact: dataset"| T2
    T2 -->|"artifact + validation-status"| T3
    T3 -->|"artifact + spark-status"| T4
    T3 -->|"создаёт SparkApplication CRD"| SparkOp
    SparkOp -.->|"pulls image :SHA"| SparkImg

    WFTemplates -.->|"templateRef"| T1
    WFTemplates -.->|"templateRef"| T2
    WFTemplates -.->|"templateRef"| T3
    WFTemplates -.->|"templateRef"| T4
```

---

## Часть 1. Технологический стек

### 1.1 Выбор инструментов сборки и хранения артефактов

**CI-система: GitLab CI**

Выбор пал на GitLab CI по следующим причинам: pipeline-конфигурация (`.gitlab-ci.yml`) лежит рядом с кодом в том же репозитории — это удобно для ревью; GitLab предоставляет встроенный container registry и поддерживает динамические переменные окружения (`$CI_COMMIT_SHORT_SHA`, `$CI_PROJECT_NAME`), которые нужны для тегирования образов. По сравнению с Jenkins — меньше инфраструктурного накладного расхода (нет master/agent топологии, нет Groovy DSL). По сравнению с Argo Workflows в роли CI — Argo WF оптимален для оркестрации data-пайплайнов, но не для сборки образов: у него нет нативной интеграции с Git-событиями (push/MR).

**Реестр артефактов: Harbor**

Harbor выбран вместо jFrog Artifactory и Nexus. Ключевые аргументы:

| Критерий | Harbor | Nexus Repository | jFrog Artifactory |
|---|---|---|---|
| Лицензия | Open-source (Apache 2.0) | Core — open-source, Pro — платный | Платный (OSS-редакция ограничена) |
| Docker images | Да (OCI-совместим) | Да | Да |
| Helm charts (OCI) | Да, нативно | Через plugin | Да |
| Vulnerability scanning | Встроен (Trivy) | Нет (только в платной) | Нет в OSS |
| RBAC | Проектная модель | Базовый | Расширенный |
| Сложность установки | Helm chart, 1 команда | Сложнее | Сложнее |

Harbor хранит в одном реестре и Docker-образы, и Helm-чарты в OCI-формате — это устраняет необходимость держать два отдельных хранилища.

### 1.2 Сводная таблица стека

| Задача | Инструмент | Альтернативы | Обоснование |
|---|---|---|---|
| Сборка Docker-образов | GitLab CI | Jenkins, Argo WF | нативная интеграция с GitLab, pipeline рядом с кодом |
| Хранение образов и чартов | Harbor | Nexus, jFrog | open-source, OCI, Docker + Helm в одном реестре, Trivy |
| GitOps CD-контроллер | Argo CD | Flux CD | UI, ApplicationSet, показан на слайде лекции |
| Оркестрация data-джобов | Argo Workflows + Spark Operator | Airflow, Prefect | уже используется в Д3, `templateRef` переиспользование |
| Деплой стороннего ПО | Helm | чистый kubectl, Kustomize | rollback, versioning, параметризация |
| Деплой внутренних сервисов | Kustomize | Helm | GitOps-основа, патчи без Go-шаблонов |

**Разграничение Helm и Kustomize** соответствует слайдам лекции. Helm выбирается, когда: устанавливается стороннее ПО, нужен rollback, требуется сложная параметризация. Kustomize — когда: внутренние микросервисы, GitOps как основа, нужно модифицировать чужой манифест.

---

## Часть 2. Структура репозиториев

Проект использует **два репозитория** (GitOps-split). Причина разделения: `data-platform` меняется при каждом коммите кода — feature-ветки, правки фронтенда, рефакторинг. Если конфигурация K8s лежала бы в том же репозитории, Argo CD синхронизировал бы кластер при каждом пуше React-компонента, не связанного с деплоем. Infra-репозиторий — единственный источник истины для желаемого состояния кластера (GitOps-принцип: "разработчик меняет конфигурацию в Git → review и merge → контроллер читает репозиторий").

### 2.1 Репозиторий `data-platform` — исходный код + CI

```
data-platform/
├── .gitlab-ci.yml                         # главный CI-пайплайн (стадии: build, publish, update-infra)
│
├── spark-jobs/
│   ├── course-checker/                    # Spark-приложение проверки курсовых
│   │   ├── Dockerfile                     # FROM python:3.11-slim + pyspark
│   │   ├── main.py                        # точка входа Spark-джоба
│   │   ├── requirements.txt
│   │   └── tests/
│   └── data-quality-job/                  # второй Spark-джоб (проверка качества данных)
│       ├── Dockerfile
│       └── job.py
│
├── services/
│   ├── dashboard-api/                     # Node.js backend витрины
│   │   ├── Dockerfile
│   │   ├── src/
│   │   └── package.json
│   └── dashboard-ui/                      # React frontend витрины
│       ├── Dockerfile
│       ├── src/
│       └── package.json
│
└── ci/
    └── scripts/
        ├── generate-spark-workflow.sh     # генерирует Workflow CRD через yq
        └── update-image-tag.sh            # обновляет newTag в kustomization.yaml infra-репо
```

**Что здесь живёт:** только исходный код приложений и Dockerfile. Никаких K8s-манифестов. GitLab CI читает `.gitlab-ci.yml` и при изменении Spark-кода запускает цепочку build → publish → update-infra.

### 2.2 Репозиторий `data-platform-infra` — GitOps-конфиг

```
data-platform-infra/
│
├── argocd/
│   └── applications/
│       ├── argo-workflows-app.yaml        # Argo CD Application: Helm release Argo WF
│       ├── spark-operator-app.yaml        # Argo CD Application: Helm release Spark Operator
│       ├── minio-app.yaml                 # Argo CD Application: Helm release MinIO
│       ├── postgresql-app.yaml            # Argo CD Application: Helm release PostgreSQL
│       ├── dashboard-api-app.yaml         # Argo CD Application: Kustomize dashboard-api
│       ├── dashboard-ui-app.yaml          # Argo CD Application: Kustomize dashboard-ui
│       ├── workflow-templates-app.yaml    # Argo CD Application: WorkflowTemplate CRDs
│       └── submitted-workflows-app.yaml   # Argo CD Application: сгенерированные Workflow
│
├── helm-releases/                         # values-файлы для стороннего ПО
│   ├── argo-workflows/
│   │   ├── Chart.yaml                     # dependency wrapper: chart из argoproj/argo-helm
│   │   ├── values.yaml                    # общие настройки: executor, minio endpoint
│   │   └── values-prod.yaml              # prod: resource limits, replicas
│   ├── spark-operator/
│   │   └── values.yaml
│   ├── minio/
│   │   ├── values.yaml
│   │   └── values-prod.yaml
│   └── postgresql/
│       ├── values.yaml
│       └── values-prod.yaml
│
├── services/                              # Kustomize для внутренних сервисов
│   ├── dashboard-api/
│   │   ├── base/
│   │   │   ├── deployment.yaml            # базовый манифест без env-специфики
│   │   │   ├── service.yaml
│   │   │   └── kustomization.yaml
│   │   └── overlays/
│   │       ├── dev/
│   │       │   └── kustomization.yaml     # replicas: 1, tag: latest
│   │       ├── staging/
│   │       │   └── kustomization.yaml     # replicas: 2, tag: <SHA>
│   │       └── prod/
│   │           └── kustomization.yaml     # replicas: 3, resource limits, tag: <SHA>
│   └── dashboard-ui/
│       ├── base/
│       └── overlays/
│           ├── dev/
│           ├── staging/
│           └── prod/
│
├── workflow-templates/                    # переиспользуемые WorkflowTemplate (из Д3)
│   ├── 01-download-artifact-template.yaml
│   ├── 02-unpack-archive-template.yaml
│   ├── 03-project-check-template.yaml
│   └── 04-report-template.yaml
│
└── workflows/
    ├── spark-job-workflow-template.yaml   # шаблон для генерации (не CRD kind:Workflow, а заготовка)
    └── submitted/                         # CRD kind:Workflow — пишет GitLab CI
        └── spark-course-checker-abc1234.yaml  # пример сгенерированного файла
```

### 2.3 Расположение ключевых артефактов

| Артефакт задания | Расположение в репозитории |
|---|---|
| Helm charts для стороннего ПО | `data-platform-infra/helm-releases/` |
| YAML/Helm charts внутренних сервисов | `data-platform-infra/services/*/base/` |
| Kustomize overlays для сред | `data-platform-infra/services/*/overlays/dev|staging|prod/` |
| Переиспользуемые Argo WF шаблоны (Д3) | `data-platform-infra/workflow-templates/` |
| Исходный код Spark-приложений | `data-platform/spark-jobs/` |
| Исходный код React/Node.js сервисов | `data-platform/services/` |
| Argo Workflow файлы для запусков | `data-platform-infra/workflows/submitted/` |

---

## Часть 3. Пайплайн от git push до запуска в кластере

### 3.1 Структура `.gitlab-ci.yml`

GitLab CI определяет три стадии для каждого Spark-приложения:

```yaml
stages:
  - lint
  - build
  - publish
  - update-infra

variables:
  HARBOR_REGISTRY: harbor.internal.example.com
  SPARK_IMAGE_NAME: data-platform/spark-course-checker
  IMAGE_TAG: $CI_COMMIT_SHORT_SHA

build-spark-image:
  stage: build
  image: docker:24
  services:
    - docker:24-dind
  script:
    - docker build
        -t $HARBOR_REGISTRY/$SPARK_IMAGE_NAME:$IMAGE_TAG
        spark-jobs/course-checker/
  rules:
    - changes:
        - spark-jobs/course-checker/**

publish-spark-image:
  stage: publish
  image: docker:24
  services:
    - docker:24-dind
  script:
    - docker login -u $HARBOR_USER -p $HARBOR_PASSWORD $HARBOR_REGISTRY
    - docker push $HARBOR_REGISTRY/$SPARK_IMAGE_NAME:$IMAGE_TAG
    - docker tag  $HARBOR_REGISTRY/$SPARK_IMAGE_NAME:$IMAGE_TAG
                  $HARBOR_REGISTRY/$SPARK_IMAGE_NAME:latest
    - docker push $HARBOR_REGISTRY/$SPARK_IMAGE_NAME:latest
  rules:
    - changes:
        - spark-jobs/course-checker/**

generate-and-commit-workflow:
  stage: update-infra
  image: alpine:3.19
  script:
    - apk add --no-cache git yq
    - git clone https://gitlab-ci-token:${INFRA_REPO_TOKEN}@gitlab.internal.example.com/data-platform-infra.git
    - cd data-platform-infra
    - ci/scripts/generate-spark-workflow.sh
    - git config user.email "ci@gitlab.internal.example.com"
    - git config user.name "GitLab CI"
    - git add workflows/submitted/
    - git commit -m "ci: submit spark-course-checker-${IMAGE_TAG} [skip ci]"
    - git push
  rules:
    - changes:
        - spark-jobs/course-checker/**

build-publish-dashboard-api:
  stage: publish
  image: docker:24
  services:
    - docker:24-dind
  script:
    - docker login -u $HARBOR_USER -p $HARBOR_PASSWORD $HARBOR_REGISTRY
    - docker build -t $HARBOR_REGISTRY/data-platform/dashboard-api:$IMAGE_TAG services/dashboard-api/
    - docker push $HARBOR_REGISTRY/data-platform/dashboard-api:$IMAGE_TAG
  rules:
    - changes:
        - services/dashboard-api/**

update-dashboard-api-tag:
  stage: update-infra
  image: alpine:3.19
  script:
    - apk add --no-cache git yq
    - git clone https://gitlab-ci-token:${INFRA_REPO_TOKEN}@gitlab.internal.example.com/data-platform-infra.git
    - cd data-platform-infra
    - yq e -i '
        .images[] |= select(.name == "dashboard-api").newTag = env(IMAGE_TAG)
      ' services/dashboard-api/overlays/prod/kustomization.yaml
    - git add services/dashboard-api/overlays/prod/kustomization.yaml
    - git commit -m "ci: update dashboard-api tag to ${IMAGE_TAG} [skip ci]"
    - git push
  rules:
    - changes:
        - services/dashboard-api/**
```

**Аннотация:** флаг `[skip ci]` в сообщении коммита предотвращает рекурсивный триггер CI при коммите в infra-репо. `INFRA_REPO_TOKEN` — GitLab CI/CD variable с project access token infra-репозитория.

### 3.2 Генерация Workflow YAML

Скрипт `ci/scripts/generate-spark-workflow.sh` принимает шаблон из `workflows/spark-job-workflow-template.yaml` и через `yq` подставляет актуальные значения:

```bash
#!/usr/bin/env bash
# Запускается внутри GitLab CI в директории data-platform-infra
set -euo pipefail

WORKFLOW_NAME="spark-course-checker-${IMAGE_TAG}"
TEMPLATE_FILE="workflows/spark-job-workflow-template.yaml"
OUTPUT_FILE="workflows/submitted/${WORKFLOW_NAME}.yaml"

yq e "
  .metadata.name = \"${WORKFLOW_NAME}\" |
  .spec.arguments.parameters[0].value = \"${IMAGE_TAG}\" |
  .spec.arguments.parameters[1].value = \"${HARBOR_REGISTRY}/data-platform/spark-course-checker:${IMAGE_TAG}\"
" "${TEMPLATE_FILE}" > "${OUTPUT_FILE}"

echo "Generated: ${OUTPUT_FILE}"
```

Результирующий файл `workflows/submitted/spark-course-checker-abc1234.yaml`:

```yaml
# Сгенерирован GitLab CI на основе workflows/spark-job-workflow-template.yaml
# commit SHA: abc1234
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  name: spark-course-checker-abc1234     # подставлен yq
  namespace: argo
spec:
  entrypoint: spark-quality-dag
  arguments:
    parameters:
      - name: image-tag
        value: "abc1234"                 # подставлен yq
      - name: spark-image
        value: "harbor.internal.example.com/data-platform/spark-course-checker:abc1234"
  templates:
    - name: spark-quality-dag
      dag:
        tasks:
          # Шаг 1: скачать датасет — templateRef на шаблон из Д3
          - name: download-dataset
            templateRef:
              name: download-artifact-template   # из workflow-templates/01-download-artifact-template.yaml
              template: download-artifact
            arguments:
              parameters:
                - name: file-url
                  value: "s3://data-bucket/input/dataset.zip"
                - name: output-filename
                  value: "dataset.zip"

          # Шаг 2: запустить Spark-джоб
          - name: run-spark-job
            depends: download-dataset
            templateRef:
              name: spark-job-template
              template: run-spark
            arguments:
              parameters:
                - name: image
                  value: "{{workflow.parameters.spark-image}}"
                - name: main-class
                  value: "main"
              artifacts:
                - name: input-data
                  from: "{{tasks.download-dataset.outputs.artifacts.downloaded-file}}"

          # Шаг 3: сгенерировать отчёт — templateRef на шаблон из Д3
          - name: generate-report
            depends: run-spark-job
            templateRef:
              name: report-template              # из workflow-templates/04-report-template.yaml
              template: generate-report
            arguments:
              parameters:
                - name: project-name
                  value: "spark-course-checker"
                - name: student-name
                  value: "pipeline"
                - name: check-status
                  value: "{{tasks.run-spark-job.outputs.parameters.status}}"
                - name: missing-paths
                  value: "-"
              artifacts:
                - name: check-report
                  from: "{{tasks.run-spark-job.outputs.artifacts.spark-output}}"
```

**Связь с Д3:** шаги `download-dataset` и `generate-report` ссылаются через `templateRef` на те же шаблоны, которые были созданы в `hw11_example/templates/`: `download-artifact-template` и `report-template`. Паттерн `templateRef` — точно такой же, как в `hw11_example/workflows/coursework-check-workflow.yaml`. Шаблоны хранятся в `data-platform-infra/workflow-templates/` и применяются в кластере заранее через отдельный Argo CD Application.

### 3.3 Как Argo CD подхватывает Workflow

Argo CD Application `submitted-workflows` следит за директорией `workflows/submitted/`:

```yaml
# argocd/applications/submitted-workflows-app.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: submitted-workflows
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://gitlab.internal.example.com/data-platform-infra.git
    targetRevision: HEAD
    path: workflows/submitted
  destination:
    server: https://kubernetes.default.svc
    namespace: argo
  syncPolicy:
    automated:
      prune: true       # удалять из кластера объекты, исчезнувшие из Git
      selfHeal: true    # восстанавливать ручные изменения
```

**Механизм запуска:** Argo CD воспринимает `kind: Workflow` как обычный Kubernetes Custom Resource. При `kubectl apply` нового Workflow-объекта Argo Workflows controller (уже запущенный в кластере через Helm) немедленно подхватывает его через механизм informer watch и начинает исполнение. Это замыкает GitOps-цикл из лекции:

1. GitLab CI коммитит `spark-course-checker-abc1234.yaml` в `workflows/submitted/`
2. Argo CD (опрос каждые 3 минуты или через webhook) замечает новый файл
3. Argo CD сравнивает Git и кластер — файл есть в Git, нет в кластере
4. Argo CD применяет: `kubectl apply -f spark-course-checker-abc1234.yaml -n argo`
5. Argo Workflows controller видит новый `kind: Workflow` → запускает DAG

### 3.4 Helm для сторонних сервисов

Argo CD управляет Helm-релизами сторонних продуктов. Пример для Argo Workflows:

```yaml
# argocd/applications/argo-workflows-app.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: argo-workflows
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://argoproj.github.io/argo-helm
    chart: argo-workflows
    targetRevision: "0.42.2"          # версия чарта зафиксирована
    helm:
      valueFiles:
        - $values/helm-releases/argo-workflows/values.yaml
  sources:
    - repoURL: https://gitlab.internal.example.com/data-platform-infra.git
      targetRevision: HEAD
      ref: values
    - repoURL: https://argoproj.github.io/argo-helm
      chart: argo-workflows
      targetRevision: "0.42.2"
  destination:
    server: https://kubernetes.default.svc
    namespace: argo
  syncPolicy:
    automated:
      prune: false      # для стороннего ПО prune отключён — защита от случайного удаления
      selfHeal: true
```

Файл `helm-releases/argo-workflows/Chart.yaml` демонстрирует паттерн dependency wrapper из лекции (слайд "Chart.yaml", поле `dependencies`):

```yaml
# helm-releases/argo-workflows/Chart.yaml
apiVersion: v2
name: argo-workflows-release
version: 1.0.0
type: application
dependencies:
  - name: argo-workflows
    version: "0.42.2"
    repository: "https://argoproj.github.io/argo-helm"
    condition: argo-workflows.enabled
```

Аналогично установлены MinIO (Bitnami), Spark Operator (официальный чарт), PostgreSQL (Bitnami). При выходе новой версии оператор меняет `targetRevision` в `argocd/applications/` → GitOps-цикл → Argo CD вызывает `helm upgrade`. При проблеме — `helm rollback argo-workflows 1 --namespace argo`.

### 3.5 Kustomize для внутренних сервисов

Внутренние сервисы (dashboard-api, dashboard-ui) управляются через Kustomize по схеме base/overlays из лекции. Базовый манифест:

```yaml
# services/dashboard-api/base/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dashboard-api
spec:
  replicas: 1
  selector:
    matchLabels:
      app: dashboard-api
  template:
    metadata:
      labels:
        app: dashboard-api
    spec:
      containers:
        - name: dashboard-api
          image: dashboard-api          # Kustomize заменит тег через images:
          ports:
            - containerPort: 3000
```

Prod-оверлей с патчами для production:

```yaml
# services/dashboard-api/overlays/prod/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
patches:
  - patch: |-
      - op: replace
        path: /spec/replicas
        value: 3
      - op: add
        path: /spec/template/spec/containers/0/resources
        value:
          requests:
            cpu: "250m"
            memory: "512Mi"
          limits:
            cpu: "500m"
            memory: "1Gi"
    target:
      kind: Deployment
      name: dashboard-api
images:
  - name: dashboard-api
    newName: harbor.internal.example.com/data-platform/dashboard-api
    newTag: "abc1234"   # обновляется GitLab CI через yq при пуше нового образа
```

Dev-оверлей минимален:

```yaml
# services/dashboard-api/overlays/dev/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
patches:
  - patch: |-
      - op: replace
        path: /spec/replicas
        value: 1
    target:
      kind: Deployment
      name: dashboard-api
images:
  - name: dashboard-api
    newName: harbor.internal.example.com/data-platform/dashboard-api
    newTag: latest
```

Argo CD Application для prod-оверлея:

```yaml
# argocd/applications/dashboard-api-app.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: dashboard-api-prod
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://gitlab.internal.example.com/data-platform-infra.git
    targetRevision: HEAD
    path: services/dashboard-api/overlays/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: data-platform
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

При необходимости откатить сервис: `git revert <commit>` в infra-репо → Argo CD sync → Kustomize берёт предыдущий `newTag` → `kubectl apply -k` применяет предыдущий образ. Это отражает лекционный тезис: "GitOps — прозрачная история изменений, воспроизводимость и меньше ручных правок в production."

---

## Часть 4. Обоснование архитектуры

### 4.1 Сравнение Helm и Kustomize в контексте проекта

| Критерий | Helm (для 3rd-party) | Kustomize (для внутренних сервисов) |
|---|---|---|
| Источник манифестов | публичный chart (Bitnami, Argo) | собственные YAML в Git |
| Шаблонизация | Go-templates + values.yaml | JSON-патчи поверх base, без шаблонов |
| Rollback | `helm rollback <release> <rev>` | `git revert` + Argo CD sync |
| Версионирование | chart version + appVersion | Git SHA коммита |
| Зависимости | Chart.yaml dependencies | нет (нативные YAML) |
| Встроен в kubectl | нет (отдельная установка) | да (kubectl apply -k, с версии 1.14) |
| Когда применять | стороннее ПО, сложная параметризация | внутренние сервисы, GitOps, patch чужого манифеста |
| Ссылка на лекцию | слайд "Выбирайте Helm когда" | слайд "Выбирайте Kustomize когда" |

В некоторых случаях (например, патч чужого стороннего чарта) можно объединить оба подхода через post-rendering: `helm install --post-renderer ./kustomize-wrapper.sh` или через `helmCharts:` в `kustomization.yaml` (слайд "Helm + Kustomize вместе"). В данном проекте это не требуется: чёткое разграничение — Helm для стороннего ПО, Kustomize для внутреннего — достаточно.

### 4.2 Четыре ключевых преимущества схемы

**1. Переиспользование WorkflowTemplates**

Все Spark-джобы и data-пайплайны ссылаются на шаблоны из `workflow-templates/` через `templateRef`, не дублируя логику. Шаблоны из Д3 (`download-artifact-template`, `unpack-archive-template`, `project-check-template`, `report-template`) применяются в кластере один раз и переиспользуются несколькими Workflow. Добавление нового Spark-джоба — это создание нового файла в `workflows/submitted/` с нужными `templateRef`, без правки самих шаблонов. Это соответствует принципу, продемонстрированному в `hw11_example/`: "Основной Workflow связывает шаблоны через `templateRef`, поэтому отдельные блоки легко переиспользовать в других пайплайнах."

**2. Версионирование артефактов**

Каждый Docker-образ в Harbor тегируется `$CI_COMMIT_SHORT_SHA` (`abc1234`). Сгенерированный Workflow CRD в `workflows/submitted/` содержит тот же SHA в имени (`spark-course-checker-abc1234`) и в параметре `spark-image`. Это создаёт полную прослеживаемость: по имени запущенного Workflow в Argo можно найти точный коммит в `data-platform`, посмотреть diff, найти связанный MR и его ревью. Harbor хранит все версии образов — можно в любой момент перезапустить Workflow со старым SHA.

**3. Изоляция конфигурации сред**

Kustomize overlays (dev/staging/prod) — единственное место, где задаётся environment-специфичная конфигурация. Prod-оверлей определяет `replicas: 3` и `resource limits`; dev-оверлей — `replicas: 1` и `tag: latest`. Разработчик, работающий над базовым манифестом (`base/deployment.yaml`), физически не может случайно изменить prod-параметры. Это отражает лекционный паттерн base/overlays: `app/overlays/prod/kustomization.yaml — патч: replicas: 3`.

**4. Упрощение отката**

- **Helm (3rd-party):** `helm history minio -n minio` → `helm rollback minio 2 -n minio`. Helm хранит историю релизов в кластере.
- **Внутренние сервисы (Kustomize):** `git revert <sha>` коммита, который обновил `newTag` в `overlays/prod/kustomization.yaml` → пуш в infra-репо → Argo CD sync → кластер возвращается к предыдущему образу. Git — история изменений конфигурации.
- **Workflow CRDs:** `git rm workflows/submitted/spark-course-checker-badsha.yaml` + пуш → Argo CD с `prune: true` удаляет объект из кластера. Исполняемые Workflow можно остановить через `argo stop`.

Передача между командами (handoff) упрощается: весь желаемый state кластера описан в двух Git-репозиториях. Новый инженер клонирует infra-репо и получает полное понимание того, что развёрнуто и в каком окружении.

---

## Раздел 5. Верификация

Чеклист команд для проверки корректности работы схемы:

```bash
# 1. Убедиться, что WorkflowTemplates из Д3 применены
kubectl get workflowtemplate -n argo
# Ожидаемый вывод: download-artifact-template, unpack-archive-template,
#                  project-check-template, report-template

# 2. Проверить статус всех Argo CD Applications
argocd app list
# Ожидаемый вывод: все Applications в статусе Synced / Healthy

# 3. Проверить, что submitted-workflows Application видит новый Workflow
argocd app get submitted-workflows
# Видны ресурсы: Workflow/spark-course-checker-abc1234

# 4. Увидеть запущенный Workflow в кластере
kubectl get workflow -n argo
# Ожидаемый статус: Running → Succeeded

# 5. Посмотреть логи всех шагов
argo logs -n argo spark-course-checker-abc1234

# 6. Убедиться, что Helm релизы обновились
helm history argo-workflows -n argo
helm history minio -n minio

# 7. Проверить, что Spark Operator создал SparkApplication
kubectl get sparkapplication -n argo

# 8. Dry-run Kustomize prod-оверлея
kubectl kustomize services/dashboard-api/overlays/prod | grep -E "replicas|image"
# Ожидаемый вывод: replicas: 3, image: harbor.internal.example.com/.../dashboard-api:abc1234

# 9. Проверить образ в Harbor через CLI
curl -u ${HARBOR_USER}:${HARBOR_PASSWORD} \
  https://harbor.internal.example.com/v2/data-platform/spark-course-checker/tags/list
```

---

## Список источников

1. Helm Documentation — Chart File Structure. https://helm.sh/docs/topics/charts/
2. Kustomize — Declarative Management of Kubernetes Objects. https://kustomize.io/
3. Argo CD — Getting Started. https://argo-cd.readthedocs.io/en/stable/getting_started/
4. Argo Workflows — WorkflowTemplates. https://argoproj.github.io/argo-workflows/workflow-templates/
5. Harbor — Overview. https://goharbor.io/docs/
6. Spark Operator — Getting Started. https://www.kubeflow.org/docs/components/spark-operator/
7. GitOps Principles — OpenGitOps. https://opengitops.dev/
8. Bitnami Helm Charts — MinIO, PostgreSQL. https://charts.bitnami.com/bitnami
