<#
.SYNOPSIS
  Da permisos de SOLO LECTURA a la identidad del SRE Agent sobre Azure Databricks (mismo tenant).

.DESCRIPTION
  1) Azure (plano ARM): rol Reader en la suscripcion (cubre el RG del workspace y el RG gestionado databricks-rg-*).
  2) Databricks (plano workspace): anade la identidad como service principal del workspace (Entra ID managed)
     y le da CAN_VIEW en jobs, pipelines y SQL warehouses existentes.
  3) Unity Catalog: BROWSE + USE_CATALOG + USE_SCHEMA en los catalogos (solo metadatos, nunca SELECT ni ficheros).
  Al final muestra el bloque de configuracion para pegar en el .py.

  Quien lo ejecuta necesita: Owner o User Access Administrator en la suscripcion + Workspace Admin en Databricks.

.EXAMPLE
  .\grant-databricks-permissions.ps1 -IdentityClientId "PEGAR-AQUI-EL-CLIENT-ID"
#>
param(
    [Parameter(Mandatory = $true)][string]$IdentityClientId,
    [string]$SubscriptionId,
    [string[]]$Workspaces,          # opcional: "rg/nombre-workspace" para limitar
    [switch]$SkipDatabricksGrants,  # solo Azure Reader (sin tocar Databricks)
    [switch]$SkipUnityCatalog
)

$ErrorActionPreference = "Stop"
$DatabricksResource = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
$ReaderRoleId = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
$SkipCatalogs = @("system", "hive_metastore", "samples", "__databricks_internal")

function Write-Step($t) { Write-Host "`n== $t ==" -ForegroundColor Cyan }
function Write-Ok($t) { Write-Host "  [OK] $t" -ForegroundColor Green }
function Write-Warn2($t) { Write-Host "  [AVISO] $t" -ForegroundColor Yellow }

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw "Falta Azure CLI (az). Instalalo y ejecuta 'az login'." }

Write-Step "Contexto"
if ($SubscriptionId) { az account set --subscription $SubscriptionId | Out-Null }
$acct = az account show -o json | ConvertFrom-Json
$SubscriptionId = $acct.id
$TenantId = $acct.tenantId
Write-Ok "Suscripcion: $($acct.name) ($SubscriptionId)"
Write-Ok "Tenant: $TenantId"

$sp = az ad sp show --id $IdentityClientId -o json 2>$null | ConvertFrom-Json
if (-not $sp) { throw "No encuentro la identidad con Client ID $IdentityClientId en este tenant. Revisa el Client ID (sre.azure.com > Settings > Azure settings > Managed Identity > Go to Identity > Overview > Client ID)." }
$PrincipalId = $sp.id
$SpName = $sp.displayName
Write-Ok "Identidad: $SpName (objectId $PrincipalId)"

Write-Step "Workspaces de Databricks"
$all = az resource list --subscription $SubscriptionId --resource-type "Microsoft.Databricks/workspaces" -o json | ConvertFrom-Json
if ($Workspaces) {
    $wanted = $Workspaces | ForEach-Object { $_.ToLower() }
    $all = $all | Where-Object { $wanted -contains ("$($_.resourceGroup)/$($_.name)").ToLower() }
}
if (-not $all) { throw "No se han encontrado workspaces de Databricks en la suscripcion $SubscriptionId." }
$ws = @()
foreach ($w in $all) {
    $full = az resource show --ids $w.id -o json | ConvertFrom-Json
    $ws += [pscustomobject]@{
        Name = $w.name; ResourceGroup = $w.resourceGroup; Host = $full.properties.workspaceUrl
        ManagedRg = $full.properties.managedResourceGroupId; Sku = $full.sku.name
    }
    Write-Ok "$($w.resourceGroup)/$($w.name) -> $($full.properties.workspaceUrl) (SKU $($full.sku.name))"
}
if ($ws.Count -gt 5) { Write-Warn2 "Hay $($ws.Count) workspaces; el tool admite hasta 5. Usa -Workspaces 'rg/nombre' para elegir." }

Write-Step "1/3 Azure: Reader en la suscripcion"
$scope = "/subscriptions/$SubscriptionId"
$exists = az role assignment list --assignee-object-id $PrincipalId --scope $scope --role $ReaderRoleId --query "[0].id" -o tsv 2>$null
if ($exists) { Write-Ok "Reader ya asignado" }
else {
    az role assignment create --assignee-object-id $PrincipalId --assignee-principal-type ServicePrincipal --role $ReaderRoleId --scope $scope -o none
    Write-Ok "Reader asignado en $scope"
}

