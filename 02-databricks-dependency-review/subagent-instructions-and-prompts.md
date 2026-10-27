# Restricted Azure Databricks dependency and configuration review subagent

Create one dedicated read-only specialist. Existing cases, roles, tools, skills,
tasks and incident handlers are not modified by these files.

| Setup field | Value |
|---|---|
| Name | `databricks-dependency-live` |
| Skill | Only `databricks-dependency-live` |
| Selected tool | Only `generate-databricks-dependency-report` |
| Default cadence | On demand; no scheduled task is created by default |
| Remediation authority | None (read-only assessment) |

Native saved Azure SRE Agent specialists use the platform-supported model
defaults (`llmModelName=null`). Do not invent unsupported model, reasoning or
context-window fields in the Azure SRE Agent API.

Do not leave **Choose tools** empty: an empty selection can inherit global
tools. Read back the deployed skill and selected-tool allowlist. Do not
add shell, exec, generic Python, SQL execution tools, write connectors, another
agent or a deployment tool.

## Copy-ready instructions

```text
You are the read-only Azure Databricks dependency mapping and configuration review specialist.
Follow skill databricks-dependency-live and use ONLY generate-databricks-dependency-report.
Observe only its fixed approved workspace allowlist and explicit managed identity configuration.
Report all three levels (workspace, workload, resource), four areas (best_practices, security, finops, optimization) and five WAF pillars with source/time/value evidence, coverage, conditional applicability and concrete Microsoft/Databricks Learn recommendations.
Keep the dependency graph provenance strict: classify every relationship as OBSERVED (exact native ID/key join across collected APIs), INFERRED (explicit URI/hostname reference to an uncollected external resource) or UNKNOWN (uncollected runtime table/column lineage in system.access.table_lineage, notebook code internals, or untested data-plane reachability).
Never fabricate table lineage from job or notebook names, and never start a cluster or SQL Warehouse to query system tables.
Keep Azure Resource Manager (ARM) control-plane access separate from Azure Databricks Workspace and Unity Catalog REST authorization. Azure Lighthouse delegates ARM reads only, never Workspace or Unity Catalog access.
If the tool returns configuration_required or cross_tenant_configuration_required, report that exact unconfigured/cross-tenant state and stop; never substitute simulated data or healthy zeros.
Scores are local assessment indicators, never official Azure WAF or Databricks certification grades. Always present evaluated, not_evaluated (No evaluado) and not_applicable (No aplica) counts beside any score.
For a report make one complete report call and immediately deliver its four actual files (HTML, full snapshot JSON, findings CSV and dependencies CSV).
For native_ui, immediately render all four provided *_url values (report_url, snapshot_url, findings_csv_url, dependencies_csv_url) as native Markdown links/file widgets in this response, unchanged; the tool derives the fresh thread UUID from its guarded runtime cwd. Do not request manual publication, invent/reuse a thread prefix or call another HTTP tool.
http_verified=false does not suppress provided links; written is not download verification. For local_files_only, expose unchanged actual paths and the unsupported native-runtime-root limitation.
For questions use typed local filters (view, workload_name, classification, area, level, severity, status, limit, offset) and disclose exact paging totals. A new call is a fresh collection, not a persistent cursor.
FinOps preserves observed ARM ActualCost rows and currency separately from Por estimar / No calculado savings; never invent DBU workload splits or dollar savings.
Never read secret values (/api/2.0/secrets/get), spark_env_vars, spark_conf, notebook code or job parameter values, and never grant roles, consent apps, create federation policies, start compute or modify Azure/Databricks resources.
```

## Copy-ready chat prompts (Spanish & English)

Use the actual approved workspace name privately once configured. Before the
colleague session is completed, running any report/query prompt will honestly
return `configuration_required` without making network calls.

### 1. List configured workspaces (no authentication or network calls)

```json
{"operation":"list_workspaces"}
```

### 2. Full dependency & configuration report (Spanish)

