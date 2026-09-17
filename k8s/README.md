# k8s — Deploy do metricas-loader no ARO OpenShift

Manifests para publicar o **metricas-loader** (worker de carga contínua
Mimir → PostgreSQL) no **Azure Red Hat OpenShift (ARO)**.

O worker não expõe HTTP: é um processo que roda em loop (`INTERVALO_SEGUNDOS`),
lê do Mimir e grava no Postgres. Por isso **não há Service nem Route** para ele —
apenas um `Deployment` de 1 réplica.

## Arquivos

| Arquivo | O que é | Aplicado por padrão (`-k`) |
| --- | --- | --- |
| `namespace.yaml` | Projeto/namespace `metricas` | ✅ |
| `configmap.yaml` | Variáveis não sensíveis + `metricas.json` | ✅ |
| `deployment.yaml` | Worker (`replicas: 1`, estratégia `Recreate`) | ✅ |
| `secret.example.yaml` | Modelo do Secret (POSTGRES_DSN, auth Mimir) | ❌ crie `secret.yaml` |
| `build.yaml` | ImageStream + BuildConfig (build no cluster) | ❌ opcional |
| `job-backfill.yaml` | Job pontual de backfill histórico | ❌ sob demanda |
| `optional-postgres.yaml` | Postgres interno (só POC) | ❌ prefira DB gerenciado |
| `kustomization.yaml` | Base kustomize | — |

## Pré-requisitos

- CLI `oc` autenticado no cluster ARO (`oc login ...`).
- Um Mimir acessível pelo cluster (ajuste `MIMIR_URL` no `configmap.yaml`).
- Um Postgres de destino **vazio** (banco criado, sem schema): o próprio worker
  provisiona o schema `metricas` no boot a partir do DDL único embarcado
  (`metricas-loader/src/loader/sql/schema.sql`), de forma idempotente. O usuário
  do `POSTGRES_DSN` precisa de permissão de DDL no banco (`CREATE` no database).
  Para provisionar sem subir o worker: `MODO=schema` (roda o DDL e sai).
  Em ARO, o recomendado é o **Azure Database for PostgreSQL Flexible Server**.

## Passo a passo

### 1. Criar o projeto e o Secret

```bash
oc new-project metricas   # ou: oc apply -f namespace.yaml

# Secret com os dados sensíveis (NÃO versione valores reais):
oc -n metricas create secret generic metricas-loader-secret \
  --from-literal=POSTGRES_DSN='postgresql://metricas:SENHA@HOST:5432/metricas' \
  --from-literal=MIMIR_BEARER_TOKEN='' \
  --from-literal=MIMIR_BASIC_AUTH_USER='' \
  --from-literal=MIMIR_BASIC_AUTH_PASS=''
```

### 2. Obter a imagem

**Opção A — buildar no próprio cluster (registry interno do OpenShift):**

```bash
oc -n metricas apply -f build.yaml
# Buildar a partir do fonte local (roda a partir da raiz do repo):
oc -n metricas start-build metricas-loader --from-dir=metricas-loader --follow
```

**Opção B — buildar fora (CI/ACR)** e ajustar `image:` no `deployment.yaml`
para `<seu-acr>.azurecr.io/metricas-loader:<tag>`. Para o cluster puxar de um
ACR privado, vincule o pull secret:

```bash
oc -n metricas create secret docker-registry acr-pull \
  --docker-server=<seu-acr>.azurecr.io \
  --docker-username=<user> --docker-password=<senha>
oc -n metricas secrets link default acr-pull --for=pull
```

### 3. Aplicar os manifests base

```bash
# Sincronize o configmap.yaml com metricas-loader/config/metricas.json antes.
oc apply -k k8s/
```

### 4. Verificar

```bash
oc -n metricas rollout status deploy/metricas-loader
oc -n metricas logs -f deploy/metricas-loader        # logs JSON, uma linha por evento
```

## Operações comuns

**Backfill histórico** (recarrega um período do Mimir):

```bash
# edite BACKFILL_INICIO/BACKFILL_FIM em job-backfill.yaml
oc -n metricas delete job metricas-backfill --ignore-not-found
oc -n metricas apply -f job-backfill.yaml
oc -n metricas logs -f job/metricas-backfill
```

**Import de histórico via CSV** (no pod do worker, via stdin):

```bash
POD=$(oc -n metricas get pods -l app.kubernetes.io/name=metricas-loader \
  -o jsonpath='{.items[0].metadata.name}')
oc -n metricas exec -i "$POD" -- python -m loader.import_csv < historico.csv
```

**Adicionar/remover métricas sem reiniciar** — edite o ConfigMap
`metricas-loader-metricas` (o worker relê `metricas.json` a cada rodada):

```bash
oc -n metricas edit configmap metricas-loader-metricas
```

## Notas de OpenShift / ARO

- **SCC `restricted-v2`**: o `deployment.yaml` não fixa `runAsUser` — o UID é
  atribuído pelo namespace. Capabilities são descartadas e
  `allowPrivilegeEscalation: false`. A imagem já roda como não-root.
- **Réplica única**: o worker usa um watermark no banco; **não** aumente
  `replicas` — duas réplicas processariam a mesma janela em duplicidade. A
  estratégia `Recreate` garante que não haja dois pods simultâneos no rollout.
- **Segredos**: em produção, prefira o *Azure Key Vault Provider for Secrets
  Store CSI Driver* ou o *External Secrets Operator* em vez de um Secret em texto.
