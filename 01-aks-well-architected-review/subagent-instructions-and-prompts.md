# Restricted AKS review subagent

Create one separate specialist. Existing cases, roles, tools, skills, tasks
and incident handlers are not changed by these files.

| Setup field | Value |
|---|---|
| Name | `aks-waf-live` |
| Skill | Only `aks-waf-live` |
| Selected tool | Only `generate-aks-waf-report` |
| Default cadence | On demand; no scheduled task is created |
| Remediation authority | None |

Do not leave **Choose tools** empty: an empty selection can inherit global
tools. Read back the deployed skill and selected-tool list. Do not add shell,
exec, generic Python, write connectors, another agent or a deployment tool.

> [!NOTE]
> **Dated SRE UI badge caveat:** In the live SRE Agent UI, Agent Canvas list/card summary badges can display inherited global rollups (for example, `131 tools` / `52 skills`) even when both native APIs, selected chips and the saved form policy enforce one explicit tool and skill (`tools`, `allowedSkills`, `addSystemSkills=false`, `disableDocumentRetrieval=true`). Verify the exact selected tool and skill **names** in the saved form and effective supported policy; the list summary badge is not authoritative restriction proof, and you must not modify globals to change it.

## Copy-ready instructions

```text
You are the read-only AKS architecture/configuration review specialist.
Follow skill aks-waf-live and use ONLY generate-aks-waf-report.
Observe only its fixed approved cluster allowlist and explicit managed identity.
For a multi-subscription fleet, page list_clusters with limit=100 and the
returned next_offset until null, use only returned cluster_name keys, and
review in report batches of at most five cluster_names, then summarize the
fleet per subscription/environment without extrapolating unreported clusters.
Report all three levels, four areas and five pillars with source/time/value
evidence, coverage, conditional applicability and concrete linked recommendations.
Scores are local assessment indicators, never official Azure WAF/Polaris/WARA
grades or certification. No evaluated checks means null/No evaluado.
Keep LAB/PRODUCTION separate from LIVE/SIMULATED. ARM Running and enabled addons
do not demonstrate application health, enforcement, measurements or license.
No IaC supplied does not mean portal-created infrastructure or drift.
No Kubernetes access means unknown manifests, not no workloads. Honor per-kind
403/502/partial lists and do not use admin config, OBO, another identity or
command invoke to bypass them. Only strict CA/FQDN TLS GETs are allowed.
For a report make one complete report call and immediately deliver its four
actual files. For native_ui, immediately render all four provided *_url values
as native Markdown links/file widgets in this response, unchanged; the tool
derives the fresh thread UUID from its guarded runtime cwd. Do not request
manual publication, invent/reuse a thread prefix or call another HTTP tool.
http_verified=false does not suppress provided links; written is not download
verification. For local_files_only, expose unchanged actual paths and the
unsupported native-runtime-root limitation. Show partial source evidence
explicitly without hiding files. A current Kubernetes 502 is No evaluado,
not old workload data, empty-health success or a reason to broaden access.
For questions use typed local filters and exact paging totals. A new call is a
fresh collection, not the previous report. No hidden paging just to learn totals.
Do not pad critical risks, infer owners or promise effort/delivery/savings.
FinOps keeps actual currency/window/ResourceId and current usage units/samples;
requests are not usage, current metrics are not historical underutilization,
node costs are not workload billing and nodeRG names do not prove orphan assets.
On error show the sanitized code/source and stop, with no fabricated files,
zero/healthy fallback, narrower success retry, ZIP or Storage workaround.
Never grant roles, enable plans, install tools/metrics, patch/delete/scale/upgrade,
start/stop clusters, execute commands, reveal credentials or create issues/tasks.
```

## Spanish chat prompts

Use the actual approved cluster name privately. The public label remains
**CLIENTE**. None of these prompts authorizes changing Azure.

List the fixed roster before choosing a scope. It is configuration metadata,
not observed cluster health. Multi-cluster selection uses exact approved
names; reports retain all selected snapshots and disclose failed cluster
collections instead of replacing them with another cluster's data.

```json
{"operation":"list_clusters"}
```

Flota multi-suscripcion (decenas de suscripciones): pagina el roster y
revisa por lotes de hasta cinco clusteres.

```text
@aks-waf-live Lista todos los clusteres AKS aprobados con operation=list_clusters,
limit=100, siguiendo next_offset hasta que sea null. Resume cuantos hay por
suscripcion y entorno. Despues genera informes por lotes de hasta 5 clusteres
(cluster_names) empezando por PRODUCTION y termina con un resumen de flota con
puntuacion y cobertura por lote. No extrapoles clusteres no revisados.
```

```json
{"operation":"list_clusters","limit":100,"offset":0}
```

```json
{"operation":"report","cluster_names":"<APPROVED_AKS_NAME>,<APPROVED_SECOND_AKS_NAME>"}
```

```json
{"operation":"query","view":"architecture","cluster_name":"<APPROVED_AKS_NAME>","limit":5}
```

```text
@aks-waf-live Genera la revision de configuracion de AKS para
<APPROVED_AKS_NAME> con operation=report. Cubre cluster, workloads y recursos
asociados: buenas practicas, seguridad, FinOps y optimizacion, con los cinco
pilares WAF. Entrega HTML, JSON completo, CSV de hallazgos y CSV de inventario.
Muestra cobertura, fuentes, No evaluado/No aplica y limites. No cambies nada.
```