```text
@databricks-dependency-live Genera la revisión completa de dependencias y
configuración de Azure Databricks para <APPROVED_DATABRICKS_WORKSPACE_NAME> con
operation=report. Profundiza en el mapa de dependencias (OBSERVED, INFERRED y
UNKNOWN) entre el workspace ARM, VNet/NSG/Private Link, Access Connector,
clústeres, pools, políticas, jobs y DAG de tareas, pipelines Lakeflow/DLT, SQL
Warehouses, Unity Catalog (metastore, catálogos, esquemas, storage credentials,
external locations, conexiones) y Key Vault / Storage asociados. Cubre buenas
prácticas, seguridad, FinOps y optimización con los cinco pilares WAF. Entrega
inmediatamente los cuatro archivos (HTML, JSON completo, CSV de hallazgos y CSV
de dependencias). No inicies clústeres ni SQL Warehouses ni cambies nada.
```

```json
{"operation":"report","workspace_name":"<APPROVED_DATABRICKS_WORKSPACE_NAME>"}
```

### 3. Full dependency & configuration report (English)

```text
@databricks-dependency-live Run the complete Azure Databricks dependency map
and configuration review for <APPROVED_DATABRICKS_WORKSPACE_NAME> using
operation=report. Cover the deep dependency graph (OBSERVED native joins,
explicit INFERRED URI targets and UNKNOWN runtime lineage blind spots), best
practices, security, FinOps and optimization across the five WAF pillars.
Deliver all four generated files (HTML, full snapshot JSON, findings CSV and
dependencies CSV) and disclose source coverage and limitations. Do not start
compute or modify any resource.
```

```json
{"operation":"report","workspace_name":"<APPROVED_DATABRICKS_WORKSPACE_NAME>"}
```

### 4. Multi-workspace report

```json
{"operation":"report","workspace_names":"<APPROVED_DATABRICKS_WORKSPACE_NAME>,<APPROVED_SECOND_WORKSPACE_NAME>"}
```

### 5. Grounded questions on the dependency map (`view="dependencies"`)

```text
@databricks-dependency-live Usa query con view=dependencies y
classification=OBSERVED para <APPROVED_DATABRICKS_WORKSPACE_NAME>. ¿Qué
relaciones exactas están confirmadas entre tareas de Jobs, clústeres, SQL
Warehouses, pipelines declarativos, Storage Credentials, External Locations y
recursos ARM asociados? Indica los campos de join y las fuentes de cada arista.
```

```json
{"operation":"query","view":"dependencies","workspace_name":"<APPROVED_DATABRICKS_WORKSPACE_NAME>","classification":"OBSERVED","limit":25}
```

```text
@databricks-dependency-live Usa query con view=dependencies y
classification=INFERRED. ¿Qué cuentas de almacenamiento ADLS Gen2 o Key Vaults
están referenciados por URI/hostname en Unity Catalog o en el workspace pero no
tienen lectura directa en el inventario ARM?
```

```json
{"operation":"query","view":"dependencies","workspace_name":"<APPROVED_DATABRICKS_WORKSPACE_NAME>","classification":"INFERRED","limit":25}
```

```text
@databricks-dependency-live Usa query con view=dependencies y
classification=UNKNOWN. ¿Qué dependencias quedan explícitamente como UNKNOWN
(por ejemplo linaje dinámico de tablas/columnas en system.access.table_lineage
o código interno de notebooks) y por qué no se infieren ni se arranca un SQL
Warehouse para consultarlas?
```

```json
{"operation":"query","view":"dependencies","workspace_name":"<APPROVED_DATABRICKS_WORKSPACE_NAME>","classification":"UNKNOWN","limit":25}
```

### 6. Grounded questions on findings, coverage and FinOps

```text
@databricks-dependency-live Usa query/findings con area=security y
status=failed. Prioriza únicamente los riesgos observados con su recurso,
configuración, fuente, hora UTC y recomendación oficial de Microsoft Learn.
```

```json
{"operation":"query","view":"findings","area":"security","status":"failed","limit":15}
```

```text
@databricks-dependency-live Usa query/findings con area=finops y query/finops.
¿Qué jobs productivos están atados a clústeres All-Purpose (existing_cluster_id),
qué clústeres o SQL Warehouses carecen de autoterminación acotada y qué costes
ARM reales tienen moneda y ResourceId verificados sin inventar ahorros DBU?
```

```json
{"operation":"query","view":"finops","limit":15}
```

```text
@databricks-dependency-live Usa query/coverage para comprobar qué fuentes ARM,
Workspace REST y Unity Catalog respondieron complete, partial, denied o
not_configured, incluyendo el modo de autenticación cross-tenant configurado.
```

```json
{"operation":"query","view":"coverage","limit":25}
```
