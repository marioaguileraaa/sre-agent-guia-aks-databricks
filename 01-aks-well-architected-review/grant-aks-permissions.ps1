<#
.SYNOPSIS
  Da al SRE Agent los permisos de SOLO LECTURA del caso AKS Well-Architected Review
  en una, varias o todas las suscripciones (o un Management Group) y genera el bloque
  APPROVED_CLUSTERS listo para pegar en generate-aks-waf-report.py.

.DESCRIPTION
  Roles (todos de solo lectura; nunca se usan credenciales admin del cluster):
    - Reader                                          -> alcance (MG o cada suscripcion)
    - Cost Management Reader (opcional, -IncludeCost) -> alcance
    - Azure Kubernetes Service Cluster User Role      -> cada AKS (o el alcance con -AksRolesAtScope)
    - Azure Kubernetes Service RBAC Reader            -> cada AKS (o el alcance con -AksRolesAtScope)

  Modos de alcance (elige uno; si no pones ninguno usa la suscripcion activa de "az login"):
    -ManagementGroupId <mg>     Asigna los roles UNA vez en el Management Group (heredan todas sus
                                suscripciones, tambien las futuras) y recorre sus suscripciones.
    -AllSubscriptions           Todas las suscripciones habilitadas del tenant que ve tu "az login".
    -SubscriptionIds a,b,c      Lista explicita de suscripciones.
    -SubscriptionId <id>        Una sola suscripcion (compatibilidad con la version anterior).

  Salida:
    - Tabla con el modo de autenticacion de cada AKS (Entra ID + Azure RBAC es necesario para leer
      workloads) y CSV aks-auth-modes.csv.
    - Fichero approved-clusters.generated.py con CLUSTER_DEFAULTS + APPROVED_CLUSTERS
      (una linea por cluster, con su subscription_id). Pegalo en el bloque CONFIGURACION del .py.

  Requisitos de quien lo ejecuta: Owner, User Access Administrator o Role Based Access Control
  Administrator en el alcance elegido, y Azure CLI con "az login" en el tenant correcto.
  Volver a ejecutarlo es seguro: las asignaciones existentes no se duplican.

.EXAMPLE
  # Recomendado para muchas suscripciones: un Management Group, roles AKS a nivel de alcance
  .\grant-aks-permissions.ps1 -IdentityClientId "<client-id>" -ManagementGroupId "mg-plataforma" -AksRolesAtScope -IncludeCost

.EXAMPLE
  # Todas las suscripciones que ve tu sesion
  .\grant-aks-permissions.ps1 -IdentityClientId "<client-id>" -AllSubscriptions -AksRolesAtScope

.EXAMPLE
  # Lista explicita de suscripciones, roles AKS cluster a cluster (minimo privilegio)
  .\grant-aks-permissions.ps1 -IdentityClientId "<client-id>" -SubscriptionIds "<sub-1>","<sub-2>"

.EXAMPLE
  # Solo algunos clusters: "suscripcion/resourceGroup/nombre" (o "resourceGroup/nombre" con una sola suscripcion)
  .\grant-aks-permissions.ps1 -IdentityClientId "<client-id>" -Clusters "<sub-1>/rg-aks-pro/aks-pro-01","<sub-2>/rg-aks/aks-pre-01"
#>
param(
    [Parameter(Mandatory)][string]$IdentityClientId,
    [string]$ManagementGroupId,
    [switch]$AllSubscriptions,
    [string[]]$SubscriptionIds,
    [string]$SubscriptionId,
    [string[]]$Clusters,
    [switch]$IncludeCost,
    [switch]$AksRolesAtScope,
    [ValidateSet("UNSPECIFIED", "PRODUCTION", "LAB")][string]$Environment = "UNSPECIFIED",
    [string]$OutFile = "approved-clusters.generated.py"
)
$ErrorActionPreference = "Stop"

$ROLE_READER          = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
$ROLE_AKS_CLUSTERUSER = "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"
$ROLE_AKS_RBACREADER  = "7f6c6a51-bcf8-42ba-9220-52d62157d7db"
$ROLE_COST_READER     = "72fafb9e-0641-4937-9268-a91bfd8191a3"
$MAX_CLUSTERS         = 500
$failures = New-Object System.Collections.Generic.List[string]