```json
{"operation":"report","cluster_name":"<APPROVED_AKS_NAME>"}
```

```text
@aks-waf-live Usa query/coverage para <APPROVED_AKS_NAME>. Que fuentes
fallaron o fueron parciales, con que codigo HTTP y que campos no podemos
evaluar? Un 502/429 no es permiso denegado, y un 403 no significa cero objetos.
```

```json
{"operation":"query","view":"coverage","cluster_name":"<APPROVED_AKS_NAME>","status":"denied","limit":10}
```

```text
@aks-waf-live Usa query/findings con area=security y status=failed.
Prioriza solo riesgos observados con recurso, configuracion, fuente, hora,
impacto cualificado y recomendacion Microsoft. No inventes hallazgos criticos.
```

```json
{"operation":"query","view":"findings","area":"security","status":"failed","limit":10}
```

```text
@aks-waf-live Usa query/workloads y query/findings para namespace=client-apps
y workload_name=frontend. Distingue replicas deseadas, probes, requests,
PDB/HPA/NetworkPolicy seleccionados y datos No evaluado. No infieras la
configuracion de Kubernetes a partir de telemetria o del estado Running de ARM.
```

```json
{"operation":"query","view":"findings","namespace":"client-apps","workload_name":"frontend","limit":5}
```

```text
@aks-waf-live Usa query/finops. Que mediciones tienen timestamp, ventana,
unidad, muestra y denominador? Que costes tienen ResourceId y moneda reales?
Separa candidatos de capacidad actual de infrautilizacion sostenida y de
ahorro calculado. No compres planes, cambies SKU o elimines activos.
```

```json
{"operation":"query","view":"finops","limit":5}
```

```text
@aks-waf-live Usa query/summary y explica puntuacion y cobertura juntas.
Cuantos indicadores se evaluaron, cuantos son No evaluado y No aplica?
Explica por que esta puntuacion local no certifica la salud del cluster ni
constituye el Azure Architecture Review oficial.
```

```json
{"operation":"query","view":"summary"}
```

## English chat prompts

Use the same private approved cluster names. The public client label remains
**CLIENTE**, and every prompt below is strictly read-only.

```text
@aks-waf-live Use operation=list_clusters to list the fixed configured cluster
roster before choosing a scope. Explain why this roster is configuration
metadata rather than observed cluster presence or health.
```

```json
{"operation":"list_clusters"}
```

```text
@aks-waf-live Generate the complete read-only AKS configuration review for
<APPROVED_AKS_NAME> with operation=report. Cover cluster, workloads and
associated resources across best practices, security, FinOps and optimization,
including all five WAF pillars. Immediately deliver the HTML, complete JSON,
findings CSV and inventory CSV. Show coverage, sources, No evaluado/No aplica
and limits; do not change anything.
```

```json
{"operation":"report","cluster_name":"<APPROVED_AKS_NAME>"}
```

```text
@aks-waf-live Generate the combined read-only AKS configuration review for
<APPROVED_AKS_NAME>,<APPROVED_SECOND_AKS_NAME> with operation=report and
cluster_names (exact comma-separated names, no spaces). Deliver all four files,
preserve every per-cluster snapshot and disclose any failed cluster read
without borrowing another cluster's score, workloads or cost.
```

```json
{"operation":"report","cluster_names":"<APPROVED_AKS_NAME>,<APPROVED_SECOND_AKS_NAME>"}
```

```text
@aks-waf-live Use query/coverage for <APPROVED_AKS_NAME>. Which sources failed
or were partial, with what HTTP/error code, and which checks cannot be
evaluated? Explain why HTTP 502/429 is not permission denied and why HTTP 403
does not mean zero Kubernetes objects.
```

```json
{"operation":"query","view":"coverage","cluster_name":"<APPROVED_AKS_NAME>","status":"denied","limit":10}
```

```text
@aks-waf-live Use query/findings with area=security and status=failed.
Prioritize only observed risks with their resource, configuration, source,
UTC timestamp, qualified impact and Microsoft recommendation. Do not invent
critical findings.
```

```json
{"operation":"query","view":"findings","area":"security","status":"failed","limit":10}
```

```text
@aks-waf-live Use query/workloads and query/findings for namespace=client-apps
and workload_name=frontend. Distinguish desired replicas, probes, requests,
matched PDB/HPA/NetworkPolicy and No evaluado items. Do not infer Kubernetes
manifests from telemetry or from ARM Running state.
```

```json
{"operation":"query","view":"findings","namespace":"client-apps","workload_name":"frontend","limit":5}
```

```text
@aks-waf-live Use query/finops. Which measurements include timestamp, window,
unit, sample count and denominator? Which costs have real ResourceId and
currency? Separate current capacity-review candidates from sustained
underutilization and from calculated monetary savings. Do not buy plans,
change SKUs or delete assets.
```

```json
{"operation":"query","view":"finops","limit":5}
```

```text
@aks-waf-live Use query/summary and explain score and coverage together.
How many checks were evaluated, and how many are No evaluado or No aplica?
Explain why this local pass indicator neither certifies cluster health nor
constitutes the official Azure Architecture Review.
```

```json
{"operation":"query","view":"summary"}
```
