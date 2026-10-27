# Azure SRE Agent · Guía paso a paso: AKS (multi-suscripción) y Azure Databricks

Esta guía explica, paso a paso y con capturas reales del portal, cómo desplegar en
[Azure SRE Agent](https://sre.azure.com) dos casos de uso **de solo lectura**:

| # | Caso | Qué obtienes | Carpeta |
|---|---|---|---|
| 1 | **Revisión de arquitectura y configuración de AKS** (Well-Architected) | Informe HTML + JSON + 2 CSV por clúster o por lotes de clústeres, en **todas tus suscripciones** (hasta 500 clústeres) | [`01-aks-well-architected-review`](01-aks-well-architected-review/README.md) |
| 2 | **Mapa de dependencias y revisión de Azure Databricks** | Informe HTML + JSON + 2 CSV del workspace: dependencias, seguridad, buenas prácticas y FinOps | [`02-databricks-dependency-review`](02-databricks-dependency-review/README.md) |

> **Antes de empezar:** si todavía no habéis desplegado el caso de **auditoría de credenciales
> y certificados de Microsoft Entra ID**, es el más sencillo y sirve para familiarizarse con el
> portal. Su guía paso a paso está aquí:
> **[Caso 01 · Entra credential expiration audit](https://github.com/marioaguileraaa/sre-agent-use-cases/tree/main/01-entra-credential-expiration-audit)**.

---

## Qué valor aportan

- **AKS:** en vez de revisar clúster a clúster en el portal, el SRE Agent recorre toda la flota
  (decenas de suscripciones), aplica los checks de Azure Well-Architected a nivel **clúster,
  workload y recursos asociados**, y entrega un informe con hallazgos priorizados, cobertura
  (qué se pudo leer y qué no) y, opcionalmente, costes reales.
- **Databricks:** traza cómo se conectan el workspace, la red (VNet injection, Private Link),
  clusters, pools, jobs, pipelines, SQL Warehouses, Unity Catalog, Secret Scopes y los recursos
  Azure asociados; marca cada dependencia como `OBSERVED`, `INFERRED` o `UNKNOWN` y la compara
  con una arquitectura de referencia.
- **Seguridad:** ambos casos son **solo lectura**. La identidad del SRE Agent solo recibe roles
  de lectura, el subagente solo tiene **una** herramienta y **un** skill, y ninguno puede
  modificar, escalar, borrar ni ejecutar comandos.

---

## Plan sugerido (próximas 2 semanas)

| Semana | Tarea | Tiempo aproximado | Guía |
|---|---|---|---|
| 1 | (Si no está hecho) desplegar el caso de **credenciales de Entra ID** | 30 min | [Caso 01](https://github.com/marioaguileraaa/sre-agent-use-cases/tree/main/01-entra-credential-expiration-audit) |
| 1 | **AKS:** pasar a multi-suscripción (si ya lo tenéis, son **7 cambios**) | 45–60 min | [AKS → «Si ya lo tenéis desplegado»](01-aks-well-architected-review/README.md#a-si-ya-lo-tenéis-desplegado-qué-cambiar-7-pasos) |
| 2 | **Databricks:** desplegarlo y generar el primer informe | 45 min | [Databricks](02-databricks-dependency-review/README.md) |
| 2 | Revisar los informes y apuntar dudas para la siguiente sesión | — | — |

---

## Requisitos previos (comunes a los dos casos)

1. **Un Azure SRE Agent ya creado** en [sre.azure.com](https://sre.azure.com) y permiso para
   crear herramientas, skills y subagentes en él (rol **SRE Agent Administrator**, o
   **SRE Agent Author + SRE Agent Standard User**).
2. **El Client ID de la identidad del SRE Agent** (lo usan los scripts y el código):
   **sre.azure.com → vuestro agente → Settings → Azure settings → Managed Identity →
   Go to Identity → Overview → _Client ID_**.
   > ⚠️ Es el **Client ID**, no el *Object (principal) ID*. Son dos GUID distintos.
3. **PowerShell 7** y **Azure CLI** (`az`) con `az login` en el tenant correcto.
4. **Las personas con los roles adecuados** para dar permisos: ver
   [⭐ Roles: quién necesita qué](#-roles-quién-necesita-qué-resumen) justo debajo.
5. **Un solo tenant:** todas las suscripciones que se revisen deben estar en el mismo tenant de
   Entra ID que el SRE Agent.

Cambiar de suscripción en PowerShell (por si lo necesitáis):

```powershell
az account list -o table                       # ver suscripciones
az account set --subscription "<id-o-nombre>"  # cambiar la activa
az account show -o table                       # comprobar
```

---

## ⭐ Roles: quién necesita qué (resumen)

> **Es lo que más tiempo lleva.** Antes de la sesión, comprobad que **cada persona** tiene su rol
> **asignado y activo** (si usáis PIM, activado). Cada guía tiene la sección completa con
> comprobaciones y errores típicos:
> [roles AKS](01-aks-well-architected-review/README.md#-roles-y-permisos-quién-necesita-qué-leer-antes-de-empezar) ·
> [roles Databricks](02-databricks-dependency-review/README.md#-roles-y-permisos-quién-necesita-qué-leer-antes-de-empezar).

**Roles de las personas** (lo que necesita quien hace cada tarea):

| Persona | Qué hace | Rol que necesita | Dónde | Caso |
|---|---|---|---|---|
| **Administrador de Azure** | Ejecuta el script de permisos (da roles de lectura a la identidad del agente) | **Owner**, **User Access Administrator** o **Role Based Access Control Administrator**. ❌ **Contributor NO sirve** (no puede asignar roles) | AKS: en el **Management Group** (si usa `-ManagementGroupId`) o en **cada suscripción**. Databricks: en la suscripción del workspace | Ambos |
| **Administrador del workspace Databricks** | Añade la identidad como *service principal* y le da `CAN_VIEW` en jobs, pipelines y warehouses | **Workspace Admin**. Un Owner/Contributor de Azure lo es automáticamente al pulsar **Launch Workspace** en el portal | Workspace de Databricks | Databricks |
| **Administrador de Unity Catalog** | Da `BROWSE`, `USE_CATALOG`, `USE_SCHEMA` en los catálogos | **Metastore Admin**, **owner** del catálogo o privilegio `MANAGE` | Unity Catalog | Databricks |
| **Quien configura el SRE Agent** | Crea herramienta, skill y subagente; usa *Test playground* | **SRE Agent Administrator**, o **SRE Agent Author + SRE Agent Standard User**. ❌ Owner/Contributor de Azure **no** los sustituyen | IAM del recurso **SRE Agent** | Ambos |
| **Usuarios** | Piden informes en el chat | **SRE Agent Standard User** | IAM del recurso **SRE Agent** | Ambos |
| **Equipo de plataforma AKS** *(opcional)* | Activa Entra ID + Azure RBAC en los clústeres | **Contributor** o **Azure Kubernetes Service Contributor Role** | Clúster AKS | AKS |

**Roles de la identidad del SRE Agent** (los da el script; **todos de solo lectura**):

| Caso | Roles |
|---|---|
| AKS | **Reader** + **Azure Kubernetes Service Cluster User Role** + **Azure Kubernetes Service RBAC Reader** (+ **Cost Management Reader** si se usan costes) |
| Databricks | **Reader** en la suscripción + *service principal* en el workspace con `workspace-access` + **CAN_VIEW** en jobs/pipelines/warehouses + `BROWSE`/`USE_CATALOG`/`USE_SCHEMA` en Unity Catalog (+ **Cost Management Reader** opcional) |

Comprobad vuestro propio rol de Azure antes de ejecutar un script:

```powershell
$me = az ad signed-in-user show --query id -o tsv
az role assignment list --assignee $me --all --include-inherited --include-groups `
    --query "[].{rol:roleDefinitionName, alcance:scope}" -o table
```

---

## ⭐ Test playground: probar la herramienta antes del chat

Cada herramienta Python tiene un **Test playground** en el Builder con sus 12 parámetros. Cada guía
explica **qué valor poner en cada parámetro** para las 3 pruebas (configuración → permisos →
informe) y qué significa cada error:
[playground AKS](01-aks-well-architected-review/README.md#-test-playground-qué-valores-poner-en-cada-parámetro) ·
[playground Databricks](02-databricks-dependency-review/README.md#-paso-4--test-playground-qué-valores-poner-en-cada-parámetro).

Resumen:

1. **Dejad los valores por defecto** en todos los campos: `report`, `summary`, `all`, `25`, `0`.
   **Nunca vacíos** los campos de lista (`operation`, `view`, `area`, `level`, `severity`,
   `status`…): vacío da error.
2. **Prueba 1:** `operation = list_clusters` (AKS) o `list_workspaces` (Databricks), sin elegir
   nombre → comprueba la configuración.
3. **Prueba 2:** `operation = query`, `view = coverage` y el nombre del clúster/workspace →
   comprueba los **permisos** (fuentes `denied` = falta un rol).
4. **Prueba 3:** `operation = report` y el nombre, **resto por defecto** → informe completo.
5. «Function test succeeded» solo significa que el código se ejecutó: mirad el `status` del
   resultado.

---

## Cómo está organizado cada caso

Las dos carpetas siguen la misma estructura:

| Fichero | Para qué sirve |
|---|---|
| `README.md` | **Guía paso a paso en español** (empieza aquí) |
| `grant-*-permissions.ps1` | Script que da los permisos de solo lectura y **genera el bloque de configuración** |
| `generate-*-report.py` | Código de la herramienta Python. Solo se edita el bloque **CONFIGURACION** del principio |
| `requirements.txt` | Paquetes Python que se pegan en el campo *Requirements* de la herramienta |
| `tool-parameters.schema.json` | Los 12 parámetros de la herramienta (nombre, tipo, valor por defecto) |
| `skills/*/SKILL.md` | Texto del skill |
| `subagent-instructions-and-prompts.md` | Instrucciones del subagente y prompts de ejemplo |
| `README-tecnico-EN.md` | Documentación técnica completa en inglés (referencia) |
| `images/` | Capturas del portal y del informe |

**Orden de despliegue (igual en los dos casos):**
permisos → editar el `.py` → herramienta Python → skill → subagente → probar en un chat nuevo.

---

## Preguntas frecuentes

**¿El SRE Agent puede cambiar algo en AKS o Databricks?**
No. Solo recibe roles de lectura, la herramienta solo hace peticiones `GET`, y el subagente tiene
prohibido remediar. Las recomendaciones del informe las aplica una persona.

**¿Hace falta habilitar «Entra ID + Azure RBAC» en los AKS?**
Solo si queréis que el informe evalúe también los **workloads** (deployments, probes, requests,
PDB, HPA, NetworkPolicy…). Sin ello, el informe sale igual con el nivel clúster y recursos, y la
parte de workloads aparece como *No evaluado*. Es una práctica recomendada por Microsoft
(identidades de Entra ID y autorización con Azure RBAC en lugar de cuentas locales), pero cambia
cómo se autentican las personas en el clúster, así que **hay que planificarlo con el equipo de
plataforma**. Detalle en la [guía de AKS](01-aks-well-architected-review/README.md#entra-id--azure-rbac-en-los-clústeres).

**¿Y los costes?**
Son opcionales. Si se ejecuta el script con `-IncludeCost`, da el rol **Cost Management Reader**
y el bloque generado ya pone `cost_scope = "node_resource_group"`. El informe muestra entonces
coste real (*ActualCost*) con moneda, ventana y `ResourceId`.

**¿Dónde se ven los ficheros generados?**
En el propio chat: el subagente devuelve 4 enlaces descargables (HTML, JSON, CSV de hallazgos,
CSV de inventario).

**Me da `SyntaxError` al probar la herramienta.**
Casi siempre es un pegado incorrecto en el bloque de configuración: dos líneas que han quedado
unidas (`..."CLIENT_NAME = ...`) o unas comillas sin cerrar. Cada variable debe ir en su propia
línea.
