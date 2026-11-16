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

## 🔄 Actualización 16/11/2026 — leed esto primero

1. **Descargad de nuevo el repositorio** (GitHub → *Code → Download ZIP*). Los dos `.py` traen una
   corrección imprescindible: sin ella, cualquier lectura de Azure falla con `identity_mismatch`.
   **Si pegasteis el código antes del 16/11/2026, volved a pegarlo** (AKS: cambio 3; Databricks: paso 3).
2. **Databricks: la herramienta se llama ahora `databricks-dependency-report`.** El campo *Tool Name*
   admite 36 caracteres y `generate-databricks-dependency-report` (37) se queda cortado. El fichero
   `.py` mantiene su nombre.
3. **El portal ha cambiado** respecto a las guías y capturas de octubre:

| Guía / capturas de octubre | Portal de hoy |
|---|---|
| *Builder* (o *Build + setup*) | **Build + configure** (menú izquierdo) **→ Agent Canvas** |
| *Settings → Azure settings → …* para el Client ID | Ya no existe. Client ID: línea `MANAGED_IDENTITY_CLIENT_ID = "…"` del código actual de AKS, o portal de Azure → identidad administrada `id-…` del agente → **Overview → Client ID** |
| Campo *Requirements* | No existe. El entorno ya trae azure-identity, PyYAML y Pillow (`requirements.txt` es informativo) |
| Editor de parámetros / enum | No existe. Los 12 parámetros salen solos de `def main(...)` al pegar el código |
| *Test playground* con valores ya escritos | 12 cajas **vacías**: escribid todos los valores |
| *Save* / *Create tool* | En gris hasta pulsar **Test** en el playground (no es un problema de rol) |
| Skill: elegir la herramienta y pegar el `SKILL.md` | **Primero** pegar el `SKILL.md`, **después** *Choose tools* |
| `@aks-waf-live …` en el chat | **/agent** → elegir el subagente → chip **Agent: …** → pegar el prompt |

