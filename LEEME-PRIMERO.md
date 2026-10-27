# Gestamp · Azure SRE Agent · Resumen en una página

Repositorio **temporal para Gestamp** (se borrará al terminar las pruebas): los `.py` ya llevan el
nombre y el logo de Gestamp. Solo tenéis que poner el Client ID de vuestro SRE Agent. Este fichero es el **resumen**; la guía detallada, con capturas,
está en el `README.md` de cada carpeta.

| Carpeta | Qué es | Qué hacer ahora |
|---|---|---|
| `01-aks-well-architected-review/` | Revisión de AKS, **ahora multi-suscripción** (30+ suscripciones, hasta 500 clústeres) | **Actualizar** lo que ya tenéis: [7 cambios](#1-aks-qué-cambiar-en-lo-que-ya-tenéis-7-cambios) |
| `02-databricks-dependency-review/` | Dependencias, configuración, seguridad y FinOps de Databricks (un solo tenant) | **Desplegar y probar** antes de la próxima sesión: [pasos](#2-databricks-desplegar-y-probar) |
| `00-sre-usecase-builder-v3/` | Subagente *builder* para crear casos nuevos | Opcional |

Todo es **solo lectura**: no se cambia nada en AKS, Databricks ni Azure.

En cada carpeta:
- `README.md`: la guía paso a paso en español (la misma que la del repo público).
- `README-tecnico-EN.md`: el detalle técnico en inglés.

---

## ⭐ 0. Roles: quién necesita qué (leer antes de nada)

Es lo que más tiempo lleva. **Comprobad que cada persona tiene su rol activo** antes de la sesión.
Si usáis PIM, tiene que estar **activado**, no solo «elegible».

### Roles de las personas

| Persona | Para qué | Rol que necesita | Dónde se asigna |
|---|---|---|---|
| **A. Admin de Azure** | Ejecutar `grant-aks-permissions.ps1` y `grant-databricks-permissions.ps1` (asignan roles a la identidad del agente) | **Owner**, **User Access Administrator** o **Role Based Access Control Administrator**. ❌ **Contributor NO sirve** (no puede asignar roles) | AKS: **en el Management Group** que contiene las 30+ suscripciones (o en cada suscripción). Databricks: en la suscripción del workspace |
| **B. Admin del workspace Databricks** | Añadir la identidad del agente al workspace y darle `CAN_VIEW` | **Workspace Admin** | Se consigue entrando en **Portal → workspace → Launch Workspace** con Owner o Contributor en Azure |
| **C. Admin de Unity Catalog** | Dar `BROWSE`, `USE_CATALOG` y `USE_SCHEMA` en los catálogos | **Metastore Admin**, o **owner** del catálogo, o `MANAGE` en el catálogo | Databricks → Catalog → Permissions |
| **D. Quien configura el SRE Agent** | Editar la herramienta, el skill y el subagente; usar *Test playground* | **SRE Agent Administrator**, o **SRE Agent Author + SRE Agent Standard User**. ❌ Owner de Azure **no** basta | Portal → recurso del **SRE Agent** → Access control (IAM) |
| **E. Usuarios** | Pedir informes en el chat | **SRE Agent Standard User** | Igual que D |

> **Lo más rápido:** que **una persona** sea a la vez A + B + C (Owner en el Management Group y
> Metastore Admin) y ejecute los dos scripts. Si no es posible, cada persona hace su parte (ver la
> sección *Roles* del `README.md` de cada carpeta).
>
> Quien ejecuta los scripts necesita además:
> - **PowerShell 7** y **Azure CLI**;
> - `az login --tenant <TENANT_ID>`;
> - poder leer la identidad en Entra ID. Por defecto pueden todos los usuarios miembro; si no,
>   el rol de Entra **Directory Readers**.

**Comprobar vuestro propio rol** (debe salir Owner / User Access Administrator / RBAC Administrator
en el Management Group o en las suscripciones):

```powershell
az login --tenant <TENANT_ID>
$me = az ad signed-in-user show --query id -o tsv
az role assignment list --assignee $me --all --include-inherited --include-groups `
    --query "[].{rol:roleDefinitionName, alcance:scope}" -o table
```

### Roles que recibe la identidad del SRE Agent (los dan los scripts, todos de lectura)

| Caso | Roles |
|---|---|
| AKS | **Reader** + **Cost Management Reader** (opcional) en el Management Group o en cada suscripción; **Azure Kubernetes Service Cluster User Role** + **Azure Kubernetes Service RBAC Reader** en cada AKS (o en el mismo alcance con `-AksRolesAtScope`) |
| Databricks | **Reader** en la suscripción; *service principal* en el workspace con `workspace-access` + **CAN_VIEW** en jobs, pipelines y warehouses; **BROWSE / USE_CATALOG / USE_SCHEMA** en Unity Catalog (solo metadatos, nunca `SELECT`) |

**Client ID del agente** (el mismo para AKS y Databricks): sre.azure.com → vuestro agente →
**Settings → Azure settings → Managed Identity → Go to Identity → Overview → Client ID**. Es el que
ya tenéis en la herramienta de AKS actual (empieza por `6431996a-`). **No** es el *Object ID*.

---

## 1. AKS: qué cambiar en lo que ya tenéis (7 cambios)

Ya tenéis creados la herramienta `generate-aks-waf-report`, el skill `aks-waf-live` y el subagente
`aks-waf-live`. La versión actual solo sabe leer **una** suscripción. Para las 30+ suscripciones:

| # | ✏️ Qué cambiar | Dónde | Cómo |
|---|---|---|---|
| 1 | **Permisos** | PowerShell (persona A) | Ejecutar el **nuevo** `grant-aks-permissions.ps1` sobre el Management Group |
| 2 | **Configuración** | El **nuevo** `generate-aks-waf-report.py` de este repositorio | Pegar el fichero `approved-clusters.generated.py` que genera el script |
| 3 | **Código** | Portal → herramienta `generate-aks-waf-report` → **Code** | Borrar todo y pegar el `.py` nuevo |
| 4 | **Parámetro `cluster_name`** | Misma herramienta → parámetros | **Quitar el enum** (texto libre) |
| 5 | **Skill** | Portal → skill `aks-waf-live` | Sustituir por el nuevo `skills/aks-waf-live/SKILL.md` |
| 6 | **Subagente** | Portal → subagente `aks-waf-live` → Instructions | Sustituir por el bloque *Copy-ready instructions* de `subagent-instructions-and-prompts.md` |
| 7 | **Forma de pedirlo** | Chat | Usar los prompts de flota (abajo) |

> ⚠️ **No reutilicéis el `.py` antiguo** añadiendo clústeres a mano: no sabe ir a otra suscripción.

### Cambio 1 · Permisos en las 30+ suscripciones

En PowerShell, dentro de `01-aks-well-architected-review`:

```powershell
az login --tenant <TENANT_ID>
.\grant-aks-permissions.ps1 -IdentityClientId "<CLIENT_ID_SRE_AGENT>" `
    -ManagementGroupId "<ID-del-management-group>" -AksRolesAtScope -IncludeCost
```

- `-ManagementGroupId`: el **ID** del Management Group (Portal → Management groups → columna *ID*),
  **no** el nombre visible. Quien lo ejecuta necesita el rol **en el propio Management Group**.
- Si no usáis Management Group: `-SubscriptionIds "sub1","sub2",...` o `-AllSubscriptions`.
- `-AksRolesAtScope`: los roles de AKS se dan en el Management Group, así **los clústeres nuevos
  quedan cubiertos** sin volver a ejecutar.
- `-IncludeCost`: añade el coste real (FinOps). Quitadlo si no lo queréis.

Al terminar deja **dos ficheros** en la carpeta:

- `approved-clusters.generated.py`: la configuración que se pega en el cambio 2.
- `aks-auth-modes.csv`: qué clústeres permiten leer *workloads*. Los que salen con `AVISO`
  necesitan **Entra ID + Azure RBAC** para ver los workloads. Si no lo tienen, el informe sale
  igual, pero esa parte aparece como «No evaluado».

Si falla en alguna suscripción, sigue con las demás y las lista al final. Los permisos tardan
**5–10 min** en propagarse.

### Cambio 2 · Configurar el `.py` nuevo

Abrid `01-aks-well-architected-review/generate-aks-waf-report.py` con VS Code o Notepad++. Al
principio está el bloque `CONFIGURACION GESTAMP (editar solo este bloque)`:

1. **Borrad** las 5 variables marcadas con `(*)`: `SUBSCRIPTION_ID`, `TENANT_ID`,
   `MANAGED_IDENTITY_CLIENT_ID`, `CLUSTER_DEFAULTS` y `APPROVED_CLUSTERS`.
2. **Pegad** en su lugar el contenido completo de `approved-clusters.generated.py`.
3. **No toquéis** `CLIENT_NAME = "Gestamp"` ni el logo: ya vienen puestos.

> ⚠️ **El error `SyntaxError: invalid syntax` que salió en la prueba anterior** fue por esto: al
> pegar, dos variables quedaron en la **misma línea**
> (`MANAGED_IDENTITY_CLIENT_ID = "..."CLIENT_NAME = "Gestamp"`). Cada variable va en **su propia
> línea**.

### Cambio 3 · Pegar el código nuevo

**sre.azure.com → agente → Builder → Agent Canvas** → herramienta **`generate-aks-waf-report`** →
lápiz (editar) → pestaña **Code** → `Ctrl+A`, borrar y pegar el `.py` del cambio 2.

Comprobad que **Timeout = 900** e **Identity = Off**.

### Cambio 4 · Quitar el enum de `cluster_name`

En los parámetros de la misma herramienta, abrid `cluster_name`:

- Tipo **string**.
- **Borrad la lista de valores permitidos.**
- Valor por defecto vacío.

Con cientos de clústeres no se puede mantener esa lista, y no hace falta: el código rechaza
cualquier nombre que no esté en `APPROVED_CLUSTERS`. Si existe `cluster_names` (plural), dejadlo
también sin enum; si no existe, añadidlo como `string` con valor por defecto vacío.

Después haced las **3 pruebas del playground** (siguiente apartado) y **Save**.

### Cambios 5 y 6 · Skill y subagente

- **Skill:** Table view → **Skills** → `aks-waf-live` → lápiz → sustituid el texto por
  `skills/aks-waf-live/SKILL.md`. Trae la nueva sección *Large fleets*.
- **Subagente:** Table view → **Sub Agents** → `aks-waf-live` → lápiz → en **Instructions**, pegad
  el bloque *Copy-ready instructions* de `subagent-instructions-and-prompts.md`.
- Los dos siguen teniendo **solo** la herramienta `generate-aks-waf-report`.

### ⭐ AKS · Valores del Test playground

**Herramienta → Test playground.** Cada parámetro ya trae un valor por defecto, y casi todos están
bien así. **Solo cambiad lo marcado con ←**.

| Parámetro | Prueba 1 · Configuración | Prueba 2 · Permisos | Prueba 3 · Informe |
|---|---|---|---|
| `operation` | **`list_clusters`** ← | **`query`** ← | `report` |
| `view` | `summary` | **`coverage`** ← | `summary` |
| `cluster_name` | *(vacío)* | **nombre de la prueba 1** ← | **nombre de la prueba 1** ← |
| `cluster_names` | *(vacío)* | *(vacío)* | *(vacío)* |
| `namespace`, `workload_name` | *(vacío)* | *(vacío)* | *(vacío)* |
| `area`, `level`, `severity`, `status` | `all` | `all` | `all` |
| `limit` / `offset` | `25` / `0` (o `100` / `0`) | `25` / `0` | `25` / `0` |
| **Qué debe salir** | `status: ok`, `total` = nº de clústeres, `subscription_count` = nº de suscripciones | Una fila por fuente: `complete` = bien, `denied` = falta rol o propagación | `status: ok` (2–10 min) |

Reglas:

- Nunca dejéis vacíos `operation`, `view`, `area`, `level`, `severity` ni `status`. Si los dejáis
  vacíos, da `must use its declared enum`.
- `limit` y `offset` son números.
- En `report` y `list_clusters`, todos los filtros se quedan por defecto. Si no, da
  `invalid_combination`.
- `cluster_name` o `cluster_names`, nunca los dos.
- `cluster_names`: hasta 5 nombres, sin espacios.
- Si `next_offset` no es `null` en la prueba 1, hay más clústeres: repetid con `offset` = ese número.

### Prompts de flota (chat nuevo)

```
@aks-waf-live Lista todos los clústeres aprobados (paginando hasta el final) y agrúpalos por suscripción.
```
```
@aks-waf-live Revisa la flota en lotes de 5 clústeres con operation=query y view=coverage, y dame un resumen de qué fuentes salen denied o error por suscripción.
```
```
@aks-waf-live Genera la revisión completa de arquitectura y configuración de AKS para <nombre-aks> con operation=report.
```

Guía completa con capturas, opciones y errores:
`01-aks-well-architected-review/README.md`, sección **A. Si ya lo tenéis desplegado**.

---

## 2. Databricks: desplegar y probar

SRE Agent y Databricks están en el **mismo tenant**. **No** hace falta App Registration, secretos
ni Lighthouse: se usa la identidad del agente (el mismo Client ID que en AKS).

| Paso | Quién | Qué |
|---|---|---|
| **0** | Persona B | **Portal → workspace → Launch Workspace** (con Owner o Contributor) para ser **Workspace Admin** |
| **1** | Personas A + B + C (o una que sea las tres) | Ejecutar el script de permisos (abajo) |
| **2** | Persona D | Pegar en `generate-databricks-dependency-report.py`, bloque `CONFIGURACION GESTAMP`, lo que imprime el script. Ya trae nombre y logo |
| **3** | Persona D | Crear la herramienta `generate-databricks-dependency-report` (ver detalle abajo) |
| **4** | Persona D | **Test playground**: 3 pruebas (abajo) |
| **5** | Persona D | Crear el skill `databricks-dependency-live` con `skills/databricks-dependency-live/SKILL.md` |
| **6** | Persona D | Crear el subagente `databricks-dependency-live` con las instrucciones de `subagent-instructions-and-prompts.md` (solo esa herramienta y ese skill) |
| **7** | Persona E | Chat nuevo: `@databricks-dependency-live Genera el informe completo de dependencias y configuración de Databricks para <workspace> con operation=report.` |

**Paso 1:** en PowerShell, dentro de `02-databricks-dependency-review`:

```powershell
az login --tenant <TENANT_ID>
.\grant-databricks-permissions.ps1 -IdentityClientId "<CLIENT_ID_SRE_AGENT>" -SubscriptionId "<SUB_DEL_WORKSPACE>"
```

Para limitarlo a un workspace: `-Workspaces "<rg>/<workspace>"`.

Si no sois Workspace Admin o Metastore Admin, el script avisa con `[AVISO]` y sigue. Esa parte la
hace la persona B o C a mano:

- **Workspace (persona B):** Settings → Identity and access → Service principals → **Add →
  Microsoft Entra ID managed** → Client ID. Después, `CAN_VIEW` en jobs, pipelines y warehouses.
- **Unity Catalog (persona C)**, en SQL:

  ```sql
  GRANT BROWSE, USE CATALOG, USE SCHEMA ON CATALOG <catalogo> TO `<CLIENT_ID_SRE_AGENT>`;
  ```

**Paso 3**, detalle de la herramienta:

| Campo | Valor |
|---|---|
| Code | El `.py` editado en el paso 2 |
| Requirements | El contenido de `requirements.txt` |
| Timeout | `900` |
| Identity | **Off** |
| Parameters | Los 12 de `tool-parameters.schema.json` |

En el enum de `workspace_name`, poned `""` y el nombre exacto del workspace (el script lo imprime).

### ⭐ Databricks · Valores del Test playground

| Parámetro | Prueba 1 · Configuración | Prueba 2 · Permisos | Prueba 3 · Informe |
|---|---|---|---|
| `operation` | **`list_workspaces`** ← | **`query`** ← | `report` |
| `view` | `summary` | **`coverage`** ← | `summary` |
| `workspace_name` | *(vacío)* | **vuestro workspace** ← | **vuestro workspace** ← |
| `workspace_names`, `workload_name` | *(vacío)* | *(vacío)* | *(vacío)* |
| `classification`, `area`, `level`, `severity`, `status` | `all` | `all` | `all` |
| `limit` / `offset` | `25` / `0` | `25` / `0` | `25` / `0` |
| **Qué debe salir** | `status: ok` y el workspace con `auth_mode: same_tenant_uami` | Fuentes `complete`. Si sale `denied`: `arm_…` → persona A · `dbx_…` → persona B · `uc_…` → persona C | `status: ok` (2–10 min) |

> «Function test succeeded» solo significa que el código se ejecutó. Mirad el resultado: si pone
> `configuration_required`, quedan valores `<...>` en el bloque de configuración.

Guía completa con capturas: `02-databricks-dependency-review/README.md`.

---

## Errores más frecuentes

| Error | Causa | Arreglo |
|---|---|---|
| `SyntaxError: invalid syntax` | Dos variables en la misma línea al pegar | Una por línea |
| `configuration_required` | Quedan `<...>` en el bloque de configuración | Pegar el bloque que genera el script |
| `AuthorizationFailed ... roleAssignments/write` | Quien ejecuta el script es Contributor | Owner, User Access Administrator o RBAC Administrator |
| `No encuentro la identidad con Client ID` | Se copió el *Object ID* o hay otro tenant en `az login` | Client ID + `az login --tenant` |
| `must use its declared enum` | Un parámetro vacío en el playground | Ponerle su valor por defecto (`summary` / `all`) |
| `invalid_combination` | Filtros cambiados en `report` o `list_…` | Todo por defecto salvo el nombre |
| `denied` / 403 justo después de dar permisos | Propagación | Esperar 5–10 min |
| No se puede guardar en el Builder | Falta el rol del SRE Agent | SRE Agent Administrator |

---

## Builder v3 (`00-sre-usecase-builder-v3/`), opcional

No necesita permisos de Azure. Ver su `README.md`.

1. **Herramienta** Python `sre-usecase-builder-kit`:
   - Code: `sre-usecase-builder-kit.py`, sin editar.
   - Timeout: `300`.
   - Identity: **Off**.
2. **Skill** `sre-usecase-builder`: pegar `skills/sre-usecase-builder/SKILL.md` y adjuntar esa
   herramienta.
3. **Subagente** `sre-usecase-builder`: instrucciones de `skills/subagent-instructions.md`, con
   solo esa herramienta y ese skill.