if (-not $SkipDatabricksGrants) {
    $token = az account get-access-token --resource $DatabricksResource --query accessToken -o tsv
    if (-not $token) { throw "No se pudo obtener token de Databricks con tu usuario." }
    $hdr = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }

    function Invoke-Dbx($wsHost, $method, $path, $body) {
        $uri = "https://$wsHost$path"
        if ($body) { return Invoke-RestMethod -Method $method -Uri $uri -Headers $hdr -Body ($body | ConvertTo-Json -Depth 10) }
        return Invoke-RestMethod -Method $method -Uri $uri -Headers $hdr
    }
    function Grant-View($wsHost, $objType, $id, $label) {
        $acl = @{ access_control_list = @(@{ service_principal_name = $IdentityClientId; permission_level = "CAN_VIEW" }) }
        try { Invoke-Dbx $wsHost "PATCH" "/api/2.0/permissions/$objType/$id" $acl | Out-Null; return $true }
        catch { Write-Warn2 "CAN_VIEW en $label fallo: $($_.Exception.Message)"; return $false }
    }

    foreach ($w in $ws) {
        $h = $w.Host
        Write-Step "2/3 Databricks: $($w.Name) ($h)"
        try {
            $filter = [uri]::EscapeDataString("applicationId eq `"$IdentityClientId`"")
            $found = Invoke-Dbx $h "GET" "/api/2.0/preview/scim/v2/ServicePrincipals?filter=$filter" $null
            if ($found.totalResults -gt 0) { Write-Ok "Service principal ya existe en el workspace" }
            else {
                $body = @{
                    schemas       = @("urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal")
                    applicationId = $IdentityClientId
                    displayName   = $SpName
                    entitlements  = @(@{ value = "workspace-access" })
                }
                Invoke-Dbx $h "POST" "/api/2.0/preview/scim/v2/ServicePrincipals" $body | Out-Null
                Write-Ok "Service principal anadido (Entra ID managed, workspace-access, sin admin)"
            }
        }
        catch {
            Write-Warn2 "No se pudo anadir el service principal: $($_.Exception.Message)"
            Write-Warn2 "Necesitas ser Workspace Admin. Hazlo en el portal (ver README.md) y vuelve a ejecutar."
            continue
        }

        $n = 0; $pt = ""
        do {
            $q = "/api/2.1/jobs/list?limit=100"; if ($pt) { $q += "&page_token=$pt" }
            $r = Invoke-Dbx $h "GET" $q $null
            foreach ($j in @($r.jobs)) { if ($j -and (Grant-View $h "jobs" $j.job_id "job $($j.job_id)")) { $n++ } }
            $pt = $r.next_page_token
        } while ($pt)
        Write-Ok "Jobs con CAN_VIEW: $n"

        $n = 0; $pt = ""
        do {
            $q = "/api/2.0/pipelines?max_results=100"; if ($pt) { $q += "&page_token=$pt" }
            $r = Invoke-Dbx $h "GET" $q $null
            foreach ($p in @($r.statuses)) { if ($p -and (Grant-View $h "pipelines" $p.pipeline_id "pipeline $($p.name)")) { $n++ } }
            $pt = $r.next_page_token
        } while ($pt)
        Write-Ok "Pipelines con CAN_VIEW: $n"

        $n = 0
        $r = Invoke-Dbx $h "GET" "/api/2.0/sql/warehouses" $null
        foreach ($s in @($r.warehouses)) { if ($s -and (Grant-View $h "warehouses" $s.id "warehouse $($s.name)")) { $n++ } }
        Write-Ok "SQL warehouses con CAN_VIEW: $n"

        if (-not $SkipUnityCatalog) {
            Write-Step "3/3 Unity Catalog: $($w.Name)"
            try {
                $cats = @(); $pt = ""
                do {
                    $q = "/api/2.1/unity-catalog/catalogs?max_results=100"; if ($pt) { $q += "&page_token=$pt" }
                    $r = Invoke-Dbx $h "GET" $q $null
                    $cats += @($r.catalogs); $pt = $r.next_page_token
                } while ($pt)
                foreach ($c in $cats) {
                    if (-not $c -or $SkipCatalogs -contains $c.name) { continue }
                    $chg = @{ changes = @(@{ principal = $IdentityClientId; add = @("BROWSE", "USE_CATALOG", "USE_SCHEMA") }) }
                    try {
                        Invoke-Dbx $h "PATCH" "/api/2.1/unity-catalog/permissions/catalog/$([uri]::EscapeDataString($c.name))" $chg | Out-Null
                        Write-Ok "Catalogo $($c.name): BROWSE, USE_CATALOG, USE_SCHEMA"
                    }
                    catch { Write-Warn2 "Catalogo $($c.name): $($_.Exception.Message) (hace falta ser owner del catalogo o metastore admin)" }
                }
            }
            catch { Write-Warn2 "Unity Catalog no disponible en este workspace: $($_.Exception.Message)" }
        }
    }
}

Write-Step "LISTO. Pega esto en el bloque CONFIGURACION GESTAMP del .py"
$cfg = @()
$cfg += "SUBSCRIPTION_ID = `"$SubscriptionId`""
$cfg += "SRE_TENANT_ID = `"$TenantId`""
$cfg += "TARGET_TENANT_ID = `"$TenantId`""
$cfg += "MANAGED_IDENTITY_CLIENT_ID = `"$IdentityClientId`""
$cfg += "FEDERATED_APP_CLIENT_ID = `"`""
$cfg += "KEYVAULT_OAUTH_SECRET_URI = `"`""
$cfg += ""
$cfg += "APPROVED_WORKSPACES = {"
foreach ($w in ($ws | Select-Object -First 5)) {
    $cfg += "    `"$($w.Name)`": {"
    $cfg += "        `"resource_group`": `"$($w.ResourceGroup)`","
    $cfg += "        `"workspace_host`": `"$($w.Host)`","
    $cfg += "        `"environment`": `"PRODUCTION`","
    $cfg += "        `"auth_mode`": `"same_tenant_uami`","
    $cfg += "        `"workspace_api_enabled`": True,"
    $cfg += "        `"unity_catalog_enabled`": True,"
    $cfg += "        `"include_managed_resource_group`": True,"
    $cfg += "        `"advisor_enabled`": True,"
    $cfg += "        `"approved_related_resource_ids`": [],"
    $cfg += "        `"approved_cost_resource_ids`": [],"
    $cfg += "        `"cost_scope`": `"disabled`","
    $cfg += "        `"cost_timeframe`": `"month_to_date`","
    $cfg += "        `"requirements`": {},"
    $cfg += "    },"
}
$cfg += "}"
$cfg | ForEach-Object { Write-Host $_ }

$names = ($ws | Select-Object -First 5 | ForEach-Object { "`"$($_.Name)`"" }) -join ", "
Write-Step "Y en el parametro workspace_name del tool (enum)"
Write-Host "[`"`", $names]"