4. **Kubernetes:** hoy las herramientas Python del SRE Agent no llegan a la API de Kubernetes de AKS
   (HTTP 502 de la plataforma; **no es de permisos**). El informe sale igual, con los workloads
   «No evaluado». Poned `"kubernetes_enabled": False` en `CLUSTER_DEFAULTS`
   ([cambio 2](#cambio-2--configurar-el-py-nuevo)).
5. **Test playground: ~100 s como máximo.** Pasado ese tiempo da HTTP 504, aunque el Timeout sea 900.
   Con `kubernetes_enabled: False` las pruebas de AKS tardan ~15 s; si aun así da 504, lanzad el
   informe desde el chat.

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

**Client ID del agente** (el mismo para AKS y Databricks): es el que ya tenéis en la herramienta de
AKS actual, en la línea `MANAGED_IDENTITY_CLIENT_ID = "…"` de su código (empieza por `6431996a-`).
También está en el portal de Azure → grupo de recursos del agente → identidad administrada `id-…` →
**Overview → Client ID** (en el portal del agente ya no hay *Settings*). Es el **Client ID**, **no**
el *Object ID*.

---

## 1. AKS: qué cambiar en lo que ya tenéis (7 cambios)

Ya tenéis creados la herramienta `generate-aks-waf-report`, el skill `aks-waf-live` y el subagente
`aks-waf-live`. La versión actual solo sabe leer **una** suscripción. Para las 30+ suscripciones:

| # | ✏️ Qué cambiar | Dónde | Cómo |
|---|---|---|---|
| 1 | **Permisos** | PowerShell (persona A) | Ejecutar el **nuevo** `grant-aks-permissions.ps1` sobre el Management Group |
| 2 | **Configuración** | El **nuevo** `generate-aks-waf-report.py` de este repositorio | Pegar el fichero `approved-clusters.generated.py` que genera el script (sin borrar nada) y poner `kubernetes_enabled: False` |
| 3 | **Código** | Portal → herramienta `generate-aks-waf-report` → **Code** | Borrar todo y pegar el `.py` nuevo |
| 4 | **Parámetros** (`cluster_name` sin enum) | Misma herramienta → **Test playground** | **Automático** al pegar el `.py` nuevo: comprobad que salen los 12, con `cluster_name` y `cluster_names` como texto libre |
| 5 | **Skill** | Portal → skill `aks-waf-live` | Pegar el nuevo `skills/aks-waf-live/SKILL.md` y **después** volver a elegir la herramienta |
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
  igual, pero esa parte aparece como «No evaluado». **Hoy (16/11/2026)** los workloads salen
  «No evaluado» en todos los clústeres: la plataforma no deja llegar a la API de Kubernetes (HTTP 502;
  no es de permisos y activar Entra ID + Azure RBAC no lo arregla). Ver el cambio 2.

Si falla en alguna suscripción, sigue con las demás y las lista al final. Los permisos tardan
**5–10 min** en propagarse.

### Cambio 2 · Configurar el `.py` nuevo

Abrid `01-aks-well-architected-review/generate-aks-waf-report.py` con VS Code o Notepad++. Al
principio está el bloque `CONFIGURACION GESTAMP (editar solo este bloque)`:

1. **Pegad** el contenido completo de `approved-clusters.generated.py` **justo encima de la línea
   `# ====…` que cierra el bloque** (la línea 313, justo debajo de `APPROVED_CLUSTERS`), **sin borrar
   nada**. Python usa la última definición: lo pegado sustituye a las 5 variables de ejemplo
   marcadas con `(*)`.
2. **Hoy, recomendado:** en lo que acabáis de pegar, cambiad `"kubernetes_enabled": True` por
   `"kubernetes_enabled": False` (ver el punto 4 de la [actualización](#-actualización-16112026--leed-esto-primero)).
3. **No toquéis** `CLIENT_NAME = "Gestamp"` ni el logo: ya vienen puestos.

Borrar las 5 variables de ejemplo es opcional. Lo que **no** hay que hacer es borrar todo el tramo
entre `SUBSCRIPTION_ID` y `APPROVED_CLUSTERS`: en medio están `CLIENT_NAME` y el logo, y el fichero
falla con `NameError: name 'CLIENT_LOGO_ALT' is not defined`.

> ⚠️ **El error `SyntaxError: invalid syntax` que salió en la prueba anterior** fue por esto: al
> pegar, dos variables quedaron en la **misma línea**
> (`MANAGED_IDENTITY_CLIENT_ID = "..."CLIENT_NAME = "Gestamp"`). Cada variable va en **su propia
> línea**.

### Cambio 3 · Pegar el código nuevo

**sre.azure.com → agente → Build + configure → Agent Canvas → Table view** → tarjeta **Python tools**
(o «…» → *Python tools*) → buscad **`generate-aks-waf-report`** → clic en el nombre → lápiz →
pestaña **Code** → clic en el editor, `Ctrl+A` y pegad el `.py` del cambio 2. (También desde
*Canvas view*: pastilla *Subagent* → `aks-waf-live` → nodo de la herramienta → lápiz.)

Comprobad que **Timeout (seconds) = 900** y que en **Identity**, **Enable Azure Identity = Off**. No lo
encendáis: el portal añade 4 líneas al principio del código (`# Azure Identity Authentication`,
`from azure.identity import DefaultAzureCredential`, `credential = DefaultAzureCredential()` y
`AZURE_ARM_TOKEN = …`) que se quedan aunque lo volváis a apagar. Si pasa, borradlas en **Code**.

### Cambio 4 · Parámetros: automático (`cluster_name` sin enum)

Ya no hay editor de parámetros ni de enum: al pegar el `.py` nuevo, el portal saca los 12
parámetros de `def main(...)`, con `cluster_name` y `cluster_names` como **texto libre**. Solo
comprobad en la pestaña **Test playground** que salen las 12 cajas, incluidas `cluster_name` y
`cluster_names`. No hace falta una lista de nombres: el código rechaza cualquier nombre que no esté
en `APPROVED_CLUSTERS`.

Después haced las **3 pruebas del playground** (siguiente apartado) y **Save** (sigue en gris hasta
que pulsáis **Test**).

### Cambios 5 y 6 · Skill y subagente

- **Skill:** **Agent Canvas → Table view → Skills** → `aks-waf-live` → lápiz. El orden importa:
  1. En el editor *SKILL.md*, `Ctrl+A` y pegad `skills/aks-waf-live/SKILL.md` (trae la nueva sección
     *Large fleets*).
  2. **Después**: **Choose tools** → buscad `generate-aks-waf-report` → desplegad el grupo
     **PythonFunctionTool (n)** → marcad solo esa herramienta (nunca la casilla del grupo) →
     **Close panel**.
  3. Comprobad el chip de la herramienta y que el editor tenga `tools:` → **Save**.

  Al pegar el `SKILL.md` se sustituye su cabecera y la herramienta desaparece: por eso se elige después.
- **Subagente:** **Table view → Sub Agents** → `aks-waf-live` → lápiz → **Edit custom agent** → en
  **Instructions**, `Ctrl+A` y pegad el bloque *Copy-ready instructions* de
  `subagent-instructions-and-prompts.md` → comprobad los chips (skill `aks-waf-live` y herramienta
  `generate-aks-waf-report`) → **Save**. No uséis *Refine with AI* ni *View AI suggestions*.
- Los dos siguen teniendo **solo** la herramienta `generate-aks-waf-report`. En *Table view* la
  columna *Tools* puede decir «Inherits 165 tools · 76 skills»: lo que vale son los chips del formulario.

### ⭐ AKS · Valores del Test playground

**Herramienta → pestaña Test playground → Test** (la pestaña del diálogo de la herramienta, no el
botón *Test playground* de la barra del Agent Canvas). Las 12 cajas salen **vacías** (sin valores ni
desplegables): **escribid todos los valores de la columna**; lo marcado con ← es lo que cambia entre
pruebas.

| Parámetro | Prueba 1 · Configuración | Prueba 2 · Permisos | Prueba 3 · Informe |
|---|---|---|---|
| `operation` | **`list_clusters`** ← | **`query`** ← | `report` |
| `view` | `summary` | **`coverage`** ← | `summary` |
| `cluster_name` | *(vacío)* | **nombre de la prueba 1** ← | **nombre de la prueba 1** ← |
| `cluster_names` | *(vacío)* | *(vacío)* | *(vacío)* |
| `namespace`, `workload_name` | *(vacío)* | *(vacío)* | *(vacío)* |
| `area`, `level`, `severity`, `status` | `all` | `all` | `all` |
| `limit` / `offset` | `25` / `0` | `25` / `0` | `25` / `0` |
| **Qué debe salir** | `status: ok`, `total` = nº de clústeres, `subscription_count` = nº de suscripciones | Una fila por fuente: `complete` = bien, `denied` = falta rol o propagación. Hoy `kube_*` sale `error` (HTTP 502) o `not_configured` con `kubernetes_enabled: False` | `status: ok` o `partial` (`partial` = alguna fuente sin leer, p. ej. Kubernetes) |

Reglas:

- Escribid todos los valores: `operation`, `view`, `area`, `level`, `severity` y `status` nunca
  vacíos. `must use its declared enum` = valor mal escrito en uno de ellos.
- `limit` y `offset` son números (`25` y `0`); con texto da un error de tipo de la plataforma.
- En `report` y `list_clusters`, todos los filtros se quedan por defecto. Si no, da
  `invalid_combination`.
- `cluster_name` o `cluster_names`, nunca los dos.
- `cluster_names`: hasta 5 nombres, sin espacios.
- Si `next_offset` no es `null` en la prueba 1, hay más clústeres: repetid con `offset` = ese número.
  Cada página trae unos 18 aunque `limit` sea mayor (la respuesta tiene un tamaño máximo).
- El playground corta a los **~100 s con HTTP 504**. Con `kubernetes_enabled: False` las pruebas 2 y 3
  tardan ~15 s por clúster; si aun así da 504, lanzad el informe desde el chat.
- **Save** sigue en gris hasta que pulsáis **Test**.

### Prompts de flota (chat nuevo)

**New chat** → **/agent** → elegid `aks-waf-live` → comprobad el chip **Agent: aks-waf-live** →
pegad el prompt. (Escribir `@` ya no abre selector, y `/aks-waf-live` bajo *SKILLS* es el skill, no
el subagente.)

```
Lista todos los clústeres aprobados (paginando hasta el final) y agrúpalos por suscripción.
```
```
Revisa la flota en lotes de 5 clústeres con operation=query y view=coverage, y dame un resumen de qué fuentes salen denied o error por suscripción.
```
```
Genera la revisión completa de arquitectura y configuración de AKS para <nombre-aks> con operation=report.
```

Lotes de 5 con `kubernetes_enabled: False`; si lo dejáis en `True`, pedid lotes de 2.

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
| **2** | Persona D | Pegar lo que imprime el script en `generate-databricks-dependency-report.py`, justo encima de la línea `# === FIN CONFIGURACION GESTAMP ===` del bloque `CONFIGURACION GESTAMP (editar solo esto)`, sin borrar nada. Ya trae nombre y logo |
| **3** | Persona D | Crear la herramienta `databricks-dependency-report` (ver detalle abajo) |
| **4** | Persona D | **Test playground**: 3 pruebas (abajo) |
| **5** | Persona D | Crear el skill `databricks-dependency-live`: pegar `skills/databricks-dependency-live/SKILL.md` y **después** *Choose tools* → `databricks-dependency-report` |
| **6** | Persona D | Crear el subagente `databricks-dependency-live` con las instrucciones de `subagent-instructions-and-prompts.md` (solo esa herramienta y ese skill) |
| **7** | Persona E | Chat nuevo → **/agent** → `databricks-dependency-live` (chip **Agent: …**) → `Genera el informe completo de dependencias y configuración de Databricks para <workspace> con operation=report.` |

**Paso 1:** en PowerShell, dentro de `02-databricks-dependency-review`:

```powershell
az login --tenant <TENANT_ID>
.\grant-databricks-permissions.ps1 -IdentityClientId "<CLIENT_ID_SRE_AGENT>" -SubscriptionId "<SUB_DEL_WORKSPACE>"
```

Para limitarlo a un workspace: `-Workspaces "<rg>/<workspace>"`. Si la identidad ya tiene Reader
(también heredado del Management Group del script de AKS), el script lo detecta y no lo duplica.

Si no sois Workspace Admin o Metastore Admin, el script avisa con `[AVISO]` y sigue. Esa parte la
hace la persona B o C a mano:

- **Workspace (persona B):** Settings → Identity and access → Service principals → **Add →
  Microsoft Entra ID managed** → Client ID. Después, `CAN_VIEW` en jobs, pipelines y warehouses.
- **Unity Catalog (persona C)**, en SQL:

  ```sql
  GRANT BROWSE, USE CATALOG, USE SCHEMA ON CATALOG <catalogo> TO `<CLIENT_ID_SRE_AGENT>`;
  ```

**Paso 3**, detalle de la herramienta (**Build + configure → Agent Canvas → Create ▾ → Tool › →
Python tool**):

| Campo | Valor |
|---|---|
| Tool Name | `databricks-dependency-report` (el campo admite 36 caracteres) |
| Code | El `.py` editado en el paso 2 |
| Timeout (seconds) | `900` |
| Identity | **Off** |
| Parámetros | Automáticos: salen los 12 de `def main(...)` al pegar el código |

No hay campo *Requirements* ni editor de parámetros o enum: la lista «para el enum de
`workspace_name`» que imprime el script ya no hace falta (el código solo acepta los nombres de
`APPROVED_WORKSPACES`). Si ya creasteis la herramienta con el nombre cortado
(`generate-databricks-dependency-repor`), o la volvéis a crear como `databricks-dependency-report`,
o usáis **exactamente** el nombre guardado en el `SKILL.md`, en las instrucciones del subagente y en
*Choose tools*.

### ⭐ Databricks · Valores del Test playground

Como en AKS: 12 cajas **vacías**, escribid todos los valores; `workspace_name` se teclea (no hay
desplegable).

| Parámetro | Prueba 1 · Configuración | Prueba 2 · Permisos | Prueba 3 · Informe |
|---|---|---|---|
| `operation` | **`list_workspaces`** ← | **`query`** ← | `report` |
| `view` | `summary` | **`coverage`** ← | `summary` |
| `workspace_name` | *(vacío)* | **vuestro workspace** ← | **vuestro workspace** ← |
| `workspace_names`, `workload_name` | *(vacío)* | *(vacío)* | *(vacío)* |
| `classification`, `area`, `level`, `severity`, `status` | `all` | `all` | `all` |
| `limit` / `offset` | `25` / `0` | `25` / `0` | `25` / `0` |
| **Qué debe salir** | `status: ok` y el workspace con `auth_mode: same_tenant_uami` | Fuentes `complete`. Si sale `denied`: `arm_…` → persona A · `dbx_…` → persona B · `uc_…` → persona C | `status: ok` o `partial` (`partial` = alguna fuente sin leer) |

> «Function test succeeded» solo significa que el código se ejecutó. Mirad el resultado: si pone
> `configuration_required`, quedan valores `<...>` en el bloque de configuración. Las pruebas 2 y 3
> pueden pasar de los ~100 s del playground (HTTP 504): si pasa, lanzad el informe desde el chat.

Guía completa con capturas: `02-databricks-dependency-review/README.md`.

---

## Errores más frecuentes

| Error | Causa | Arreglo |
|---|---|---|
| `identity_mismatch` | Se está usando un `.py` anterior al 16/11/2026 | Pegad el `.py` actual del repositorio |
| `SyntaxError: invalid syntax` | Dos variables en la misma línea al pegar | Una por línea |
| `NameError: name 'CLIENT_LOGO_ALT' is not defined` | Al pegar se borró el tramo con `CLIENT_NAME` y el logo | Partid del `.py` del repositorio y pegad encima de la línea de cierre, sin borrar nada |
| `configuration_required` | Quedan `<...>` en el bloque de configuración | Pegar el bloque que genera el script |
| `AuthorizationFailed ... roleAssignments/write` | Quien ejecuta el script es Contributor | Owner, User Access Administrator o RBAC Administrator |
| `No encuentro la identidad con Client ID` | Se copió el *Object ID* o hay otro tenant en `az login` | Client ID + `az login --tenant` |
| `must use its declared enum` | Valor mal escrito en `operation`, `view`, `area`, `level`, `severity` o `status` | Escribid el valor exacto de la tabla (`summary`, `all`…) |
| Error de tipo en `limit` / `offset` | Texto en una caja numérica | Son números: `25` y `0` |
| `invalid_combination` | Filtros cambiados en `report` o `list_…` | Todo por defecto salvo el nombre |
| `workspace_name must exactly match the fixed operator allowlist` | El workspace no está en `APPROVED_WORKSPACES` | Copiad el nombre que devuelve la prueba 1 |
| Databricks: `http_error` 404 en `arm_workspace` | Nombre, grupo de recursos o suscripción del workspace mal escritos en `APPROVED_WORKSPACES` | Copiadlos exactos del portal de Azure (paso 2) |
| HTTP 504 en *Test playground* (~100 s) | Límite del playground, no de la herramienta | `kubernetes_enabled: False` (AKS) o lanzad el informe desde el chat |
| Fuentes `kube_*` en `error` (HTTP 502) | Limitación actual de la plataforma, no de permisos | `kubernetes_enabled: False` |
| `azure_cost` con HTTP 429 | Cost Management limita las peticiones seguidas | Esperad unos minutos y repetid |
| `denied` / 403 justo después de dar permisos | Propagación | Esperar 5–10 min |
| *Save* / *Create tool* en gris | Aún no se ha pulsado **Test** en el playground | Primero **Test**; si sigue en gris tras un Test correcto, SRE Agent Administrator |
| Nombre de herramienta cortado (`generate-databricks-dependency-repor`) | *Tool Name* admite 36 caracteres | `databricks-dependency-report` |

---

## Builder v3 (`00-sre-usecase-builder-v3/`), opcional

No necesita permisos de Azure. Ver su `README.md`.

1. **Herramienta** Python `sre-usecase-builder-kit` (**Create ▾ → Tool › → Python tool**):
   - Code: `sre-usecase-builder-kit.py`, sin editar.
   - Timeout: `300`.
   - Identity: **Off**.
2. **Skill** `sre-usecase-builder`: pegar `skills/sre-usecase-builder/SKILL.md` y **después** adjuntar esa
   herramienta.
3. **Subagente** `sre-usecase-builder`: instrucciones de `skills/subagent-instructions.md`, con
   solo esa herramienta y ese skill.