function Grant([string]$principalId, [string]$roleId, [string]$roleName, [string]$scope) {
    $null = az role assignment create --assignee-object-id $principalId --assignee-principal-type ServicePrincipal `
        --role $roleId --scope $scope --only-show-errors -o none 2>&1
    if ($LASTEXITCODE -ne 0) {
        $failures.Add("$roleName -> $scope")
        Write-Host "  [ERROR] $roleName  ->  $scope (revisa tus permisos en ese alcance)" -ForegroundColor Red
    } else {
        Write-Host "  [OK] $roleName  ->  $scope" -ForegroundColor Green
    }
}

function Get-MgSubscriptions([string]$mg) {
    $url = "https://management.azure.com/providers/Microsoft.Management/managementGroups/$mg/descendants?api-version=2020-05-01"
    $subs = @()
    while ($url) {
        $page = az rest --method get --url $url -o json | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw "No se pudo leer el Management Group '$mg'. Revisa el nombre (ID, no display name) y tus permisos." }
        $subs += @($page.value | Where-Object { $_.type -eq "Microsoft.Management/managementGroups/subscriptions" -or $_.type -eq "/subscriptions" } | ForEach-Object { $_.name })
        $url = $page.nextLink
    }
    return $subs
}

Write-Host "`n== 1. Tenant, identidad y suscripciones ==" -ForegroundColor Cyan
$tenantId = az account show --query tenantId -o tsv
if (-not $tenantId) { throw "No hay sesion de Azure CLI. Ejecuta primero: az login" }
$principalId = az ad sp show --id $IdentityClientId --query id -o tsv 2>$null
if (-not $principalId) { throw "No se encuentra la identidad con Client ID $IdentityClientId en este tenant." }
Write-Host "  Tenant: $tenantId"
Write-Host "  Identidad SRE Agent: clientId=$IdentityClientId  principalId=$principalId"

$explicitClusters = @{}
if ($Clusters) {
    foreach ($c in $Clusters) {
        $p = $c.Split("/")
        if ($p.Count -eq 3) { $sub = $p[0].ToLower(); $rg = $p[1]; $name = $p[2] }
        elseif ($p.Count -eq 2) {
            $sub = if ($SubscriptionId) { $SubscriptionId } else { az account show --query id -o tsv }
            $sub = $sub.ToLower(); $rg = $p[0]; $name = $p[1]
        } else { throw "Formato incorrecto '$c'. Usa  suscripcion/resourceGroup/nombre  o  resourceGroup/nombre" }
        if (-not $explicitClusters.ContainsKey($sub)) { $explicitClusters[$sub] = @() }
        $explicitClusters[$sub] += "$rg/$name".ToLower()
    }
}

$scopes = @()
if ($ManagementGroupId) {
    $scopes = @("/providers/Microsoft.Management/managementGroups/$ManagementGroupId")
    $subs = @(Get-MgSubscriptions $ManagementGroupId)
} elseif ($AllSubscriptions) {
    $subs = @(az account list --all --query "[?tenantId=='$tenantId' && state=='Enabled'].id" -o tsv)
} elseif ($SubscriptionIds) {
    $subs = @($SubscriptionIds | ForEach-Object { $_.Split(",") } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
} elseif ($explicitClusters.Count -gt 0) {
    $subs = @($explicitClusters.Keys)
} elseif ($SubscriptionId) {
    $subs = @($SubscriptionId)
} else {
    $subs = @(az account show --query id -o tsv)
}
$subs = @($subs | ForEach-Object { $_.ToLower() } | Select-Object -Unique)
if (-not $subs) { throw "No se ha encontrado ninguna suscripcion en el alcance indicado." }
if (-not $scopes) { $scopes = @($subs | ForEach-Object { "/subscriptions/$_" }) }
Write-Host "  Suscripciones en alcance: $($subs.Count)"

Write-Host "`n== 2. Roles de lectura en el alcance ==" -ForegroundColor Cyan
foreach ($scope in $scopes) {
    Grant $principalId $ROLE_READER "Reader" $scope
    if ($IncludeCost) { Grant $principalId $ROLE_COST_READER "Cost Management Reader" $scope }
    if ($AksRolesAtScope) {
        Grant $principalId $ROLE_AKS_CLUSTERUSER "Azure Kubernetes Service Cluster User Role" $scope
        Grant $principalId $ROLE_AKS_RBACREADER  "Azure Kubernetes Service RBAC Reader" $scope
    }
}

Write-Host "`n== 3. Clusters AKS por suscripcion ==" -ForegroundColor Cyan
$found = @()
foreach ($sub in $subs) {
    $json = az aks list --subscription $sub --query "[].{name:name, rg:resourceGroup, id:id, managed:aadProfile.managed, azRbac:aadProfile.enableAzureRbac}" -o json 2>$null
    if ($LASTEXITCODE -ne 0) { $failures.Add("az aks list -> $sub"); Write-Host "  [ERROR] No se pudo listar AKS en $sub" -ForegroundColor Red; continue }
    $list = @($json | ConvertFrom-Json)
    if ($explicitClusters.Count -gt 0) {
        $wanted = $explicitClusters[$sub]
        $list = @($list | Where-Object { $wanted -and $wanted -contains "$($_.rg)/$($_.name)".ToLower() })
    }
    Write-Host "  $sub : $($list.Count) AKS"
    foreach ($aks in $list) {
        $found += [pscustomobject]@{ Subscription = $sub; ResourceGroup = $aks.rg; Name = $aks.name; Id = $aks.id
                                     Managed = [bool]$aks.managed; AzureRbac = [bool]$aks.azRbac }
    }
}
if (-not $found) { throw "No se ha encontrado ningun AKS en el alcance indicado." }
if ($found.Count -gt $MAX_CLUSTERS) { throw "Hay $($found.Count) AKS; el tool admite hasta $MAX_CLUSTERS. Usa -SubscriptionIds o -Clusters para acotar." }

if (-not $AksRolesAtScope) {
    Write-Host "`n== 4. Roles AKS en cada cluster ==" -ForegroundColor Cyan
    foreach ($aks in $found) {
        Write-Host " $($aks.Name)"
        Grant $principalId $ROLE_AKS_CLUSTERUSER "Azure Kubernetes Service Cluster User Role" $aks.Id
        Grant $principalId $ROLE_AKS_RBACREADER  "Azure Kubernetes Service RBAC Reader" $aks.Id
    }
} else {
    Write-Host "`n== 4. Roles AKS ya asignados a nivel de alcance (cubre tambien clusters futuros) ==" -ForegroundColor Cyan
}

Write-Host "`n== 5. Modo de autenticacion de los clusters ==" -ForegroundColor Cyan
$summary = foreach ($aks in $found) {
    $mode = if ($aks.Managed -and $aks.AzureRbac) { "OK: Entra ID + Azure RBAC" }
            elseif ($aks.Managed) { "AVISO: Entra ID con RBAC de Kubernetes -> workloads 'No evaluado'" }
            else { "AVISO: sin Entra ID -> solo parte ARM; workloads 'No evaluado'" }
    [pscustomobject]@{ Suscripcion = $aks.Subscription; ResourceGroup = $aks.ResourceGroup; Cluster = $aks.Name; LecturaKubernetes = $mode }
}
$summary | Format-Table -AutoSize -Wrap
$summary | Export-Csv -Path "aks-auth-modes.csv" -NoTypeInformation -Encoding UTF8
$okCount = @($summary | Where-Object { $_.LecturaKubernetes -like "OK*" }).Count
Write-Host "  $okCount de $($summary.Count) clusters con lectura Kubernetes completa. Detalle: aks-auth-modes.csv"

Write-Host "`n== 6. Bloque de configuracion -> $OutFile ==" -ForegroundColor Cyan
$defaultSub = ($found | Group-Object Subscription | Sort-Object Count -Descending | Select-Object -First 1).Name
$dupes = @($found | Group-Object Name | Where-Object { $_.Count -gt 1 } | ForEach-Object { $_.Name })
$costScope = if ($IncludeCost) { "node_resource_group" } else { "disabled" }
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# Generado por grant-aks-permissions.ps1 el $(Get-Date -Format 'yyyy-MM-dd HH:mm'): $($found.Count) AKS en $(@($found | Select-Object -ExpandProperty Subscription -Unique).Count) suscripciones.")
$lines.Add("# Sustituye en el bloque CONFIGURACION de generate-aks-waf-report.py estas 5 variables.")
$lines.Add("SUBSCRIPTION_ID = `"$defaultSub`"")
$lines.Add("TENANT_ID = `"$tenantId`"")
$lines.Add("MANAGED_IDENTITY_CLIENT_ID = `"$IdentityClientId`"")
$lines.Add("CLUSTER_DEFAULTS = {")
$lines.Add("    `"environment`": `"$Environment`",")
$lines.Add("    `"kubernetes_enabled`": True,")
$lines.Add("    `"include_node_resource_group`": True,")
$lines.Add("    `"advisor_enabled`": True,")
$lines.Add("    `"approved_related_resource_ids`": [],")
$lines.Add("    `"approved_cost_resource_ids`": [],")
$lines.Add("    `"cost_scope`": `"$costScope`",")
$lines.Add("    `"cost_timeframe`": `"month_to_date`",")
$lines.Add("    `"requirements`": {},")
$lines.Add("}")
$lines.Add("APPROVED_CLUSTERS = {")
foreach ($aks in ($found | Sort-Object Subscription, Name)) {
    $key = $aks.Name
    $extra = ""
    if ($dupes -contains $aks.Name) {
        $key = ($aks.Subscription.Substring(0, 8) + "-" + $aks.Name)
        if ($key.Length -gt 63) { $key = $key.Substring(0, 63) }
        $extra = ", `"aks_name`": `"$($aks.Name)`""
    }
    $lines.Add("    `"$key`": {`"resource_group`": `"$($aks.ResourceGroup)`", `"subscription_id`": `"$($aks.Subscription)`"$extra},")
}
$lines.Add("}")
[IO.File]::WriteAllText((Join-Path (Get-Location) $OutFile), ($lines -join "`n") + "`n", (New-Object Text.UTF8Encoding $false))
$lines | Select-Object -First 25 | ForEach-Object { Write-Host $_ }
if ($lines.Count -gt 25) { Write-Host "    ... ($($found.Count) clusters en total, ver $OutFile)" }
if ($dupes) { Write-Host "  Nombres repetidos en varias suscripciones (clave con prefijo de suscripcion + aks_name): $($dupes -join ', ')" -ForegroundColor Yellow }

Write-Host "`nIMPORTANTE: en el tool del portal, el parametro cluster_name debe ser texto libre (SIN enum)." -ForegroundColor Yellow
Write-Host "Los permisos pueden tardar 5-10 minutos en propagarse." -ForegroundColor Yellow
if ($failures.Count -gt 0) {
    Write-Host "`n$($failures.Count) operaciones fallaron:" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
