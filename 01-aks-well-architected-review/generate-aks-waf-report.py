"""Read-only AKS assessment. Build assets with build_tool.py before SRE paste."""

import base64
import binascii
import csv
import hashlib
import io
import json
import math
import os
import re
import ssl
import tempfile
import time
import warnings
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import UUID, uuid4


# ====================== CONFIGURACION (editar solo este bloque) ======================
# Lo mas facil: ejecutar grant-aks-permissions.ps1, abrir el fichero que genera
# (approved-clusters.generated.py) y sustituir aqui las 5 variables marcadas con (*):
# SUBSCRIPTION_ID, TENANT_ID, MANAGED_IDENTITY_CLIENT_ID, CLUSTER_DEFAULTS y APPROVED_CLUSTERS.
SUBSCRIPTION_ID = "<SUBSCRIPTION_ID>"                 # (*) suscripcion por defecto (la de los clusters sin "subscription_id")
TENANT_ID = "<TENANT_ID>"                             # (*) tenant; TODAS las suscripciones deben estar en este tenant
MANAGED_IDENTITY_CLIENT_ID = "<SRE_AGENT_CLIENT_ID>"  # (*) Client ID (no Object ID) de la identidad del SRE Agent
CLIENT_NAME = "Mi empresa"                          # nombre que aparece en el informe
CLIENT_LOGO_DATA_URI = ""                              # opcional: logo PNG/JPEG en data URI base64
CLIENT_LOGO_ALT = "Logotipo del cliente"
# (*) Valores comunes a TODOS los clusters. Cualquier cluster puede sobreescribir uno concreto
#     poniendolo en su propia entrada (por ejemplo "environment": "PRODUCTION").
CLUSTER_DEFAULTS = {
    "environment": "UNSPECIFIED",          # LAB / PRODUCTION / UNSPECIFIED (exactamente asi)
    "kubernetes_enabled": True,            # lee workloads (requiere AKS con Entra ID + Azure RBAC)
    "include_node_resource_group": True,   # inventario del RG de nodos (MC_...)
    "advisor_enabled": True,               # recomendaciones de Azure Advisor
    "approved_related_resource_ids": [],
    "approved_cost_resource_ids": [],
    "cost_scope": "disabled",              # "node_resource_group" si se dio Cost Management Reader (-IncludeCost)
    "cost_timeframe": "month_to_date",
    "requirements": {},
}
# (*) Clusters aprobados: hasta 500, de cualquier suscripcion del tenant. Una linea por cluster:
#     "resource_group"  obligatorio
#     "subscription_id" solo si el cluster NO esta en SUBSCRIPTION_ID
#     "aks_name"        solo si la clave es un alias (p. ej. dos AKS con el mismo nombre en suscripciones distintas)
APPROVED_CLUSTERS = {
    "<NOMBRE_AKS_1>": {"resource_group": "<RESOURCE_GROUP_AKS_1>"},
    # "<NOMBRE_AKS_2>": {"resource_group": "<RESOURCE_GROUP_AKS_2>", "subscription_id": "<OTRA_SUSCRIPCION>"},
    # "<NOMBRE_AKS_3>": {"resource_group": "<RESOURCE_GROUP_AKS_3>", "subscription_id": "<OTRA_SUSCRIPCION>", "environment": "PRODUCTION"},
}
# ============================================================================================

def validate_logo(uri, alt=CLIENT_LOGO_ALT):
    if uri == "":
        return
    if not isinstance(uri, str) or len(uri) > 140000:
        raise EvidenceError("invalid_logo", "Optional logo must be a bounded inline PNG/JPEG, not a path or URL.", "branding")
    match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", uri)
    if not match:
        raise EvidenceError("invalid_logo", "Only inline PNG/JPEG MIME with canonical base64 is allowed.", "branding")
    try:
        data = base64.b64decode(match[2], validate=True)
    except binascii.Error as exc:
        raise EvidenceError("invalid_logo", "Invalid logo base64.", "branding") from exc
    if not 1 <= len(data) <= 100000:
        raise EvidenceError("invalid_logo", "Optional raster logo must contain 1..100000 bytes.", "branding")
    if not isinstance(alt, str) or not alt.strip() or len(alt) > 120 or re.search(r"[\x00-\x1f\x7f]", alt):
        raise EvidenceError("invalid_logo", "Provide 1..120 plain characters of descriptive logo alt text.", "branding")
    try:
        from PIL import Image, ImageFile, UnidentifiedImageError
    except ImportError as exc:
        raise EvidenceError("dependency_missing", "A configured logo requires Pillow for genuine bounded raster validation.", "branding") from exc
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        raise EvidenceError("unsafe_decoder_settings", "Truncated-image decoding must not be enabled; no signature-only fallback.", "branding")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=["PNG", "JPEG"]) as image:
                width, height = image.size
                if (
                    image.format != ("PNG" if match[1] == "png" else "JPEG")
                    or not 1 <= width <= 2048 or not 1 <= height <= 2048
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise EvidenceError("invalid_logo", "Use one PNG/JPEG frame, matching MIME, at most 2048 by 2048 pixels.", "branding")
                image.verify()
            with Image.open(io.BytesIO(data), formats=["PNG", "JPEG"]) as image:
                image.load()
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise EvidenceError("invalid_logo", "The optional logo is not a complete, valid single-frame PNG/JPEG raster.", "branding") from exc


def response_bytes(value):
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def ensure_response(value):
    if response_bytes(value) > TRANSPORT_BYTES:
        raise EvidenceError("transport_size_exceeded", "Default-serialized response exceeds 3500 bytes; no malformed/truncated JSON is returned.", "transport")
    return value


def collection_disclosure(snapshot):
    if snapshot.get("schema_version") == "aks-waf-multi-cluster-v1":
        sources = [source for run in snapshot["cluster_runs"] for source in run_sources(run)]
        return {"reference_utc": snapshot["reference_utc"], "fresh_collection": True,
                "provenance": snapshot["provenance"], "selected_cluster_names": snapshot["selected_cluster_names"],
                "cluster_run_status_counts": snapshot["aggregate"]["cluster_status_counts"],
                "source_status_counts": dict(Counter(source["status"] for source in sources)),
                "source_total": len(sources), "source_details_view": "coverage",
                "paging_basis": "Local offset in this collection; later calls recollect, not a stable cross-call cursor."}
    return {"reference_utc": snapshot["reference_utc"], "fresh_collection": True, "provenance": snapshot["provenance"],
            "environment": snapshot["environment"], "source_status_counts": dict(Counter(s["status"] for s in snapshot["sources"])),
            "source_total": len(snapshot["sources"]), "source_details_view": "coverage",
            "paging_basis": "Local offset in this collection; later calls recollect, not a stable cross-call cursor."}


def summary(snapshot):
    return {"cluster_id": snapshot["cluster"]["id"], "cluster_name": snapshot["cluster"]["name"], "global": snapshot["scores"]["global"],
            "areas": snapshot["scores"]["areas"], "pillars": snapshot["scores"]["pillars"],
            "inventory_counts": {key: len(snapshot["inventory"].get(key) or []) for key in ("node_pools", "namespaces", "workloads", "related_resources")},
            "findings": len(snapshot["findings"]), "critical": sum(f["severity"] == "critical" for f in snapshot["findings"]),
            "catalog_coverage": snapshot["methodology"]["catalog_coverage"], "official_waf_score": False,
            "actual_cost": snapshot["finops"]["actual_cost"], "currency": snapshot["finops"]["currency"], "estimated_savings": None}


def snapshot_rows(snapshot, view):
    inventory = snapshot["inventory"]
    return {
        "summary": [summary(snapshot)], "cluster": [snapshot["cluster"]],
        "node_pools": inventory.get("node_pools", []), "workloads": inventory.get("workloads", []),
        "resources": inventory.get("related_resources", []) + [
            row for key in ("services", "storage_classes", "pvcs", "pvs") for row in inventory.get(key, [])],
        "findings": snapshot["checks"], "coverage": snapshot["sources"], "actions": snapshot["actions"],
        "finops": [{**row, "record_type": "candidate"} for row in snapshot["finops"]["candidates"]]
        + [{**row, "record_type": "cost"} for row in snapshot["finops"]["cost_rows"]]
        + [{**row, "record_type": "metric"} for row in snapshot["finops"]["metrics"]],
        "architecture": [{**row, "record_type": record_type}
                         for record_type, key in (("node", "nodes"), ("edge", "edges"), ("unknown", "unknowns"))
                         for row in snapshot["architecture"][key]],
    }[view]


def run_sources(run):
    if run["snapshot"] is not None:
        return run["snapshot"]["sources"]
    return [source_receipt("arm_cluster", run["reference_utc"],
                           "denied" if run["error"].get("http_status") in (401, 403) else "error",
                           "Primary collection failed; this cluster has no snapshot or inferred healthy-zero inventory.",
                           errors=[run["error"]])]


def successful_snapshots(document):
    return [run["snapshot"] for run in document["cluster_runs"] if run["snapshot"] is not None] if (
        document.get("schema_version") == "aks-waf-multi-cluster-v1") else [document]


def query_snapshot(snapshot, params):
    view = params["view"]
    if snapshot.get("schema_version") == "aks-waf-multi-cluster-v1":
        rows = []
        for run in snapshot["cluster_runs"]:
            scope = {"cluster_id": run["cluster_id"], "cluster_name": run["cluster_name"]}
            if run["snapshot"] is not None:
                rows.extend({**row, **scope} for row in snapshot_rows(run["snapshot"], view))
            elif view == "coverage":
                rows.extend({**row, **scope} for row in run_sources(run))
            elif view == "summary":
                rows.append({**scope, "status": "error", "global": None, "inventory_counts": None,
                             "error": run["error"], "reference_utc": run["reference_utc"]})
    else:
        rows = snapshot_rows(snapshot, view)
    def matches(row):
        for field in ("namespace", "workload_name"):
            if params[field] and row.get(field) != params[field]:
                return False
        for field in ("area", "level", "severity", "status"):
            if params[field] != "all" and row.get(field) != params[field]:
                return False
        return True
    matched = [row for row in rows if matches(row)]
    selected = matched[params["offset"]:params["offset"] + params["limit"]]
    partial = snapshot.get("schema_version") == "aks-waf-multi-cluster-v1" and any(
        run["status"] != "ok" for run in snapshot["cluster_runs"])
    result = {"status": "partial" if partial else "ok", "operation": "query", "view": view, "collection": collection_disclosure(snapshot),
              "total_matched": len(matched), "offset": params["offset"], "requested_limit": params["limit"],
              "returned": len(selected), "has_more": params["offset"] + len(selected) < len(matched),
              "next_offset": params["offset"] + len(selected) if params["offset"] + len(selected) < len(matched) else None,
              "transport_capped": False, "rows": selected}
    while selected and response_bytes(result) > TRANSPORT_BYTES:
        selected = selected[:-1]
        result.update(rows=selected, returned=len(selected), transport_capped=True,
                      has_more=params["offset"] + len(selected) < len(matched),
                      next_offset=params["offset"] + len(selected) if selected and params["offset"] + len(selected) < len(matched) else None)
    if not selected and params["offset"] < len(matched):
        return ensure_response({"status": "error", "operation": "query", "view": view,
                                "error": {"code": "query_row_too_large", "message": "One complete row cannot fit 3500 default-serialized bytes. Use the full report JSON; no zero-row continuation."},
                                "total_matched": len(matched), "offset": params["offset"], "requested_limit": params["limit"],
                                "returned": 0, "has_more": True, "next_offset": None, "transport_capped": True,
                                "collection": collection_disclosure(snapshot)})
    return ensure_response(result)


def safe_json(snapshot):
    document = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    escaped = document.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if json.loads(escaped) != snapshot:
        raise EvidenceError("round_trip_failed", "Complete embedded normalized JSON did not round-trip.", "renderer")
    return escaped


def render_html(snapshot):
    if HTML_TEMPLATE.count("__REPORT_DATA_JSON__") != 1:
        raise EvidenceError("template_missing", "Build the approved template into the canonical standalone tool.", "renderer")
    validate_logo(snapshot["client"]["logo_data_uri"], snapshot["client"].get("logo_alt", CLIENT_LOGO_ALT))
    return HTML_TEMPLATE.replace("__REPORT_DATA_JSON__", safe_json(snapshot))


def csv_cell(value):
    if value is None:
        return ""
    value = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) if isinstance(value, (dict, list)) else str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value


def csv_document(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([csv_cell(row.get(field)) for field in fields])
    return stream.getvalue()


def inventory_csv_rows(snapshot):
    if snapshot.get("schema_version") == "aks-waf-multi-cluster-v1":
        rows = [row for item in successful_snapshots(snapshot) for row in inventory_csv_rows(item)]
        rows.extend({"collection": "cluster_collection_error", "cluster_id": run["cluster_id"],
                     "cluster_name": run["cluster_name"], "resource_id": run["cluster_id"],
                     "source_id": run["error"]["source"], "observed_at": run["reference_utc"], "details_json": run}
                    for run in snapshot["cluster_runs"] if run["snapshot"] is None)
        return rows
    rows = [{"collection": "cluster", **snapshot["cluster"], "details_json": snapshot["cluster"]}]
    aggregate_keys = {"deployments", "statefulsets", "daemonsets", "jobs", "cronjobs"}
    for collection, records in snapshot["inventory"].items():
        if collection in aggregate_keys or records is None:
            continue
        for record in records:
            rows.append({"collection": collection, **record, "details_json": record})
    return rows


def artifact_directory():
    try:
        cwd = Path.cwd()
        root = cwd.resolve(strict=True)
        linked = any(part.is_symlink() or getattr(part, "is_junction", lambda: False)() for part in (cwd, *cwd.parents))
    except (OSError, RuntimeError) as exc:
        raise EvidenceError("unsafe_artifact_root", "Runtime cwd must resolve to an existing directory.", "artifact_storage") from exc
    if root == Path(root.anchor) or not root.is_dir() or linked:
        raise EvidenceError("unsafe_artifact_root", "Filesystem roots, symlink/junction cwd ancestors and non-directories are forbidden.", "artifact_storage")
    if OUTPUT_DIRECTORY != "aks-waf-reports":
        raise EvidenceError("unsafe_artifact_directory", "Only the fixed aks-waf-reports child is allowed.", "artifact_storage")
    directory = root / OUTPUT_DIRECTORY
    if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)() or directory.resolve() != directory:
        raise EvidenceError("unsafe_artifact_directory", "Output directory is a symlink/junction or escapes cwd.", "artifact_storage")
    return root, directory


def sharing_fields(root, paths):
    result = {"sharing_mode": "local_files_only", "http_verified": False}
    match = re.fullmatch(
        r"/(?:root/sreagent|usr)/terminalRoot/tmp/ThreadFiles/([0-9a-fA-F-]{36})", root.as_posix()
    )
    if match is None:
        return result
    try:
        if str(UUID(match[1])) != match[1].lower():
            return result
    except ValueError:
        return result
    suffixes = {
        "report_path": ".html", "snapshot_path": "-snapshot.json",
        "findings_csv_path": "-findings.csv", "inventory_csv_path": "-inventory.csv",
    }
    if set(paths) != set(suffixes):
        raise EvidenceError("unsafe_sharing_path", "Native delivery requires exactly the four guarded report artifacts.", "artifact_storage")
    stem = paths["report_path"].name.removesuffix(".html")
    if not re.fullmatch(r"(?:aks|simulated)-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}", stem):
        raise EvidenceError("unsafe_sharing_path", "Native aliases require this tool's generated run filenames.", "artifact_storage")
    expected = root / "aks-waf-reports"
    aliases = {}
    for key, path in paths.items():
        if path.parent != expected or path.name != stem + suffixes[key]:
            raise EvidenceError("unsafe_sharing_path", "Native aliases must reference the same run in the fixed report directory.", "artifact_storage")
        aliases[key.removesuffix("_path") + "_url"] = (
            "/api/files/tmp/ThreadFiles/" + match[1] + "/aks-waf-reports/" + quote(path.name, safe="")
        )
    return {**result, "sharing_mode": "native_ui", **aliases}


def report_envelope(snapshot, paths, root):
    items = successful_snapshots(snapshot)
    findings = [row for item in items for row in item["findings"]]
    multi = snapshot.get("schema_version") == "aks-waf-multi-cluster-v1"
    aggregate = snapshot["aggregate"] if multi else None
    partial = any(s["status"] not in ("complete", "not_applicable") for item in items for s in item["sources"])
    result = {"status": "partial" if partial or (multi and aggregate["cluster_status_counts"].get("error")) else "ok",
              "operation": "report", "artifact_status": "written", **sharing_fields(root, paths),
              "collection": collection_disclosure(snapshot),
              "global": aggregate["scores"]["global"] if multi else snapshot["scores"]["global"], "findings_total": len(findings),
              "inventory_counts": aggregate["inventory_counts"] if multi else {
                  key: len(snapshot["inventory"].get(key) or []) for key in ("node_pools", "namespaces", "workloads", "related_resources")},
              "critical_total": sum(row["severity"] == "critical" for row in findings),
              "estimated_savings": None, **{key: str(path) for key, path in paths.items()}}
    return ensure_response(result)


def write_report(snapshot):
    root, directory = artifact_directory()
    reference = snapshot["reference_utc"].replace(":", "").replace("-", "")
    prefix = ("simulated-" if snapshot["provenance"] == "SIMULATED" else "aks-") + reference + "-" + uuid4().hex[:12]
    paths = {key: directory / (prefix + suffix) for key, suffix in (
        ("report_path", ".html"), ("snapshot_path", "-snapshot.json"),
        ("findings_csv_path", "-findings.csv"), ("inventory_csv_path", "-inventory.csv"))}
    response = report_envelope(snapshot, paths, root)
    snapshot_document = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if json.loads(snapshot_document) != snapshot:
        raise EvidenceError("round_trip_failed", "Full normalized export does not round-trip.", "artifact_storage")
    documents = {
        "report_path": render_html(snapshot), "snapshot_path": snapshot_document,
        "findings_csv_path": csv_document([{**row, "details_json": row} for item in successful_snapshots(snapshot) for row in item["findings"]],
            ("check_id", "checklist_id", "title", "status", "level", "area", "pillar", "severity", "resource_id",
             "cluster_id", "cluster_name", "namespace", "workload_name", "effort", "reason", "recommendation", "learn_url", "details_json")),
        "inventory_csv_path": csv_document(inventory_csv_rows(snapshot),
            ("collection", "resource_id", "cluster_id", "cluster_name", "name", "namespace", "kind", "source_id", "observed_at", "details_json")),
    }
    encoded = {key: value.encode("utf-8-sig" if key.endswith("csv_path") else "utf-8") for key, value in documents.items()}
    if any(len(value) > ARTIFACT_BYTES for value in encoded.values()):
        raise EvidenceError("artifact_size_exceeded", "Complete artifacts exceed 4800000 bytes; no records, sources or time windows were dropped and no files were written.", "artifact_storage")
    temporary, written = [], []
    try:
        directory.mkdir(exist_ok=True)
        if artifact_directory()[1] != directory or directory.resolve(strict=True) != directory or not directory.is_dir():
            raise EvidenceError("unsafe_artifact_directory", "Output directory changed after creation.", "artifact_storage")
        for key, data in encoded.items():
            if paths[key].exists() or paths[key].is_symlink():
                raise EvidenceError("artifact_collision", "A unique output path already exists; no overwrite.", "artifact_storage")
            fd, filename = tempfile.mkstemp(prefix=prefix + "-", suffix=".tmp", dir=directory)
            temp_path = Path(filename)
            temporary.append(temp_path)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, paths[key])
            temporary.remove(temp_path)
            written.append(paths[key])
        if any(path.read_bytes() != encoded[key] for key, path in paths.items()):
            raise EvidenceError("artifact_readback_failed", "Written artifact bytes differ from complete prepared content.", "artifact_storage")
        if json.loads(paths["snapshot_path"].read_text(encoding="utf-8")) != snapshot:
            raise EvidenceError("artifact_readback_failed", "Written full normalized evidence differs from source-normalized data.", "artifact_storage")
    except (OSError, EvidenceError) as exc:
        failures = []
        for path in temporary + written:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failures.append(path.name)
        if failures:
            raise EvidenceError("artifact_cleanup_failed", "Artifact failure and cleanup failure; inspect these generated files in the fixed output directory: "
                                + ", ".join(failures), "artifact_storage") from exc
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError("artifact_write_failed", "Could not atomically write/read back all four files; this run's partial files were removed.", "artifact_storage") from exc
    return response


def main(operation: str = "report", view: str = "summary", cluster_name: str = "", namespace: str = "",
         workload_name: str = "", area: str = "all", level: str = "all", severity: str = "all",
         status: str = "all", limit: int = 25, offset: int = 0, cluster_names: str = "") -> dict:
    """Collect one or several fixed approved clusters; never change source scope or identity."""
    try:
        params = validate_parameters(operation, view, cluster_name, namespace, workload_name, area, level, severity, status, limit, offset, cluster_names)
        configs, configured = selection_configs(cluster_name, cluster_names)
        if operation == "list_clusters":
            return list_clusters_page(configured, limit, offset)
        selected = {config["name"] for config in configs}
        roster = [roster_row(config) for config in configured if len(configured) <= 5 or config["name"] in selected]
        if not CATALOG.get("rules"):
            raise EvidenceError("catalog_missing", "Use the build-generated whole-file tool, not unbuilt engine.py.", "rule_catalog")
        reference = utc_now()
        if len(configs) == 1:
            config = configs[0]
            raw = collect_live(config, reference)
            snapshot = finish_snapshot(raw, reference, environment=config["environment"])
            snapshot.update(available_clusters=roster, selected_cluster_names=[config["name"]])
        else:
            runs = []
            deadline = time.monotonic() + MAX_COLLECTION_SECONDS
            for config in configs:
                run_reference = utc_now()
                run = {"cluster_id": config["id"], "cluster_name": config["name"], "reference_utc": run_reference,
                       "snapshot": None, "error": None, "status": "error"}
                try:
                    if time.monotonic() >= deadline:
                        raise EvidenceError("collection_deadline", "Shared multi-cluster time bound reached; this cluster was not collected, not empty/healthy.", "collection")
                    raw = collect_live(config, run_reference, deadline=deadline)
                    run["snapshot"] = finish_snapshot(raw, run_reference, environment=config["environment"])
                    run["status"] = "partial" if any(
                        source["status"] not in ("complete", "not_applicable") for source in run["snapshot"]["sources"]) else "ok"
                except EvidenceError as exc:
                    run["error"] = exc.public()
                runs.append(run)
            snapshot = multi_snapshot(runs, reference, roster)
            if not successful_snapshots(snapshot):
                return ensure_response({"status": "error", "error": {
                    "code": "all_clusters_failed", "source": "arm_cluster",
                    "message": "No selected cluster produced a snapshot; no empty/healthy report or files.",
                }, "artifact_status": "not_written", "cluster_errors": [
                    {"cluster_name": run["cluster_name"], "error": {
                        key: run["error"].get(key) for key in ("code", "source", "http_status")
                    }} for run in runs], "provenance": "LIVE"})
        return query_snapshot(snapshot, params) if operation == "query" else write_report(snapshot)
    except EvidenceError as exc:
        return ensure_response({"status": "error", "error": exc.public(),
                                "artifact_status": "cleanup_failed" if exc.code == "artifact_cleanup_failed" else "not_written",
                                "provenance": "LIVE"})


ARM_ORIGIN = "https://management.azure.com"
AKS_AUDIENCE = "6dae42f8-4368-4678-94ff-3960e28e3630"
AKS_API = "2025-05-01"
TRANSPORT_BYTES = 3500
ARTIFACT_BYTES = 4_800_000
HTTP_BYTES = 8_000_000
MAX_PAGES = 100
MAX_ROWS = 20000
MAX_RELATED_RESOURCES = 200
MAX_APPROVED_CLUSTERS = 500
MAX_REPORT_CLUSTERS = 5
ACTIVE_SUBSCRIPTION_ID = None
MAX_COLLECTION_SECONDS = 240
MAX_HTTP_ATTEMPTS = 300
KUBE_PAGE_SIZE = 500
OUTPUT_DIRECTORY = "aks-waf-reports"
AREAS = ("best_practices", "security", "finops", "optimization")
PILLARS = ("reliability", "security", "cost", "operations", "performance")
LEVELS = ("cluster", "workload", "resource")
SEVERITIES = ("critical", "high", "medium", "low")
CHECK_STATUSES = ("passed", "failed", "not_evaluated", "not_applicable")
SOURCE_STATUSES = ("complete", "partial", "denied", "error", "not_configured", "not_applicable")
VIEWS = ("summary", "cluster", "node_pools", "workloads", "resources", "findings", "finops", "coverage", "actions", "architecture")
SYSTEM_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease", "gatekeeper-system", "azure-arc")
KUBE_PATHS = {
    "namespaces": "/api/v1/namespaces",
    "nodes": "/api/v1/nodes",
    "pods": "/api/v1/pods",
    "services": "/api/v1/services",
    "pvcs": "/api/v1/persistentvolumeclaims",
    "pvs": "/api/v1/persistentvolumes",
    "resource_quotas": "/api/v1/resourcequotas",
    "deployments": "/apis/apps/v1/deployments",
    "statefulsets": "/apis/apps/v1/statefulsets",
    "daemonsets": "/apis/apps/v1/daemonsets",
    "replicasets": "/apis/apps/v1/replicasets",
    "jobs": "/apis/batch/v1/jobs",
    "cronjobs": "/apis/batch/v1/cronjobs",
    "pdbs": "/apis/policy/v1/poddisruptionbudgets",
    "hpas": "/apis/autoscaling/v2/horizontalpodautoscalers",
    "network_policies": "/apis/networking.k8s.io/v1/networkpolicies",
    "storage_classes": "/apis/storage.k8s.io/v1/storageclasses",
    "node_metrics": "/apis/metrics.k8s.io/v1beta1/nodes",
    "pod_metrics": "/apis/metrics.k8s.io/v1beta1/pods",
}
RESOURCE_APIS = {
    "microsoft.compute/disks": "2024-03-02",
    "microsoft.compute/virtualmachinescalesets": "2024-03-01",
    "microsoft.network/loadbalancers": "2024-05-01",
    "microsoft.network/publicipaddresses": "2024-05-01",
    "microsoft.network/networkinterfaces": "2024-05-01",
    "microsoft.network/virtualnetworks": "2024-05-01",
    "microsoft.network/virtualnetworks/subnets": "2024-05-01",
    "microsoft.network/networksecuritygroups": "2024-05-01",
    "microsoft.network/privatednszones": "2020-06-01",
    "microsoft.network/dnszones": "2018-05-01",
    "microsoft.storage/storageaccounts": "2023-05-01",
    "microsoft.containerregistry/registries": "2023-07-01",
    "microsoft.keyvault/vaults": "2023-07-01",
    "microsoft.managedidentity/userassignedidentities": "2023-01-31",
}

# BEGIN BUILD-TIME ASSETS
CATALOG = json.loads((
    '{"catalogSnapshot": {"name": "Azure AKS Review", "repository": "https://github.com/Azure/review-chec'
    'klists", "url": "https://raw.githubusercontent.com/Azure/review-checklists/f131b93418892102b00c247fd'
    '9cf685e3d356522/checklists/aks_checklist.en.json", "commit": "f131b93418892102b00c247fd9cf685e3d3565'
    '22", "blob_sha": "8c9d49dedf2c8f0aa0e11fba86c7a17aa927dc22", "sha256": "e1f7e0f44e121a4f96a9258e6438'
    '3d33a14c78e56bf0d7c7b6638f8b72acff31", "bytes": 66436, "item_count": 112, "upstream_metadata_timesta'
    'mp": "February 20, 2024", "retrieved_at": "2026-10-13", "license": "MIT", "copyright": "Copyright (c'
    ') Microsoft Corporation."}, "guide": {"url": "https://learn.microsoft.com/en-us/azure/well-architect'
    'ed/service-guides/azure-kubernetes-service", "commit": "5d6ba52923afc503730f1a61cb201269060d6ecb", "'
    'updated_at": "2026-09-29T17:03:00Z", "retrieved_at": "2026-10-13"}, "assessment_url": "https://learn'
    '.microsoft.com/en-us/assessments/azure-architecture-review/", "interpretation": ["Local rule-resourc'
    'e indicators, not an official Azure WAF assessment, certification, Polaris or WARA grade.", "Checkli'
    'st IDs and GUIDs are references. Legacy graph snippets are never executed.", "Standard/Premium and A'
    'KS Automatic/NAP replace obsolete Paid-tier and universal fixed-pool assumptions.", "Azure CNI Overl'
    'ay/Cilium is supported; a legacy default maxPods=30 is not a universal density limit.", "Absent IaC,'
    ' requirements, DR tests, licensing or telemetry is not evidence of drift, failure or health.", "Conf'
    'iguration presence is distinct from effective enforcement, source health, entitlement and applicatio'
    'n availability."], "rules": [{"rule_id": "C01", "checklist_id": "05.01.01", "test": "managed_identit'
    'y", "level": "cluster", "area": "security", "pillar": "security", "severity": "medium", "effort": "m'
    'edium", "title": "Identidad administrada del cluster", "recommendation": "Revisar la migracion a ide'
    'ntidad administrada con el propietario; no rotar credenciales desde este informe.", "business_impact'
    '": "Una identidad basada en secretos introduce gestion y riesgo de caducidad; no se ha medido un inc'
    'idente."}, {"rule_id": "C02", "checklist_id": "05.01.02", "test": "managed_entra", "level": "cluster'
    '", "area": "security", "pillar": "security", "severity": "high", "effort": "medium", "title": "Integ'
    'racion administrada con Microsoft Entra", "recommendation": "Validar una integracion administrada de'
    ' Microsoft Entra y sus requisitos antes de cualquier cambio.", "business_impact": "La autenticacion '
    'no centralizada puede dificultar el control y la revocacion de acceso."}, {"rule_id": "C03", "checkl'
    'ist_id": "05.01.04", "test": "rbac", "level": "cluster", "area": "security", "pillar": "security", "'
    'severity": "high", "effort": "medium", "title": "Autorizacion RBAC del cluster", "recommendation": "'
    'Validar RBAC de Kubernetes o Azure RBAC con acceso minimo; no atribuir privilegios efectivos sin lee'
    'r las asignaciones.", "business_impact": "RBAC deshabilitado reduce las barreras de autorizacion; un'
    'a opcion habilitada no prueba minimo privilegio."}, {"rule_id": "C04", "checklist_id": "05.01.08", "'
    'test": "local_accounts", "level": "cluster", "area": "security", "pillar": "security", "severity": "'
    'high", "effort": "low", "title": "Cuentas locales de AKS", "recommendation": "Revisar la deshabilita'
    'cion de cuentas locales despues de comprobar el acceso de emergencia y Microsoft Entra.", "business_'
    'impact": "Las cuentas locales pueden eludir controles de identidad centralizados."}, {"rule_id": "C0'
    '5", "checklist_id": "06.07.02", "test": "api_access", "level": "cluster", "area": "security", "pilla'
    'r": "security", "severity": "high", "effort": "medium", "title": "Acceso de red al API server", "rec'
    'ommendation": "Validar rangos autorizados o un endpoint privado segun los requisitos; conservar ruta'
    's de acceso necesarias.", "business_impact": "Un endpoint publico sin restriccion amplifica la super'
    'ficie de acceso, pero no prueba explotacion."}, {"rule_id": "C06", "checklist_id": "05.01.06", "test'
    '": "workload_identity", "level": "cluster", "area": "security", "pillar": "security", "severity": "m'
    'edium", "effort": "medium", "title": "OIDC y Microsoft Entra Workload ID", "recommendation": "Evalua'
    'r Workload ID para aplicaciones que acceden a Azure; habilitado no significa que cada pod lo utilice'
    '.", "business_impact": "Las aplicaciones que requieren Azure pueden evitar secretos duraderos median'
    'te identidad federada."}, {"rule_id": "C07", "checklist_id": "06.07.05", "test": "network_policy_eng'
    'ine", "level": "cluster", "area": "security", "pillar": "security", "severity": "medium", "effort": '
    '"medium", "title": "Motor de politicas de red", "recommendation": "Validar un motor compatible, incl'
    'uido Cilium, si la segmentacion es un requisito; revisar las politicas efectivas por separado.", "bu'
    'siness_impact": "Sin motor compatible, una NetworkPolicy puede no aplicar la segmentacion esperada."'
    '}, {"rule_id": "C08", "checklist_id": "06.04.01", "test": "cni", "level": "cluster", "area": "optimi'
    'zation", "pillar": "performance", "severity": "low", "effort": "high", "title": "Modelo de red e IPA'
    'M", "recommendation": "Usar la guia actual para elegir Azure CNI Overlay o una alternativa justifica'
    'da; no forzar maxPods=30 ni una migracion sin requisitos.", "business_impact": "Una eleccion de red '
    'no alineada con los requisitos puede limitar escalabilidad y direcciones IP."}, {"rule_id": "C09", "'
    'checklist_id": "04.01.01", "test": "policy", "level": "cluster", "area": "best_practices", "pillar":'
    ' "operations", "severity": "medium", "effort": "medium", "title": "Configuracion de Azure Policy", "'
    'recommendation": "Revisar Azure Policy o controles equivalentes y obtener resultados de cumplimiento'
    '; la extension no acredita cumplimiento.", "business_impact": "Controles de configuracion no gestion'
    'ados pueden permitir desviaciones; no se infiere cumplimiento de un addon."}, {"rule_id": "C10", "ch'
    'ecklist_id": "04.02.05", "test": "defender", "level": "cluster", "area": "security", "pillar": "secu'
    'rity", "severity": "medium", "effort": "medium", "title": "Configuracion de Defender para Containers'
    '", "recommendation": "Revisar la necesidad, coste, licencia y salud de Defender; no activar planes n'
    'i agentes desde este lector.", "business_impact": "La deteccion depende de cobertura, licencia y tel'
    'emetria reales, no solo de un perfil habilitado."}, {"rule_id": "C11", "checklist_id": "02.02.03", "'
    'test": "sla_tier", "level": "cluster", "area": "best_practices", "pillar": "reliability", "severity"'
    ': "medium", "effort": "low", "title": "Tier de AKS y objetivo de disponibilidad", "recommendation": '
    '"Para produccion con SLA requerido, evaluar Standard o Premium y las condiciones vigentes del SLA.",'
    ' "business_impact": "Un tier sin SLA puede no respaldar el objetivo contractual; no se ha calculado '
    'disponibilidad de las aplicaciones."}, {"rule_id": "C12", "checklist_id": "02.02.02", "test": "zones'
    '", "level": "node_pool", "area": "best_practices", "pillar": "reliability", "severity": "medium", "e'
    'ffort": "high", "title": "Distribucion zonal del node pool", "recommendation": "Cuando el requisito '
    'y la region lo permitan, planificar distribucion entre zonas y validar almacenamiento y capacidad.",'
    ' "business_impact": "Un pool requerido como multizona no distribuido puede compartir un dominio de f'
    'allo."}, {"rule_id": "C13", "checklist_id": "04.01.02", "test": "pool_state", "level": "node_pool", '
    '"area": "best_practices", "pillar": "reliability", "severity": "high", "effort": "medium", "title": '
    '"Estado de aprovisionamiento del node pool", "recommendation": "Investigar el estado y los eventos d'
    'el pool con el propietario; Running del cluster no confirma salud del pool ni de pods.", "business_i'
    'mpact": "Un fallo observado de aprovisionamiento puede afectar operaciones o capacidad; no prueba un'
    'a caida de servicio."}, {"rule_id": "C14", "checklist_id": "04.01.02", "test": "system_isolation", "'
    'level": "cluster", "area": "best_practices", "pillar": "reliability", "severity": "medium", "effort"'
    ': "medium", "title": "Aplicaciones fuera de pools System", "recommendation": "Revisar la ubicacion d'
    'e los pods de aplicacion y separar recursos de sistema y usuario sin imponer afinidad innecesaria.",'
    ' "business_impact": "Pods de aplicacion observados en pools System pueden competir con servicios ese'
    'nciales."}, {"rule_id": "C15", "checklist_id": "04.01.03", "test": "system_taint", "level": "node_po'
    'ol", "area": "best_practices", "pillar": "reliability", "severity": "low", "effort": "medium", "titl'
    'e": "Proteccion de scheduling del pool System", "recommendation": "Revisar CriticalAddonsOnly u otra'
    ' estrategia de aislamiento apropiada y la compatibilidad de los pods.", "business_impact": "Sin aisl'
    'amiento de scheduling, nuevas cargas pueden competir con los componentes de sistema."}, {"rule_id": '
    '"C16", "checklist_id": "07.06.01", "test": "autoscaling", "level": "cluster", "area": "finops", "pil'
    'lar": "cost", "severity": "medium", "effort": "medium", "title": "Estrategia de escalado de nodos", '
    '"recommendation": "Si el escalado dinamico es requerido, revisar Cluster Autoscaler o NAP/Automatic,'
    ' limites y pruebas; no prometer ahorro.", "business_impact": "Una capacidad fija puede no seguir cam'
    'bios de demanda; sin uso historico no se cuantifica sobrecapacidad."}, {"rule_id": "C17", "checklist'
    '_id": "07.02.04", "test": "upgrade_policy", "level": "cluster", "area": "best_practices", "pillar": '
    '"operations", "severity": "medium", "effort": "medium", "title": "Politica de actualizaciones de Kub'
    'ernetes", "recommendation": "Validar canal automatico o proceso manual documentado, compatibilidad y'
    ' mantenimiento; no actualizar desde el informe.", "business_impact": "Sin un proceso validado se dif'
    'iculta mantener soporte y parches; la ausencia de canal no prueba ausencia de proceso."}, {"rule_id"'
    ': "C18", "checklist_id": "07.02.06", "test": "node_image_policy", "level": "cluster", "area": "best_'
    'practices", "pillar": "operations", "severity": "medium", "effort": "medium", "title": "Actualizacio'
    'nes de imagen de nodo", "recommendation": "Revisar NodeImage, SecurityPatch u otro proceso documenta'
    'do; no recomendar kured si el canal gestionado ya cubre el caso.", "business_impact": "La imagen de '
    'nodo requiere mantenimiento; un canal configurado no demuestra que una actualizacion se completara."'
    '}, {"rule_id": "C19", "checklist_id": "07.02.04", "test": "version_support", "level": "cluster", "ar'
    'ea": "best_practices", "pillar": "operations", "severity": "high", "effort": "medium", "title": "Ver'
    'sion y catalogo regional actual", "recommendation": "Contrastar version, patches y soporte con el ca'
    'talogo regional y perfil de upgrade actuales; la ausencia no equivale automaticamente a fin de sopor'
    'te.", "business_impact": "Una version explicitamente fuera de la politica de soporte puede elevar ri'
    'esgo operativo; no se usa una tabla historica fija."}, {"rule_id": "C20", "checklist_id": "07.02.15"'
    ', "test": "diagnostics", "level": "cluster", "area": "best_practices", "pillar": "operations", "seve'
    'rity": "medium", "effort": "medium", "title": "Configuracion de logs del control plane", "recommenda'
    'tion": "Revisar categorias y destino de diagnostic settings y obtener evidencia de entrega; no confu'
    'ndir configuracion con ingesta.", "business_impact": "Sin registros disponibles puede resultar mas d'
    'ificil investigar cambios e incidentes."}, {"rule_id": "C21", "checklist_id": "07.04.01", "test": "t'
    'elemetry", "level": "cluster", "area": "optimization", "pillar": "performance", "severity": "low", "'
    'effort": "medium", "title": "Mediciones actuales disponibles", "recommendation": "Revisar cobertura '
    'temporal y fuentes de monitorizacion; una muestra metrics.k8s.io no demuestra uso mensual ni monitor'
    'izacion integral.", "business_impact": "La capacidad no puede optimizarse de forma fundamentada sin '
    'medidas y una ventana representativa."}, {"rule_id": "C22", "checklist_id": "07.02.04", "test": "mai'
    'ntenance", "level": "cluster", "area": "best_practices", "pillar": "operations", "severity": "low", '
    '"effort": "low", "title": "Ventanas de mantenimiento", "recommendation": "Si se requiere mantenimien'
    'to planificado, revisar ventanas y dependencias con el propietario.", "business_impact": "Ventanas n'
    'o definidas pueden dificultar coordinar cambios, pero no son prueba de interrupcion."}, {"rule_id": '
    '"W01", "checklist_id": "07.05.01", "test": "resources", "level": "workload", "area": "optimization",'
    ' "pillar": "performance", "severity": "medium", "effort": "low", "title": "Solicitudes de CPU/memori'
    'a y limite de memoria", "recommendation": "Definir requests y limite de memoria con medidas; CPU lim'
    'its se revisan solo cuando existe ese requisito explicito.", "business_impact": "La ausencia observa'
    'da de reservas o limites de memoria puede dificultar scheduling y aislamiento; requests no equivalen'
    ' a consumo.", "kubernetes_url": "https://kubernetes.io/docs/concepts/configuration/manage-resources-'
    'containers/"}, {"rule_id": "W02", "checklist_id": "07.05.04", "test": "probes", "level": "workload",'
    ' "area": "best_practices", "pillar": "reliability", "severity": "medium", "effort": "low", "title": '
    '"Probes de workloads que sirven trafico", "recommendation": "Para contenedores de servicio, validar '
    'readiness/liveness y su comportamiento; no imponer probes a jobs o sidecars sin aplicabilidad.", "bu'
    'siness_impact": "Sin probes adecuadas, Kubernetes dispone de menos senales para enrutar o reiniciar '
    'la aplicacion.", "kubernetes_url": "https://kubernetes.io/docs/concepts/configuration/liveness-readi'
    'ness-startup-probes/"}, {"rule_id": "W03", "checklist_id": "02.02.04", "test": "pdb", "level": "work'
    'load", "area": "best_practices", "pillar": "reliability", "severity": "medium", "effort": "low", "ti'
    'tle": "PDB para servicios replicados", "recommendation": "Definir y probar un PDB apropiado para ser'
    'vicios replicados; comprobar selector, replicas y capacidad durante upgrades.", "business_impact": "'
    'Sin un PDB que seleccione el servicio, una interrupcion voluntaria puede retirar demasiados pods; no'
    ' garantiza disponibilidad frente a fallos.", "kubernetes_url": "https://kubernetes.io/docs/tasks/run'
    '-application/configure-pdb/"}, {"rule_id": "W04", "checklist_id": "07.06.03", "test": "hpa", "level"'
    ': "workload", "area": "optimization", "pillar": "performance", "severity": "medium", "effort": "medi'
    'um", "title": "HPA cuando el workload requiere escalado", "recommendation": "Validar target, min/max'
    ' y disponibilidad de metricas antes de considerar HPA; un HPA configurado no prueba eficacia.", "bus'
    'iness_impact": "Un servicio con escalado requerido y sin controlador puede no ajustar capacidad a de'
    'manda.", "kubernetes_url": "https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscal'
    'e/"}, {"rule_id": "W05", "checklist_id": "02.02.04", "test": "replicas", "level": "workload", "area"'
    ': "best_practices", "pillar": "reliability", "severity": "medium", "effort": "low", "title": "Replic'
    'as segun objetivo de disponibilidad", "recommendation": "Validar replicas y dominios de fallo contra'
    ' el requisito del servicio; cero replicas intencionales no implica una caida.", "business_impact": "'
    'Un servicio requerido como redundante con una sola replica puede tener un punto unico de fallo."}, {'
    '"rule_id": "W06", "checklist_id": "04.01.01", "test": "container_security", "level": "workload", "ar'
    'ea": "security", "pillar": "security", "severity": "high", "effort": "medium", "title": "Privilegios'
    ' explicitos de contenedores Linux", "recommendation": "Revisar privileged, allowPrivilegeEscalation '
    'y ejecucion no root con las necesidades del contenedor; no inferir el UID de la imagen.", "business_'
    'impact": "Privilegios elevados explicitamente configurados pueden ampliar el impacto de un compromis'
    'o.", "kubernetes_url": "https://kubernetes.io/docs/concepts/security/pod-security-standards/"}, {"ru'
    'le_id": "W07", "checklist_id": "04.01.04", "test": "images", "level": "workload", "area": "security"'
    ', "pillar": "security", "severity": "low", "effort": "low", "title": "Referencias de imagen reproduc'
    'ibles", "recommendation": "Evitar latest o tags omitidos; evaluar digest, registro privado y control'
    'es de supply chain por separado.", "business_impact": "Una referencia mutable dificulta reproducir e'
    'l artefacto desplegado; un digest no acredita una imagen libre de vulnerabilidades."}, {"rule_id": "'
    'N01", "checklist_id": "07.05.02", "test": "quota", "level": "namespace", "area": "finops", "pillar":'
    ' "cost", "severity": "low", "effort": "low", "title": "Cuotas de namespace segun politica", "recomme'
    'ndation": "Cuando el namespace compartido requiere limites, revisar ResourceQuota y valores apropiad'
    'os; no confundir cuota con uso o factura.", "business_impact": "Sin una cuota requerida puede ser ma'
    's dificil contener consumo entre equipos."}, {"rule_id": "N02", "checklist_id": "04.01.01", "test": '
    '"pod_security", "level": "namespace", "area": "security", "pillar": "security", "severity": "medium"'
    ', "effort": "medium", "title": "Pod Security Admission configurado", "recommendation": "Validar enfo'
    'rce y controles equivalentes de Azure Policy; audit/warn no equivalen a bloqueo y una etiqueta no mi'
    'de conformidad.", "business_impact": "Un requisito de admission no aplicado puede permitir especific'
    'aciones que excedan la politica.", "kubernetes_url": "https://kubernetes.io/docs/concepts/security/p'
    'od-security-admission/"}, {"rule_id": "W08", "checklist_id": "06.07.06", "test": "network_policies",'
    ' "level": "workload", "area": "security", "pillar": "security", "severity": "medium", "effort": "med'
    'ium", "title": "NetworkPolicy que selecciona el workload", "recommendation": "Si se requiere segment'
    'acion, revisar selectors, policyTypes y reglas; una politica seleccionada no prueba aislamiento efec'
    'tivo.", "business_impact": "Sin una politica requerida que seleccione los pods puede faltar la segme'
    'ntacion prevista.", "kubernetes_url": "https://kubernetes.io/docs/concepts/services-networking/netwo'
    'rk-policies/"}, {"rule_id": "R01", "checklist_id": "07.07.06", "test": "pvc_binding", "level": "pvc"'
    ', "area": "best_practices", "pillar": "reliability", "severity": "medium", "effort": "medium", "titl'
    'e": "Estado de binding de PVC", "recommendation": "Investigar Lost o Pending con consumidor activo; '
    'WaitForFirstConsumer sin consumidor puede ser normal.", "business_impact": "Un volumen no enlazado r'
    'equerido por pods activos puede impedir scheduling o acceso a datos."}, {"rule_id": "R02", "checklis'
    't_id": "06.01.04", "test": "load_balancer", "level": "resource", "area": "best_practices", "pillar":'
    ' "reliability", "severity": "medium", "effort": "medium", "title": "SKU del balanceador Azure", "rec'
    'ommendation": "Revisar Standard y su configuracion ELB/ILB; frontend y probe configurados no acredit'
    'an salud de backends.", "business_impact": "Una SKU obsoleta puede limitar soporte o capacidades; no'
    ' se infiere exposicion solo del nombre."}, {"rule_id": "R03", "checklist_id": "06.02.01", "test": "s'
    'torage_tls", "level": "resource", "area": "security", "pillar": "security", "severity": "medium", "e'
    'ffort": "medium", "title": "Transporte HTTPS/TLS de Storage", "recommendation": "Validar HTTPS y TLS'
    ' minimo moderno con los clientes; esta regla local concreta la proteccion de recursos PaaS asociados'
    '.", "business_impact": "Permitir transporte no seguro puede exponer datos en transito; la regla no m'
    'ide accesos reales."}, {"rule_id": "R04", "checklist_id": "04.02.01", "test": "keyvault_rbac", "leve'
    'l": "resource", "area": "security", "pillar": "security", "severity": "low", "effort": "medium", "ti'
    'tle": "Modelo de autorizacion de Key Vault", "recommendation": "Revisar Azure RBAC o la justificacio'
    'n de access policies sin leer valores de secretos ni cambiar permisos.", "business_impact": "El mode'
    'lo y sus asignaciones requieren revision para sostener minimo privilegio."}, {"rule_id": "R05", "che'
    'cklist_id": "06.07.01", "test": "nsg_exposure", "level": "resource", "area": "security", "pillar": "'
    'security", "severity": "high", "effort": "medium", "title": "Reglas NSG de acceso administrativo des'
    'de Internet", "recommendation": "Revisar reglas Allow de Internet a SSH/RDP o todos los puertos y la'
    ' asociacion del NSG; no eliminar reglas automaticamente.", "business_impact": "Una regla explicita a'
    'mplia puede abrir una ruta de acceso si el NSG esta asociado y el destino escucha."}, {"rule_id": "R'
    '06", "checklist_id": "06.04.05", "test": "dns", "level": "cluster", "area": "optimization", "pillar"'
    ': "performance", "severity": "low", "effort": "medium", "title": "DNS e IPAM: rendimiento no medido"'
    ', "recommendation": "Revisar resolucion, latencia, capacidad de rangos y LocalDNS con pruebas autori'
    'zadas; inventario de DNS no acredita disponibilidad.", "business_impact": "La resolucion y el espaci'
    'o IP pueden limitar operaciones; este lector no ha medido consultas DNS."}, {"rule_id": "F01", "chec'
    'klist_id": "03.01.01", "test": "cost_allocation", "level": "cluster", "area": "finops", "pillar": "c'
    'ost", "severity": "low", "effort": "medium", "title": "Costes reales y atribucion Kubernetes", "reco'
    'mmendation": "Separar ActualCost por ResourceId/currency de asignacion a namespace/workload; no acti'
    'var cost analysis ni instalar Kubecost.", "business_impact": "Sin evidencia de asignacion no es posi'
    'ble atribuir la factura de nodos a aplicaciones."}, {"rule_id": "F02", "checklist_id": "07.06.04", "'
    'test": "rightsizing", "level": "cluster", "area": "finops", "pillar": "cost", "severity": "low", "ef'
    'fort": "medium", "title": "Right-sizing con ventana representativa", "recommendation": "Obtener uso '
    'historico, picos y requisitos antes de cambiar SKU o requests; la muestra actual solo orienta una re'
    'vision de capacidad.", "business_impact": "Una muestra puntual no demuestra infrautilizacion de 30 d'
    'ias ni ahorro alcanzable."}, {"rule_id": "F03", "checklist_id": "07.03.01", "test": "spot", "level":'
    ' "cluster", "area": "finops", "pillar": "cost", "severity": "low", "effort": "medium", "title": "Ele'
    'gibilidad de Spot y compromisos de ahorro", "recommendation": "Validar tolerancia a eviction, histor'
    'ial, demanda y elegibilidad comercial; no comprar reservas o savings plans.", "business_impact": "De'
    'scuentos potenciales implican condiciones y compromisos que no pueden inferirse del inventario."}, {'
    '"rule_id": "F04", "checklist_id": "07.02.01", "test": "advisor", "level": "cluster", "area": "finops'
    '", "pillar": "cost", "severity": "low", "effort": "low", "title": "Cobertura de recomendaciones Azur'
    'e Advisor", "recommendation": "Revisar recomendaciones cacheadas y su fecha/alcance; una lista vacia'
    ' no prueba ausencia universal de oportunidades.", "business_impact": "La decision de optimizar requi'
    'ere recomendaciones aplicables y evidencia, no porcentajes de ahorro inventados."}], "checklist_item'
    's": [{"id": "01.01.01", "guid": "785c2fa5-5b56-4ad4-a408-fe72734c476b", "text": "Use canary or blue/'
    'green deployments", "category": "Application Deployment", "subcategory": "Development", "waf": "Oper'
    'ations", "severity": "Medium", "link": "https://learn.microsoft.com/azure/architecture/reference-arc'
    'hitectures/containers/aks/secure-baseline-aks"}, {"id": "01.01.02", "guid": "ab5351f6-383a-45ed-9c5e'
    '-b143b16db40a", "text": "If required for AKS Windows workloads HostProcess containers can be used", '
    '"category": "Application Deployment", "subcategory": "Development", "waf": "Reliability", "severity"'
    ': "Low", "link": "https://learn.microsoft.com/azure/aks/use-windows-hpc"}, {"id": "01.01.03", "guid"'
    ': "a280dcf5-90ce-465d-b8e1-3f9ccbd46926", "text": "Use KEDA if running event-driven workloads", "cat'
    'egory": "Application Deployment", "subcategory": "Development", "waf": "Performance", "severity": "L'
    'ow", "link": "https://learn.microsoft.com/azure/azure-functions/functions-kubernetes-keda"}, {"id": '
    '"01.01.04", "guid": "26886d20-b66c-457b-a591-19bf8e8f5c58", "text": "Use Dapr to ease microservice d'
    'evelopment", "category": "Application Deployment", "subcategory": "Development", "waf": "Operations"'
    ', "severity": "Low", "link": "https://dapr.io/"}, {"id": "01.02.01", "guid": "3acbe04b-be20-49d3-afd'
    'a-47778424d116", "text": "Use automation through ARM/TF to create your Azure resources", "category":'
    ' "Application Deployment", "subcategory": "Infrastructure as Code", "waf": "Operations", "severity":'
    ' "Medium", "link": "https://learn.microsoft.com/azure/developer/terraform/create-k8s-cluster-with-tf'
    '-and-aks"}, {"id": "02.01.01", "guid": "36cb45e5-7960-4332-9bdf-8cc23318da61", "text": "Schedule and'
    ' perform DR tests regularly", "category": "BC and DR", "subcategory": "Disaster Recovery", "waf": "R'
    'eliability", "severity": "High", "link": "https://learn.microsoft.com/azure/cloud-adoption-framework'
    '/ready/enterprise-scale/business-continuity-and-disaster-recovery"}, {"id": "02.02.01", "guid": "170'
    '265f4-bb46-4a39-9af7-f317284797b1", "text": "Use Azure Traffic Manager or Azure Front Door as a glob'
    'al load balancer for region failover", "category": "BC and DR", "subcategory": "High Availability", '
    '"waf": "Reliability", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/operator-'
    'best-practices-multi-region"}, {"id": "02.02.02", "guid": "578a219a-46be-4b54-9350-24922634292b", "t'
    'ext": "Use Availability Zones if they are supported in your Azure region", "category": "BC and DR", '
    '"subcategory": "High Availability", "waf": "Reliability", "severity": "Medium", "link": "https://lea'
    'rn.microsoft.com/azure/aks/availability-zones"}, {"id": "02.02.03", "guid": "71d41e36-10cc-457b-9a4b'
    '-1410d4395898", "text": "Use the SLA-backed AKS offering", "category": "BC and DR", "subcategory": "'
    'High Availability", "waf": "Reliability", "severity": "High", "link": "https://learn.microsoft.com/a'
    'zure/aks/uptime-sla"}, {"id": "02.02.04", "guid": "c1288b3c-6a57-4cfc-9444-51e1a3d3453a", "text": "U'
    'se Disruption Budgets in your pod and deployment definitions", "category": "BC and DR", "subcategory'
    '": "High Availability", "waf": "Reliability", "severity": "Low", "link": "https://learn.microsoft.co'
    'm/azure/aks/operator-best-practices-scheduler"}, {"id": "02.02.05", "guid": "3c763963-7a55-42d5-a15e'
    '-401955387e5c", "text": "If using a private registry, configure region replication to store images i'
    'n multiple regions", "category": "BC and DR", "subcategory": "High Availability", "waf": "Reliabilit'
    'y", "severity": "High", "link": "https://learn.microsoft.com/azure/container-registry/container-regi'
    'stry-geo-replication"}, {"id": "02.02.06", "guid": "daa9a260-c3ea-4490-b077-5fc1f2a80cb0", "text": "'
    'Use Zone-Redundant Storage (ZRS) with stateful workloads", "category": "BC and DR", "subcategory": "'
    'Disaster Recovery", "waf": "Reliability", "severity": "High", "link": "https://learn.microsoft.com/a'
    'zure/aks/availability-zones#azure-disk-availability-zone-support"}, {"id": "02.03.01", "guid": "bc14'
    'aea6-e65d-48d9-a3ad-c218e6436b06", "text": "Define non-functional requirements such as SLAs, RTO (Re'
    'covery Time Objective) and RPO (Recovery Point Objective).", "category": "BC and DR", "subcategory":'
    ' "Requirements", "waf": "Reliability", "severity": "High", "link": "https://learn.microsoft.com/azur'
    'e/cloud-adoption-framework/ready/enterprise-scale/business-continuity-and-disaster-recovery"}, {"id"'
    ': "03.01.01", "guid": "f82cb8eb-8c0a-4a63-a25a-4956eaa8dc4a", "text": "Use an external application s'
    'uch as kubecost to allocate costs to different users", "category": "Cost Governance", "subcategory":'
    ' "Cost", "waf": "Cost", "severity": "Low", "link": "https://learn.microsoft.com/azure/cloud-adoption'
    '-framework/scenarios/aks/eslz-cost-governance-with-kubecost"}, {"id": "03.01.02", "guid": "4d3dfbab-'
    '9924-4831-a68d-fdf0d72f462c", "text": "Use scale down mode to delete/deallocate nodes", "category": '
    '"Cost Governance", "subcategory": "Cost", "waf": "Cost", "severity": "Low", "link": "https://learn.m'
    'icrosoft.com/azure/aks/scale-down-mode"}, {"id": "03.01.03", "guid": "87e651ea-bc4a-4a87-a6df-c06a4b'
    '570ebc", "text": "When required use multi-instance partitioning GPU on AKS Clusters", "category": "C'
    'ost Governance", "subcategory": "Cost", "waf": "Cost", "severity": "Medium", "link": "https://learn.'
    'microsoft.com/azure/aks/gpu-multi-instance"}, {"id": "03.01.04", "guid": "2b72a08b-0410-4cd6-9093-e0'
    '68a5cf27e8", "text": "If running a Dev/Test cluster use NodePool Start/Stop", "category": "Cost Gove'
    'rnance", "subcategory": "Cost", "waf": "Cost", "severity": "Low", "link": "https://learn.microsoft.c'
    'om/azure/aks/start-stop-nodepools"}, {"id": "04.01.01", "guid": "9ca48e4a-85e2-4223-bce8-bb12307ca5f'
    '1", "text": "Use Azure Policy for Kubernetes to ensure cluster compliance", "category": "Governance '
    'and Security", "subcategory": "Compliance", "waf": "Security", "severity": "Medium", "link": "https:'
    '//learn.microsoft.com/azure/governance/policy/concepts/policy-for-kubernetes"}, {"id": "04.01.02", "'
    'guid": "6f158e3e-a3a9-42c2-be7e-2165c3a87af4", "text": "Separate applications from the control plane'
    ' with user/system node pools", "category": "Governance and Security", "subcategory": "Compliance", "'
    'waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/use-system-po'
    'ols"}, {"id": "04.01.03", "guid": "a7a1f893-9bda-4477-98f2-4c116775c2ea", "text": "Add taint to your'
    ' system nodepool to make it dedicated", "category": "Governance and Security", "subcategory": "Compl'
    'iance", "waf": "Security", "severity": "Low", "link": "https://learn.microsoft.com/azure/aks/use-sys'
    'tem-pools"}, {"id": "04.01.04", "guid": "55b46a94-8008-4ae7-b7e4-b475b6c8bdbf", "text": "Use a priva'
    'te registry for your images, such as ACR", "category": "Governance and Security", "subcategory": "Co'
    'mpliance", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/conta'
    'iner-registry/"}, {"id": "04.01.05", "guid": "59bce65d-e8a0-43f9-9879-468d66a786d6", "text": "Scan y'
    'our images for vulnerabilities", "category": "Governance and Security", "subcategory": "Compliance",'
    ' "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/security-center'
    '/container-security"}, {"id": "04.01.06", "guid": "cc639637-a652-42ac-89e8-06965388e9de", "text": "U'
    'se Azure Security Center to detect security posture vulnerabilities", "category": "Governance and Se'
    'curity", "subcategory": "Compliance", "waf": "Security", "severity": "Medium", "link": "https://lear'
    'n.microsoft.com/azure/security-center/container-security"}, {"id": "04.01.07", "guid": "42d4aefe-238'
    '3-470e-b019-c30df24996b2", "text": "If required configure FIPS", "category": "Governance and Securit'
    'y", "subcategory": "Compliance", "waf": "Security", "severity": "Low", "link": "https://learn.micros'
    'oft.com/azure/aks/use-multiple-node-pools#add-a-fips-enabled-node-pool"}, {"id": "04.01.08", "guid":'
    ' "d167dd18-2b0a-4c24-8b99-9a646f8389a7", "text": "Define app separation requirements (namespace/node'
    'pool/cluster)", "category": "Governance and Security", "subcategory": "Compliance", "waf": "Security'
    '", "severity": "High", "link": "https://learn.microsoft.com/azure/aks/operator-best-practices-cluste'
    'r-isolation"}, {"id": "04.02.01", "guid": "5e3df584-eccc-4d97-a3b6-bcda3b50eb2e", "text": "Store you'
    'r secrets in Azure Key Vault with the CSI Secrets Store driver", "category": "Governance and Securit'
    'y", "subcategory": "Secrets", "waf": "Security", "severity": "Medium", "link": "https://github.com/A'
    'zure/secrets-store-csi-driver-provider-azure"}, {"id": "04.02.02", "guid": "b03dda6d-58d7-4c89-8ddb-'
    '107d5769ae66", "text": "If using Service Principals for the cluster, refresh credentials periodicall'
    'y (like quarterly)", "category": "Governance and Security", "subcategory": "Secrets", "waf": "Securi'
    'ty", "severity": "High", "link": "https://learn.microsoft.com/azure/aks/update-credentials"}, {"id":'
    ' "04.02.03", "guid": "e7ba73a3-0508-4f80-806f-527db30cee96", "text": "If required add Key Management'
    ' Service etcd encryption", "category": "Governance and Security", "subcategory": "Secrets", "waf": "'
    'Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/use-kms-etcd-encrypt'
    'ion"}, {"id": "04.02.04", "guid": "ec8e4e42-0344-41b0-b865-9123e8956d31", "text": "If required consi'
    'der using Confidential Compute for AKS", "category": "Governance and Security", "subcategory": "Secr'
    'ets", "waf": "Security", "severity": "Low", "link": "https://learn.microsoft.com/azure/confidential-'
    'computing/confidential-nodes-aks-overview"}, {"id": "04.02.05", "guid": "c9e95ffe-6dd1-4a17-8c5f-110'
    '389ca9b21", "text": "Consider using Defender for Containers", "category": "Governance and Security",'
    ' "subcategory": "Secrets", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft'
    '.com/azure/defender-for-cloud/defender-for-containers-enable"}, {"id": "05.01.01", "guid": "ed127dd1'
    '-42b0-46b2-8c69-99a646f3389a", "text": "Use managed identities instead of Service Principals", "cate'
    'gory": "Identity and Access Management", "subcategory": "Identity", "waf": "Security", "severity": "'
    'High", "link": "https://learn.microsoft.com/azure/aks/use-managed-identity"}, {"id": "05.01.02", "gu'
    'id": "7e42c78e-78c0-46a6-8a21-94956e698dc4", "text": "Integrate authentication with AAD (using the m'
    'anaged integration)", "category": "Identity and Access Management", "subcategory": "Identity", "waf"'
    ': "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/managed-aad"}, {"'
    'id": "05.01.03", "guid": "a2fe27b2-e287-401a-8352-beedf79b488d", "text": "Limit access to admin kube'
    'config (get-credentials --admin)", "category": "Identity and Access Management", "subcategory": "Ide'
    'ntity", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/cont'
    'rol-kubeconfig-access"}, {"id": "05.01.04", "guid": "eec4962c-c3bd-421b-b77f-26e5e6b3bec3", "text": '
    '"Integrate authorization with AAD RBAC", "category": "Identity and Access Management", "subcategory"'
    ': "Identity", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/ak'
    's/manage-azure-rbac"}, {"id": "05.01.05", "guid": "d4f3537c-1346-4dc5-9027-a71ffe1bd05d", "text": "U'
    'se namespaces for restricting RBAC privilege in Kubernetes", "category": "Identity and Access Manage'
    'ment", "subcategory": "Identity", "waf": "Security", "severity": "High", "link": "https://learn.micr'
    'osoft.com/azure/aks/operator-best-practices-identity"}, {"id": "05.01.06", "guid": "d2e0d5d7-71d4-41'
    'e3-910c-c57b4a4b1410", "text": "For Pod Identity Access Management use Azure AD Workload Identity (p'
    'review)", "category": "Identity and Access Management", "subcategory": "Identity", "waf": "Security"'
    ', "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/workload-identity-migration-s'
    'idecar"}, {"id": "05.01.07", "guid": "f4dcf690-1b30-407d-abab-6f8aa780d3a3", "text": "For AKS non-in'
    'teractive logins use kubelogin (preview)", "category": "Identity and Access Management", "subcategor'
    'y": "Identity", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/'
    'aks/managed-aad#non-interactive-sign-in-with-kubelogin"}, {"id": "05.01.08", "guid": "b085b1f2-3119-'
    '4771-8c9a-bbf4411810ec", "text": "Disable AKS local accounts", "category": "Identity and Access Mana'
    'gement", "subcategory": "Identity", "waf": "Security", "severity": "Medium", "link": "https://learn.'
    'microsoft.com/azure/aks/managed-aad#disable-local-accounts"}, {"id": "05.01.09", "guid": "36abb0db-c'
    '118-4f4c-9880-3f30f9a2deb6", "text": "Configure if required Just-in-time cluster access", "category"'
    ': "Identity and Access Management", "subcategory": "Identity", "waf": "Security", "severity": "Low",'
    ' "link": "https://learn.microsoft.com/azure/aks/managed-aad#configure-just-in-time-cluster-access-wi'
    'th-azure-ad-and-aks"}, {"id": "05.01.10", "guid": "c4d7f4c6-79bf-45d0-aa05-ce8fc717e150", "text": "C'
    'onfigure if required AAD conditional access for AKS", "category": "Identity and Access Management", '
    '"subcategory": "Identity", "waf": "Security", "severity": "Low", "link": "https://learn.microsoft.co'
    'm/azure/aks/managed-aad#use-conditional-access-with-azure-ad-and-aks"}, {"id": "05.01.11", "guid": "'
    'e1123a7c-a333-4eb4-a120-4ee3f293c9f3", "text": "If required for Windows AKS workloads configure gMSA'
    ' ", "category": "Identity and Access Management", "subcategory": "Identity", "waf": "Security", "sev'
    'erity": "Low", "link": "https://learn.microsoft.com/azure/aks/use-group-managed-service-accounts"}, '
    '{"id": "05.01.12", "guid": "1f711a74-3672-470b-b8b8-a2148d640d79", "text": "For finer control consid'
    'er using a managed Kubelet Identity", "category": "Identity and Access Management", "subcategory": "'
    'Identity", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/u'
    'se-managed-identity#use-a-pre-created-kubelet-managed-identity"}, {"id": "06.01.01", "guid": "cbd8ac'
    '2a-aebc-4a2a-94da-1dbf3dc99248", "text": "If using AGIC, do not share an AppGW across clusters", "ca'
    'tegory": "Network Topology and Connectivity", "subcategory": "Best practices", "waf": "Reliability",'
    ' "severity": "Medium", "link": "https://azure.github.io/application-gateway-kubernetes-ingress/setup'
    '/install-existing/"}, {"id": "06.01.02", "guid": "8008ae7d-7e4b-4475-a6c8-bdbf59bce65d", "text": "Do'
    ' not use AKS HTTP Routing Add-On, use instead the managed NGINX ingress with the application routing'
    ' add-on.", "category": "Network Topology and Connectivity", "subcategory": "Best practices", "waf": '
    '"Reliability", "severity": "High", "link": "https://learn.microsoft.com/azure/aks/http-application-r'
    'outing"}, {"id": "06.01.03", "guid": "7bacd7b9-c025-4a9d-a5d2-25d6bc5439d9", "text": "For Windows wo'
    'rkloads use Accelerated Networking", "category": "Network Topology and Connectivity", "subcategory":'
    ' "Best practices", "waf": "Performance", "severity": "Medium", "link": "https://learn.microsoft.com/'
    'azure/virtual-network/accelerated-networking-overview"}, {"id": "06.01.04", "guid": "ba7da7be-9952-4'
    '914-a384-5d997cb39132", "text": "Use the standard ALB (as opposed to the basic one)", "category": "N'
    'etwork Topology and Connectivity", "subcategory": "Best practices", "waf": "Reliability", "severity"'
    ': "High", "link": "https://learn.microsoft.com/azure/aks/load-balancer-standard"}, {"id": "06.01.05"'
    ', "guid": "22fbe8d6-9b40-47ef-9011-25bb1a555a6b", "text": "If using Azure CNI, consider using differ'
    'ent Subnets for NodePools", "category": "Network Topology and Connectivity", "subcategory": "Best pr'
    'actices", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/us'
    'e-multiple-node-pools#add-a-node-pool-with-a-unique-subnet"}, {"id": "06.02.01", "guid": "c3c39c98-6'
    'bb2-4c12-859a-114b5e3df584", "text": "Use Private Endpoints (preferred) or Virtual Network Service E'
    'ndpoints to access PaaS services from the cluster", "category": "Network Topology and Connectivity",'
    ' "subcategory": "Cost", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.co'
    'm/azure/private-link/private-link-overview"}, {"id": "06.03.01", "guid": "e8a03f97-8794-468d-96a7-86'
    'd60f96c97b", "text": "If hybrid connectivity is required, use 2xER or ER+VPN for better availability'
    '", "category": "Network Topology and Connectivity", "subcategory": "HA", "waf": "Reliability", "seve'
    'rity": "Medium", "link": "https://learn.microsoft.com/azure/expressroute/designing-for-disaster-reco'
    'very-with-expressroute-privatepeering"}, {"id": "06.04.01", "guid": "a0f61565-9de5-458f-a372-49c8311'
    '12dbd", "text": "Choose the best CNI network plugin for your requirements (Azure CNI recommended)", '
    '"category": "Network Topology and Connectivity", "subcategory": "IPAM", "waf": "Reliability", "sever'
    'ity": "High", "link": "https://learn.microsoft.com/azure/aks/operator-best-practices-network"}, {"id'
    '": "06.04.02", "guid": "7faf12e7-0943-4f63-8472-2da29c2b1cd6", "text": "If using Azure CNI, size you'
    'r subnet accordingly considering the maximum number of pods per node", "category": "Network Topology'
    ' and Connectivity", "subcategory": "IPAM", "waf": "Performance", "severity": "High", "link": "https:'
    '//learn.microsoft.com/azure/aks/configure-azure-cni"}, {"id": "06.04.03", "guid": "22f54b29-bade-43a'
    'a-b1e8-c38ec9366673", "text": "If using Azure CNI, check the maximum pods/node (default 30)", "categ'
    'ory": "Network Topology and Connectivity", "subcategory": "IPAM", "waf": "Performance", "severity": '
    '"High", "link": "https://learn.microsoft.com/azure/aks/configure-azure-cni"}, {"id": "06.04.04", "gu'
    'id": "13c00567-4b1e-4945-a459-c373e7ed6162", "text": "If using private-IP LoadBalancer services, use'
    ' a dedicated subnet (not the AKS subnet)", "category": "Network Topology and Connectivity", "subcate'
    'gory": "IPAM", "waf": "Security", "severity": "Low", "link": "https://learn.microsoft.com/azure/aks/'
    'internal-lb"}, {"id": "06.04.05", "guid": "43f63047-22d9-429c-8b1c-d622f54b29ba", "text": "Size the '
    'service IP address range accordingly (it is going to limit the cluster scalability)", "category": "N'
    'etwork Topology and Connectivity", "subcategory": "IPAM", "waf": "Reliability", "severity": "High", '
    '"link": "https://learn.microsoft.com/azure/aks/configure-azure-cni"}, {"id": "06.05.01", "guid": "57'
    'bf217f-6dc8-481c-81e2-785773e9c00f", "text": "If required add your own CNI plugin", "category": "Net'
    'work Topology and Connectivity", "subcategory": "Operations", "waf": "Security", "severity": "Low", '
    '"link": "https://learn.microsoft.com/azure/aks/use-byo-cni"}, {"id": "06.05.02", "guid": "4b3bb365-9'
    '458-44d9-9ed1-5c8f52890364", "text": "If required configure Public IP per node in AKS", "category": '
    '"Network Topology and Connectivity", "subcategory": "Operations", "waf": "Performance", "severity": '
    '"Low", "link": "https://learn.microsoft.com/azure/aks/use-multiple-node-pools#assign-a-public-ip-per'
    '-node-for-your-node-pools"}, {"id": "06.06.01", "guid": "b3808b9f-a1cf-4204-ad01-3a923ce474db", "tex'
    't": "Use an ingress controller to expose web-based apps instead of exposing them with LoadBalancer-t'
    'ype services", "category": "Network Topology and Connectivity", "subcategory": "Scalability", "waf":'
    ' "Reliability", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/concepts-networ'
    'k"}, {"id": "06.06.02", "guid": "ccb534e7-416e-4a1d-8e93-533b53199085", "text": "Use Azure NAT Gatew'
    'ay as outboundType for scaling egress traffic", "category": "Network Topology and Connectivity", "su'
    'bcategory": "Scalability", "waf": "Reliability", "severity": "Low", "link": "https://learn.microsoft'
    '.com/azure/aks/nat-gateway"}, {"id": "06.06.03", "guid": "8ee9a69a-1b58-4b1e-9c61-476e110a160b", "te'
    'xt": "Use Dynamic allocations of IPs in order to avoid Azure CNI IP exhaustion", "category": "Networ'
    'k Topology and Connectivity", "subcategory": "Scalability", "waf": "Reliability", "severity": "Mediu'
    'm", "link": "https://learn.microsoft.com/azure/aks/configure-azure-cni#dynamic-allocation-of-ips-and'
    '-enhanced-subnet-support"}, {"id": "06.07.01", "guid": "3b365a91-7ecb-4e48-bbe5-4cd7df2e8bba", "text'
    '": "Filter egress traffic with AzFW/NVA if your security requirements mandate it", "category": "Netw'
    'ork Topology and Connectivity", "subcategory": "Security", "waf": "Security", "severity": "High", "l'
    'ink": "https://learn.microsoft.com/azure/aks/limit-egress-traffic"}, {"id": "06.07.02", "guid": "c45'
    '81559-bb91-463e-a908-aed8c44ce3b2", "text": "If using a public API endpoint, restrict the IP address'
    'es that can access it", "category": "Network Topology and Connectivity", "subcategory": "Security", '
    '"waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/api-server-a'
    'uthorized-ip-ranges"}, {"id": "06.07.03", "guid": "ecccd979-3b6b-4cda-9b50-eb2eb03dda6d", "text": "U'
    'se private clusters if your requirements mandate it", "category": "Network Topology and Connectivity'
    '", "subcategory": "Security", "waf": "Security", "severity": "High", "link": "https://learn.microsof'
    't.com/azure/aks/private-clusters"}, {"id": "06.07.04", "guid": "ce7f2a7c-297c-47c6-adea-a6ff838db665'
    '", "text": "For Windows 2019 and 2022 AKS nodes Calico Network Policies can be used ", "category": "'
    'Network Topology and Connectivity", "subcategory": "Security", "waf": "Security", "severity": "Mediu'
    'm", "link": "https://learn.microsoft.com/azure/aks/use-network-policies"}, {"id": "06.07.05", "guid"'
    ': "58d7c892-ddb1-407d-9769-ae669ca48e4a", "text": "Enable a Kubernetes Network Policy option (Calico'
    '/Azure)", "category": "Network Topology and Connectivity", "subcategory": "Security", "waf": "Securi'
    'ty", "severity": "High", "link": "https://learn.microsoft.com/azure/aks/use-network-policies"}, {"id'
    '": "06.07.06", "guid": "85e2223e-ce8b-4b12-907c-a5f16f158e3e", "text": "Use Kubernetes network polic'
    'ies to increase intra-cluster security", "category": "Network Topology and Connectivity", "subcatego'
    'ry": "Security", "waf": "Security", "severity": "High", "link": "https://learn.microsoft.com/azure/a'
    'ks/operator-best-practices-network"}, {"id": "06.07.07", "guid": "a3a92c2d-e7e2-4165-a3a8-7af4a7a1f8'
    '93", "text": "Use a WAF for web workloads (UIs or APIs)", "category": "Network Topology and Connecti'
    'vity", "subcategory": "Security", "waf": "Security", "severity": "High", "link": "https://learn.micr'
    'osoft.com/azure/aks/operator-best-practices-network"}, {"id": "06.07.08", "guid": "9bda4776-8f24-4c1'
    '1-9775-c2ea55b46a94", "text": "Use DDoS Standard in the AKS Virtual Network", "category": "Network T'
    'opology and Connectivity", "subcategory": "Security", "waf": "Security", "severity": "Medium", "link'
    '": "https://learn.microsoft.com/azure/virtual-network/ddos-protection-overview"}, {"id": "06.07.09",'
    ' "guid": "6c46b91a-1107-4485-ad66-3183e2a8c266", "text": "If required add company HTTP Proxy", "cate'
    'gory": "Network Topology and Connectivity", "subcategory": "Security", "waf": "Security", "severity"'
    ': "Low", "link": "https://learn.microsoft.com/azure/aks/http-proxy"}, {"id": "06.07.10", "guid": "e9'
    '855d04-c3c3-49c9-a6bb-2c12159a114b", "text": "Consider using a service mesh for advanced microservic'
    'e communication management", "category": "Network Topology and Connectivity", "subcategory": "Securi'
    'ty", "waf": "Security", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/service'
    'mesh-about"}, {"id": "07.01.01", "guid": "67f7a9ed-5b31-4f38-a3f3-9812b2463cff", "text": "Configure '
    'alerts on the most critical metrics (see Container Insights for recommendations)", "category": "Oper'
    'ations", "subcategory": "Alerting", "waf": "Operations", "severity": "High", "link": "https://learn.'
    'microsoft.com/azure/azure-monitor/insights/container-insights-metric-alerts"}, {"id": "07.02.01", "g'
    'uid": "337453a3-cc63-4963-9a65-22ac19e80696", "text": "Check regularly Azure Advisor for recommendat'
    'ions on your cluster", "category": "Operations", "subcategory": "Compliance", "waf": "Operations", "'
    'severity": "Low", "link": "https://learn.microsoft.com/azure/advisor/advisor-get-started"}, {"id": "'
    '07.02.03", "guid": "3aa70560-e7e7-4968-be3d-628af35b2ced", "text": "Enable AKS auto-certificate rota'
    'tion", "category": "Operations", "subcategory": "Compliance", "waf": "Operations", "severity": "Low"'
    ', "link": "https://learn.microsoft.com/azure/aks/certificate-rotation"}, {"id": "07.02.04", "guid": '
    '"e189c599-df0d-45a7-9dd4-ce32c1881370", "text": "Have a regular process to upgrade your kubernetes v'
    'ersion periodically (quarterly, for example), or use the AKS autoupgrade feature", "category": "Oper'
    'ations", "subcategory": "Compliance", "waf": "Operations", "severity": "High", "link": "https://lear'
    'n.microsoft.com/azure/aks/supported-kubernetes-versions"}, {"id": "07.02.05", "guid": "6f7c4c0d-4e51'
    '-4464-ad24-57ed67138b82", "text": "Use kured for Linux node upgrades in case you are not using node-'
    'image upgrade", "category": "Operations", "subcategory": "Compliance", "waf": "Operations", "severit'
    'y": "High", "link": "https://learn.microsoft.com/azure/aks/node-updates-kured"}, {"id": "07.02.06", '
    '"guid": "139c9580-ade3-426a-ba09-cf157d9f6477", "text": "Have a regular process to upgrade the clust'
    'er node images periodically (weekly, for example)", "category": "Operations", "subcategory": "Compli'
    'ance", "waf": "Operations", "severity": "High", "link": "https://learn.microsoft.com/azure/aks/node-'
    'image-upgrade"}, {"id": "07.02.07", "guid": "0102ce16-ee30-41e6-b882-e52e4621dd68", "text": "Conside'
    'r gitops to deploy applications or cluster configuration to multiple clusters", "category": "Operati'
    'ons", "subcategory": "Compliance", "waf": "Operations", "severity": "Low", "link": "https://learn.mi'
    'crosoft.com/azure/architecture/example-scenario/bedrock/bedrock-automated-deployments"}, {"id": "07.'
    '02.08", "guid": "d7672c26-7602-4482-85a4-14527fbe855c", "text": "Consider using AKS command invoke o'
    'n private clusters", "category": "Operations", "subcategory": "Compliance", "waf": "Operations", "se'
    'verity": "Low", "link": "https://learn.microsoft.com/azure/aks/command-invoke"}, {"id": "07.02.09", '
    '"guid": "31d7aaab-7571-4449-ab80-53d89e89d17b", "text": "For planned events consider using Node Auto'
    ' Drain", "category": "Operations", "subcategory": "Compliance", "waf": "Operations", "severity": "Lo'
    'w", "link": "https://learn.microsoft.com/azure/aks/node-auto-repair#node-autodrain"}, {"id": "07.02.'
    '10", "guid": "ed0fda7f-211b-47c7-8b6e-c18873fb473c", "text": "Develop own governance practices to ma'
    'ke sure no changes are performed by operators in the node RG (aka \'infra RG\')", "category": "Operati'
    'ons", "subcategory": "Compliance", "waf": "Operations", "severity": "High", "link": "https://learn.m'
    'icrosoft.com/azure/aks/faq"}, {"id": "07.02.11", "guid": "73b32a5a-67f7-4a9e-b5b3-1f38c3f39812", "te'
    'xt": "Use custom Node RG (aka \'Infra RG\') name", "category": "Operations", "subcategory": "Complianc'
    'e", "waf": "Operations", "severity": "Low", "link": "https://learn.microsoft.com/azure/aks/cluster-c'
    'onfiguration"}, {"id": "07.02.12", "guid": "b2463cff-e189-4c59-adf0-d5a73dd4ce32", "text": "Do not u'
    'se deprecated Kubernetes APIs in your YAML manifests", "category": "Operations", "subcategory": "Com'
    'pliance", "waf": "Operations", "severity": "Medium", "link": "https://kubernetes.io/docs/setup/relea'
    'se/notes/"}, {"id": "07.02.13", "guid": "c1881370-6f7c-44c0-b4e5-14648d2457ed", "text": "Taint Windo'
    'ws nodes", "category": "Operations", "subcategory": "Compliance", "waf": "Operations", "severity": "'
    'Low", "link": "https://learn.microsoft.com/azure-stack/aks-hci/adapt-apps-mixed-os-clusters"}, {"id"'
    ': "07.02.14", "guid": "67138b82-0102-4ce1-9ee3-01e6e882e52e", "text": "Keep windows containers patch'
    ' level in sync with host patch level", "category": "Operations", "subcategory": "Compliance", "waf":'
    ' "Operations", "severity": "Low", "link": "https://learn.microsoft.com/virtualization/windowscontain'
    'ers/deploy-containers/version-compatibility?tabs=windows-server-20H2%2Cwindows-10-20H2"}, {"id": "07'
    '.02.15", "guid": "5b56ad48-408f-4e72-934c-476ba280dcf5", "text": "Send master logs (aka API logs) to'
    ' Azure Monitor or your preferred log management solution", "category": "Operations", "subcategory": '
    '"Compliance", "waf": "Operations", "severity": "Low", "link": "https://learn.microsoft.com/azure/aks'
    '/monitor-aks"}, {"id": "07.02.16", "guid": "64d1a846-e28a-4b6b-9a33-22a635c15a21", "text": "If requi'
    'red use nodePool snapshots", "category": "Operations", "subcategory": "Compliance", "waf": "Cost", "'
    'severity": "Low", "link": "https://learn.microsoft.com/azure/aks/node-pool-snapshot"}, {"id": "07.03'
    '.01", "guid": "c5a5b252-1e44-4a59-a9d2-399c4d7b68d0", "text": "Consider spot node pools for non time'
    '-sensitive workloads", "category": "Operations", "subcategory": "Cost", "waf": "Operations", "severi'
    'ty": "Low", "link": "https://learn.microsoft.com/azure/aks/spot-node-pool"}, {"id": "07.03.02", "gui'
    'd": "c755562f-2b4e-4456-9b4d-874a748b662e", "text": "Consider AKS virtual node for quick bursting", '
    '"category": "Operations", "subcategory": "Cost", "waf": "Operations", "severity": "Low", "link": "ht'
    'tps://learn.microsoft.com/azure/aks/concepts-scale"}, {"id": "07.04.01", "guid": "6f8389a7-f82c-4b8e'
    '-a8c0-aa63a25a4956", "text": "Monitor your cluster metrics with Container Insights (or other tools l'
    'ike Prometheus)", "category": "Operations", "subcategory": "Monitoring", "waf": "Operations", "sever'
    'ity": "High", "link": "https://learn.microsoft.com/azure/azure-monitor/insights/container-insights-o'
    'verview"}, {"id": "07.04.02", "guid": "eaa8dc4a-2436-47b3-9697-15b1752beee0", "text": "Store and ana'
    'lyze your cluster logs with Container Insights (or other tools like Telegraf/ElasticSearch)", "categ'
    'ory": "Operations", "subcategory": "Monitoring", "waf": "Operations", "severity": "High", "link": "h'
    'ttps://learn.microsoft.com/azure/azure-monitor/insights/container-insights-overview"}, {"id": "07.04'
    '.03", "guid": "4621dd68-c5a5-4be2-bdb1-1726769ef669", "text": "Monitor CPU and memory utilization of'
    ' the nodes", "category": "Operations", "subcategory": "Monitoring", "waf": "Operations", "severity":'
    ' "Medium", "link": "https://learn.microsoft.com/azure/azure-monitor/containers/container-insights-an'
    'alyze"}, {"id": "07.04.04", "guid": "1a4835ac-9422-423e-ae80-b123081a5417", "text": "If using Azure '
    'CNI, monitor % of pod IPs consumed per node", "category": "Operations", "subcategory": "Monitoring",'
    ' "waf": "Operations", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/configure'
    '-azure-cni"}, {"id": "07.04.05", "guid": "415833ea-3ad3-4c2d-b733-165c3acbe04b", "text": "Monitor OS'
    ' disk queue depth in nodes", "category": "Operations", "subcategory": "Monitoring", "waf": "Operatio'
    'ns", "severity": "Medium", "link": "https://learn.microsoft.com/azure/virtual-machines/premium-stora'
    'ge-performance"}, {"id": "07.04.06", "guid": "be209d39-fda4-4777-a424-d116785c2fa5", "text": "If not'
    ' using egress filtering with AzFW/NVA, monitor standard ALB allocated SNAT ports", "category": "Oper'
    'ations", "subcategory": "Monitoring", "waf": "Operations", "severity": "Medium", "link": "https://le'
    'arn.microsoft.com/azure/aks/load-balancer-standard"}, {"id": "07.04.07", "guid": "74c2ee76-569b-4a79'
    '-a57e-dedf91b022c9", "text": "Subscribe to resource health notifications for your AKS cluster", "cat'
    'egory": "Operations", "subcategory": "Monitoring", "waf": "Operations", "severity": "Medium", "link"'
    ': "https://learn.microsoft.com/azure/aks/aks-resource-health"}, {"id": "07.05.01", "guid": "b54eb2eb'
    '-03dd-4aa3-9927-18e2edb11726", "text": "Configure requests and limits in your pod specs", "category"'
    ': "Operations", "subcategory": "Resources", "waf": "Operations", "severity": "High", "link": "https:'
    '//learn.microsoft.com/azure/aks/operator-best-practices-scheduler"}, {"id": "07.05.02", "guid": "769'
    'ef669-1a48-435a-a942-223ece80b123", "text": "Enforce resource quotas for namespaces", "category": "O'
    'perations", "subcategory": "Resources", "waf": "Operations", "severity": "Medium", "link": "https://'
    'learn.microsoft.com/azure/aks/operator-best-practices-scheduler"}, {"id": "07.05.03", "guid": "081a5'
    '417-4158-433e-a3ad-3c2de733165c", "text": "Ensure your subscription has enough quota to scale out yo'
    'ur nodepools", "category": "Operations", "subcategory": "Resources", "waf": "Operations", "severity"'
    ': "High", "link": "https://learn.microsoft.com/azure/azure-resource-manager/management/azure-subscri'
    'ption-service-limits"}, {"id": "07.05.04", "guid": "f4fd0602-7ab5-46f1-b66a-e9dea9654a65", "text": "'
    'Configure Liveness and Readiness probes for all deployments", "category": "Operations", "subcategory'
    '": "Resources", "waf": "Operations", "severity": "High", "link": "https://kubernetes.io/docs/tasks/c'
    'onfigure-pod-container/configure-liveness-readiness-startup-probes/"}, {"id": "07.06.01", "guid": "9'
    '0ce65de-8e13-4f9c-abd4-69266abca264", "text": "Use the Cluster Autoscaler", "category": "Operations"'
    ', "subcategory": "Scalability", "waf": "Performance", "severity": "Medium", "link": "https://learn.m'
    'icrosoft.com/azure/aks/concepts-scale"}, {"id": "07.06.02", "guid": "831c2872-c693-4b39-a887-a561bad'
    'a49bc", "text": "Customize node configuration for AKS node pools", "category": "Operations", "subcat'
    'egory": "Scalability", "waf": "Performance", "severity": "Low", "link": "https://learn.microsoft.com'
    '/azure/aks/custom-node-configuration"}, {"id": "07.06.03", "guid": "faa19bfe-9d55-4d04-a3c4-919ca1b2'
    'd121", "text": "Use the Horizontal Pod Autoscaler when required", "category": "Operations", "subcate'
    'gory": "Scalability", "waf": "Performance", "severity": "Medium", "link": "https://learn.microsoft.c'
    'om/azure/aks/concepts-scale"}, {"id": "07.06.04", "guid": "5ae124ba-34df-4585-bcdc-e9bd3bb0cdb3", "t'
    'ext": "Consider an appropriate node size, not too large or too small", "category": "Operations", "su'
    'bcategory": "Scalability", "waf": "Performance", "severity": "High", "link": "https://blog.cloudtroo'
    'per.net/2020/10/23/which-vm-size-should-i-choose-as-aks-node/"}, {"id": "07.06.05", "guid": "38800e6'
    'a-ae01-40a2-9fbc-ae5a06e5462d", "text": "If more than 5000 nodes are required for scalability then c'
    'onsider using an additional AKS cluster", "category": "Operations", "subcategory": "Scalability", "w'
    'af": "Performance", "severity": "Low", "link": "https://learn.microsoft.com/azure/aks/quotas-skus-re'
    'gions#service-quotas-and-limits"}, {"id": "07.06.06", "guid": "9583c0f6-6083-43f6-aa6b-df7102c901bb"'
    ', "text": "Consider subscribing to EventGrid Events for AKS automation", "category": "Operations", "'
    'subcategory": "Scalability", "waf": "Performance", "severity": "Low", "link": "https://learn.microso'
    'ft.com/azure/event-grid/event-schema-aks"}, {"id": "07.06.07", "guid": "c5016d8c-c6c9-4165-89ae-673e'
    'f0fff19d", "text": "For long running operation on an AKS cluster consider event termination", "categ'
    'ory": "Operations", "subcategory": "Scalability", "waf": "Performance", "severity": "Low", "link": "'
    'https://learn.microsoft.com/azure/aks/manage-abort-operations"}, {"id": "07.06.08", "guid": "c4e3713'
    '3-f186-4ce1-aed9-9f1b32f6e021", "text": "If required consider using Azure Dedicated Hosts for AKS no'
    'des", "category": "Operations", "subcategory": "Scalability", "waf": "Performance", "severity": "Low'
    '", "link": "https://learn.microsoft.com/azure/aks/use-azure-dedicated-hosts"}, {"id": "07.07.01", "g'
    'uid": "24367b33-6971-45b1-952b-eee0b9b588de", "text": "Use ephemeral OS disks", "category": "Operati'
    'ons", "subcategory": "Storage", "waf": "Performance", "severity": "High", "link": "https://learn.mic'
    'rosoft.com/azure/aks/cluster-configuration"}, {"id": "07.07.02", "guid": "f0ce315f-1120-4166-8206-94'
    'f2cf3a4d07", "text": "For non-ephemeral disks, use high IOPS and larger OS disks for the nodes when '
    'running many pods/node since it requires high performance for running multiple pods and will generat'
    'e huge logs with default AKS log rotation thresholds", "category": "Operations", "subcategory": "Sto'
    'rage", "waf": "Performance", "severity": "High", "link": "https://learn.microsoft.com/azure/virtual-'
    'machines/disks-types"}, {"id": "07.07.03", "guid": "39c486ce-d5af-4062-89d5-18bb5fd795db", "text": "'
    'For hyper performance storage option use Ultra Disks on AKS", "category": "Operations", "subcategory'
    '": "Storage", "waf": "Performance", "severity": "Low", "link": "https://learn.microsoft.com/azure/ak'
    's/use-ultra-disks"}, {"id": "07.07.04", "guid": "9f7547c1-747d-4c56-868a-714435bd19dd", "text": "Avo'
    'id keeping state in the cluster, and store data outside (AzStorage, AzSQL, Cosmos, etc)", "category"'
    ': "Operations", "subcategory": "Storage", "waf": "Performance", "severity": "Medium", "link": "https'
    '://learn.microsoft.com/azure/aks/operator-best-practices-multi-region"}, {"id": "07.07.05", "guid": '
    '"24429eb7-2281-4376-85cc-57b4a4b18142", "text": "If using AzFiles Standard, consider AzFiles Premium'
    ' and/or ANF for performance reasons", "category": "Operations", "subcategory": "Storage", "waf": "Pe'
    'rformance", "severity": "Medium", "link": "https://learn.microsoft.com/azure/aks/operator-best-pract'
    'ices-storage"}, {"id": "07.07.06", "guid": "83958a8c-2689-4b32-ab57-cfc64546135a", "text": "If using'
    ' Azure Disks and AZs, consider having nodepools within a zone for LRS disk with VolumeBindingMode:Wa'
    'itForFirstConsumer for provisioning storage in right zone or use ZRS disk for nodepools spanning mul'
    'tiple zones", "category": "Operations", "subcategory": "Storage", "waf": "Performance", "severity": '
    '"Medium", "link": "https://learn.microsoft.com/azure/aks/availability-zones#azure-disk-availability-'
    'zone-support"}], "license_text": "MIT License\\n\\nCopyright (c) Microsoft Corporation.\\n\\nPermission '
    'is hereby granted, free of charge, to any person obtaining a copy\\nof this software and associated d'
    'ocumentation files (the \\"Software\\"), to deal\\nin the Software without restriction, including witho'
    'ut limitation the rights\\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\\'
    'ncopies of the Software, and to permit persons to whom the Software is\\nfurnished to do so, subject '
    'to the following conditions:\\n\\nThe above copyright notice and this permission notice shall be inclu'
    'ded in all\\ncopies or substantial portions of the Software.\\n\\nTHE SOFTWARE IS PROVIDED \\"AS IS\\", W'
    'ITHOUT WARRANTY OF ANY KIND, EXPRESS OR\\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MER'
    'CHANTABILITY,\\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\\nAUTHORS '
    'OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\\nLIABILITY, WHETHER IN AN ACTION OF C'
    'ONTRACT, TORT OR OTHERWISE, ARISING FROM,\\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR O'
    'THER DEALINGS IN THE\\nSOFTWARE.\\n"}'
))
HTML_TEMPLATE = (
    '<!doctype html>\n<!--\nTHESIS: A consulting dossier connects a dated AKS assessment to its evidence an'
    'd human action plan; it refuses the generic SaaS card dashboard.\nOWN-WORLD: Fluent 2 pages framed by'
    ' Microsoft-blue chrome and cover, a four-color brand ribbon, Segoe UI and ruled tables; color never '
    'substitutes for evidence.\nSTORY: Establish provenance, read the local index beside coverage, follow '
    'category/namespace/workload evidence, then decide on qualified actions.\nFIRST VIEWPORT: Quiet conten'
    'ts rail; a wide document cover with client plaque, cluster identity and UTC reference; executive rea'
    'ding follows, not a hero metric.\nFORM: User-pinned Read-mode dossier with operating filters. WARA na'
    'rrative and Polaris wayfinding are references, not scoring engines. No tournament or random seed.\n--'
    '>\n<html lang="es-ES">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-widt'
    'h,initial-scale=1">\n<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-s'
    "rc 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; f"
    'orm-action \'none\'; object-src \'none\'">\n<meta name="color-scheme" content="light dark">\n<title>Revisi'
    'ón AKS | Informe de configuración</title>\n<style>\n:root {\n  color-scheme: light;\n  --canvas: #f5f5f5'
    '; --surface: #ffffff; --subtle: #fafafa; --wash: #f0f0f0;\n  --ink: #242424; --muted: #616161; --line'
    ': #d1d1d1; --field-line: #8a8a8a;\n  --accent: #0f6cbd; --accent-hover: #115ea3; --accent-wash: #eff6'
    'fc; --on-accent: #ffffff;\n  --critical: #a4262c; --critical-wash: #fde7e9; --high: #8a3700; --high-w'
    'ash: #fff1e5;\n  --medium: #795b00; --medium-wash: #fff8d6; --low: #616161; --low-wash: #f0f0f0;\n  --'
    'chrome: #0f6cbd; --chrome-hover: #0b5797; --on-chrome: #ffffff; --chrome-line: #d6e7f5;\n  --cover: #'
    'eff6fc; --cover-ink: #074b83; --cover-muted: #39576e;\n  --nav: #eaf3fb; --nav-ink: #074b83; --nav-mu'
    'ted: #3d5870;\n  --brand-red: #f25022; --brand-green: #7fba00; --brand-blue: #00a4ef; --brand-yellow:'
    ' #ffb900;\n  --radius: 4px; --space: 16px;\n}\n:root[data-theme="dark"] {\n  color-scheme: dark;\n  --can'
    'vas: #171717; --surface: #1f1f1f; --subtle: #292929; --wash: #333333;\n  --ink: #f5f5f5; --muted: #c2'
    'c2c2; --line: #525252; --field-line: #949494;\n  --accent: #78b5e6; --accent-hover: #a3d1f2; --accent'
    '-wash: #203448; --on-accent: #171717;\n  --critical: #ffb3b8; --critical-wash: #442326; --high: #ffb9'
    '8f; --high-wash: #3b2e25;\n  --medium: #f5d875; --medium-wash: #383323; --low: #d6d6d6; --low-wash: #'
    '333333;\n  --chrome: #073e63; --chrome-hover: #0d507d; --on-chrome: #f5f5f5; --chrome-line: #91bfdf;\n'
    '  --cover: #17334c; --cover-ink: #c5e4ff; --cover-muted: #c2dff1;\n  --nav: #162c3e; --nav-ink: #c5e4'
    'ff; --nav-muted: #bdd8ef;\n}\n* { box-sizing: border-box; }\nhtml { scroll-behavior: auto; scroll-paddi'
    'ng-top: 24px; }\nbody { margin: 0; background: var(--canvas); color: var(--ink); font: 16px/1.5 "Sego'
    'e UI", system-ui, sans-serif; overflow-wrap: anywhere; }\n[hidden] { display: none !important; }\nbutt'
    'on, input, select { font: inherit; }\nbutton, input, select, summary, a { touch-action: manipulation;'
    ' }\nbutton, select, input { min-height: 44px; border: 1px solid var(--field-line); border-radius: var'
    '(--radius); background: var(--surface); color: var(--ink); padding: 8px 12px; }\nbutton { cursor: poi'
    'nter; font-weight: 600; }\nbutton:hover:not(:disabled), select:hover, input:hover { background: var(-'
    '-wash); }\nbutton:active:not(:disabled) { background: var(--line); }\nbutton:disabled { opacity: .55; '
    'cursor: not-allowed; }\nbutton.primary { background: var(--accent); color: var(--on-accent); border-c'
    'olor: var(--accent); }\nbutton.primary:hover:not(:disabled) { background: var(--accent-hover); border'
    '-color: var(--accent-hover); }\ninput::placeholder { color: var(--muted); opacity: 1; }\na { color: va'
    'r(--accent); text-underline-offset: 3px; }\na:hover { text-decoration-thickness: 2px; }\n:focus-visibl'
    'e { outline: 3px solid var(--accent); outline-offset: 3px; }\n.skip { position: absolute; left: 16px;'
    ' top: -100px; z-index: 5; padding: 12px 16px; background: var(--surface); border: 2px solid var(--ac'
    'cent); }\n.skip:focus { top: 12px; }\n.toolbar { border-bottom: 1px solid var(--chrome-line); backgrou'
    'nd: var(--chrome); color: var(--on-chrome); }\n.toolbar-inner { max-width: 1440px; margin: auto; disp'
    'lay: flex; gap: 16px; align-items: center; justify-content: space-between; padding: 16px 32px; }\n.to'
    'olbar-title { font-size: 18px; font-weight: 600; }\n.toolbar-controls { display: flex; flex-wrap: wra'
    'p; gap: 10px; align-items: center; }\n.theme-field { display: flex; gap: 8px; align-items: center; fo'
    'nt-size: 14px; }\n.toolbar button, .toolbar select { font-size: 14px; }\n.toolbar select { color-schem'
    'e: only light; background: #ffffff; color: #074b83; border-color: var(--chrome-line); }\n.toolbar but'
    'ton { background: transparent; color: var(--on-chrome); border-color: var(--chrome-line); }\n.toolbar'
    ' button:hover:not(:disabled) { background: var(--chrome-hover); }\n.toolbar button.primary { backgrou'
    'nd: #ffffff; color: #074b83; border-color: #ffffff; }\n.toolbar button.primary:hover:not(:disabled) {'
    ' background: #eaf3fb; border-color: #eaf3fb; }\n.toolbar :focus-visible { outline-color: var(--on-chr'
    'ome); }\n.brand-ribbon { display: flex; height: 4px; }\n.brand-ribbon span { flex: 1; }\n.brand-ribbon '
    '.red { background: var(--brand-red); }\n.brand-ribbon .green { background: var(--brand-green); }\n.bra'
    'nd-ribbon .blue { background: var(--brand-blue); }\n.brand-ribbon .yellow { background: var(--brand-y'
    'ellow); }\n.theme-note { max-width: 1376px; margin: 0 auto; padding: 0 32px 12px; font-size: 14px; }\n'
    '.toolbar .theme-note { color: var(--on-chrome); }\n.shell { max-width: 1440px; margin: auto; display:'
    ' grid; grid-template-columns: 218px minmax(0, 1fr); }\n.contents { padding: 40px 24px 24px 32px; alig'
    'n-self: start; position: sticky; top: 0; background: var(--nav); min-height: calc(100vh - 80px); }\n.'
    'contents h2 { font-size: 16px; margin: 0 0 16px; color: var(--nav-ink); }\n.contents ol { padding: 0;'
    ' margin: 0; list-style: none; display: grid; gap: 4px; }\n.contents a { display: block; font-size: 14'
    'px; min-height: 44px; padding: 10px 8px; border-radius: var(--radius); color: var(--nav-ink); text-d'
    'ecoration: none; }\n.contents a:hover, .contents a:focus-visible { background: var(--accent-wash); co'
    'lor: var(--accent); }\n.contents .contents-note { margin-top: 32px; font-size: 13px; color: var(--nav'
    '-muted); }\n.mobile-contents { display: none; }\n.document { min-width: 0; background: var(--surface);'
    ' border-left: 1px solid var(--line); padding: 48px 48px 32px; }\n.opening-state { max-width: 900px; m'
    'argin: 48px auto; padding: 32px; border: 1px solid var(--line); background: var(--surface); }\n.openi'
    'ng-state h1 { font-size: 28px; }\nh1 { margin: 0 0 20px; font-size: 40px; line-height: 1.3; font-weig'
    'ht: 600; letter-spacing: -.02em; max-width: 22ch; }\nh2 { margin: 0 0 16px; font-size: 28px; line-hei'
    'ght: 1.3; font-weight: 600; letter-spacing: -.01em; color: var(--cover-ink); }\nh3 { margin: 0 0 12px'
    '; font-size: 20px; line-height: 1.35; font-weight: 600; }\nh4 { margin: 0 0 10px; font-size: 16px; fo'
    'nt-weight: 600; }\np { margin: 0 0 16px; }\n.prose { max-width: 72ch; }\n.muted, .qualification { color'
    ': var(--muted); }\n.qualification { font-size: 14px; max-width: 75ch; }\n.cover-head { display: grid; '
    'grid-template-columns: minmax(0, 1fr) 220px; gap: 32px; align-items: start; background: var(--cover)'
    '; border: 1px solid var(--line); padding: 24px; border-radius: var(--radius); }\n.cover-head h1, .cov'
    'er-head .client-name { color: var(--cover-ink); }\n.cover-head .introduction { max-width: 62ch; font-'
    'size: 18px; color: var(--cover-muted); margin: 0; }\n.brand { min-width: 0; display: grid; justify-it'
    'ems: start; gap: 12px; }\n.brand-plaque { color-scheme: only light; background: #ffffff; color: #4242'
    '42; border: 1px solid #d1d1d1; width: 220px; max-width: 100%; min-height: 76px; padding: 16px; displ'
    'ay: flex; align-items: center; justify-content: center; }\n.brand-plaque img { max-width: 100%; width'
    ': auto; height: auto; max-height: 56px; object-fit: contain; display: block; }\n.brand-placeholder { '
    'color: #424242; font-size: 20px; font-weight: 600; letter-spacing: .04em; overflow-wrap: anywhere; }'
    '\n.client-name { margin: 0; font-size: 18px; font-weight: 600; overflow-wrap: anywhere; }\n.provenance'
    ' { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 32px; }\n.label { display: inline-block; fon'
    't-size: 13px; font-weight: 600; line-height: 1.5; border: 1px solid var(--line); background: var(--s'
    'ubtle); padding: 5px 9px; border-radius: var(--radius); }\n.cover-facts { display: grid; grid-templat'
    'e-columns: repeat(3, minmax(0, 1fr)); gap: 24px 28px; padding: 28px 0; margin: 0; }\n.cover-facts div'
    ' { min-width: 0; }\ndt { font-size: 13px; color: var(--muted); margin-bottom: 4px; }\ndd { margin: 0; '
    'overflow-wrap: anywhere; }\n.cover-facts dd { font-size: 16px; font-weight: 600; }\n.cover-id { color:'
    ' var(--muted); font-size: 13px; overflow-wrap: anywhere; padding-top: 12px; border-top: 1px solid va'
    'r(--line); }\n.cover-id strong { font-weight: 600; }\n.snapshot-note { padding: 16px 20px; background:'
    ' var(--subtle); border: 1px solid var(--line); margin: 24px 0 0; font-size: 14px; }\n.chapter { paddi'
    'ng-top: 56px; margin-top: 48px; border-top: 1px solid var(--line); min-width: 0; }\n.chapter-head { d'
    'isplay: flex; gap: 16px; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; m'
    'argin-bottom: 20px; }\n.chapter-head h2 { margin-bottom: 8px; }\n.chapter-head p { margin: 0; }\n.chapt'
    'er-head button { font-size: 14px; }\n.score-layout { display: grid; grid-template-columns: 225px minm'
    'ax(0, 1fr); gap: 36px; margin: 28px 0; }\n.global-index { padding-right: 28px; border-right: 1px soli'
    'd var(--line); }\n.global-index h3 { font-size: 18px; }\n.score-value { display: block; font-size: 44p'
    'x; line-height: 1.2; font-weight: 600; font-variant-numeric: tabular-nums; margin: 16px 0 12px; }\n.s'
    'core-value.unknown { font-size: 28px; }\n.global-index p { font-size: 14px; margin-bottom: 10px; }\n.s'
    'core-row { padding: 0 0 16px; margin: 0 0 16px; border-bottom: 1px solid var(--line); }\n.score-row:l'
    'ast-child { border-bottom: 0; margin-bottom: 0; padding-bottom: 0; }\n.score-row-head { display: flex'
    '; justify-content: space-between; gap: 12px; font-size: 15px; font-weight: 600; margin-bottom: 8px; '
    '}\n.score-number { font-variant-numeric: tabular-nums; white-space: nowrap; }\n.score-track { height: '
    '8px; background: var(--wash); border-radius: 2px; overflow: hidden; border: 1px solid var(--line); }'
    '\n.score-fill { background: var(--accent); height: 100%; }\n.score-caption { margin: 8px 0 0; font-siz'
    'e: 13px; color: var(--muted); }\n.pillar-scores { display: grid; grid-template-columns: repeat(2, min'
    'max(0, 1fr)); column-gap: 32px; margin: 20px 0 28px; }\n.pillar-scores .score-row { margin-bottom: 20'
    'px; padding-bottom: 12px; }\n.coverage-note { padding: 16px 0; border-top: 1px solid var(--line); bor'
    'der-bottom: 1px solid var(--line); margin: 24px 0; }\n.coverage-note p { margin: 0 0 6px; font-size: '
    '14px; }\n.coverage-note p:last-child { margin-bottom: 0; }\n.assessment-chart { margin: 28px 0 32px; d'
    'isplay: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 32px; align-items: center; padding: '
    '24px; background: var(--accent-wash); border: 1px solid var(--line); }\n.assessment-chart h3 { margin'
    '-bottom: 8px; }\n.assessment-chart svg { width: 200px; max-width: 100%; height: auto; display: block;'
    ' margin: auto; }\n.assessment-chart .chart-total { fill: var(--ink); font: 600 34px "Segoe UI", syste'
    'm-ui, sans-serif; }\n.assessment-chart .chart-unit { fill: var(--muted); font: 14px "Segoe UI", syste'
    'm-ui, sans-serif; }\n.assessment-chart figcaption { grid-column: 1 / -1; margin: 0; font-size: 14px; '
    '}\n.assessment-chart table { background: var(--surface); }\n.assessment-chart th, .assessment-chart td'
    ' { padding: 9px 12px; }\n.status-swatch { display: inline-block; width: 12px; height: 12px; margin-ri'
    'ght: 8px; border: 1px solid var(--ink); vertical-align: -.05em; }\n.status-swatch.passed { background'
    ': var(--accent); }\n.status-swatch.failed { background: var(--high); }\n.status-swatch.not_evaluated {'
    ' background: var(--field-line); }\n.status-swatch.not_applicable { background: var(--surface); }\n.cov'
    'erage-number { display: inline; font-size: 24px; font-weight: 600; color: var(--cover-ink); font-var'
    'iant-numeric: tabular-nums; }\n.cluster-overview { margin: 0 0 32px; padding: 24px; border: 1px solid'
    ' var(--line); background: var(--subtle); }\n.cluster-overview h2 { font-size: 24px; }\n.cluster-picker'
    ' { max-width: 540px; margin: 20px 0; }\n.cluster-error { padding: 24px; border: 1px solid var(--criti'
    'cal); background: var(--critical-wash); margin-bottom: 32px; }\n.cluster-error h2 { color: var(--crit'
    'ical); }\n.severity-distribution { margin: 28px 0; }\n.distribution-track { display: flex; height: 12p'
    'x; background: var(--wash); border: 1px solid var(--line); border-radius: 2px; overflow: hidden; mar'
    'gin: 14px 0; }\n.distribution-segment { height: 100%; }\n.distribution-segment.critical { background: '
    'var(--critical); }\n.distribution-segment.high { background: var(--high); }\n.distribution-segment.med'
    'ium { background: var(--medium); }\n.distribution-segment.low { background: var(--low); }\n.distributi'
    'on-legend { display: flex; flex-wrap: wrap; gap: 12px 24px; font-size: 14px; }\n.severity { display: '
    'inline-block; font-size: 13px; font-weight: 600; line-height: 1.5; border: 1px solid currentColor; b'
    'order-radius: var(--radius); padding: 3px 8px; }\n.severity.critical { color: var(--critical); backgr'
    'ound: var(--critical-wash); }\n.severity.high { color: var(--high); background: var(--high-wash); }\n.'
    'severity.medium { color: var(--medium); background: var(--medium-wash); }\n.severity.low { color: var'
    '(--low); background: var(--low-wash); }\n.priority-list { margin: 0; padding-left: 24px; }\n.priority-'
    'list li { padding: 12px 0; }\n.priority-list strong { display: block; font-weight: 600; margin-bottom'
    ': 4px; }\n.priority-list p { margin: 4px 0 0; font-size: 14px; }\n.executive-messages { padding: 0; ma'
    'rgin: 28px 0 0; list-style: none; counter-reset: message; }\n.executive-messages li { padding: 16px 0'
    '; border-top: 1px solid var(--line); max-width: 75ch; }\n.filter-section { padding: 28px 0; margin-to'
    'p: 40px; border-top: 1px solid var(--line); }\n.filter-section h2 { font-size: 20px; margin-bottom: 8'
    'px; }\n.filters { display: grid; grid-template-columns: minmax(0, 1.4fr) repeat(3, minmax(0, 1fr)); g'
    'ap: 16px; margin-top: 20px; }\n.field { display: grid; gap: 6px; min-width: 0; }\n.field label { font-'
    'size: 14px; font-weight: 600; }\n.field input, .field select { width: 100%; min-width: 0; font-size: '
    '14px; }\n.extra-filters { margin: 16px 0; }\n.extra-filters .filter-extra-grid { display: grid; grid-t'
    'emplate-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-top: 16px; }\n.filter-actions { display'
    ': flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 16px; }\n.filter-actions button '
    '{ font-size: 14px; }\n.scope-path { margin: 20px 0 12px; display: flex; flex-wrap: wrap; gap: 4px 8px'
    '; align-items: center; font-size: 14px; }\n.scope-path button { border: 0; padding: 6px 8px; color: v'
    'ar(--accent); background: transparent; min-height: 40px; font-size: 14px; }\n.scope-path button:hover'
    ' { background: var(--accent-wash); }\n.scope-path .scope-part { overflow-wrap: anywhere; min-width: 0'
    '; max-width: 100%; }\n.filter-count { margin: 0; font-size: 14px; }\n.inventory-counts { display: flex'
    '; flex-wrap: wrap; gap: 12px 28px; padding: 16px 0; border-top: 1px solid var(--line); border-bottom'
    ': 1px solid var(--line); margin-bottom: 28px; font-size: 14px; }\n.inventory-counts span { overflow-w'
    'rap: anywhere; }\n.inventory-counts b { font-weight: 600; font-variant-numeric: tabular-nums; }\n.inve'
    'ntory-section { margin-top: 32px; min-width: 0; }\n.inventory-section h3 { margin-bottom: 12px; }\n.ta'
    'ble-scroll { overflow: auto; max-width: 100%; border: 1px solid var(--line); border-radius: var(--ra'
    'dius); }\ntable { width: 100%; border-collapse: collapse; font-size: 14px; }\n.wide-table { min-width:'
    ' 680px; }\nth, td { padding: 12px 14px; border-bottom: 1px solid var(--line); vertical-align: top; te'
    'xt-align: left; }\nth { font-weight: 600; background: var(--subtle); color: var(--ink); }\ntbody tr:la'
    'st-child td { border-bottom: 0; }\ntd { overflow-wrap: anywhere; max-width: 340px; }\ntd strong { font'
    '-weight: 600; }\ncaption { text-align: left; padding: 12px 14px; font-size: 13px; color: var(--muted)'
    '; border-bottom: 1px solid var(--line); }\n.cell-note { display: block; color: var(--muted); font-siz'
    'e: 12px; margin-top: 4px; }\n.text-button { border: 0; padding: 4px 0; border-radius: 0; color: var(-'
    '-accent); background: transparent; min-height: 32px; text-align: left; font-size: inherit; overflow-'
    'wrap: anywhere; }\n.text-button:hover:not(:disabled) { background: transparent; text-decoration: unde'
    'rline; }\n.table-footer { display: flex; gap: 16px; align-items: center; justify-content: space-betwe'
    'en; padding: 12px 0; font-size: 13px; color: var(--muted); flex-wrap: wrap; }\n.table-footer button {'
    ' font-size: 13px; min-height: 40px; }\n.empty { padding: 20px; border: 1px solid var(--line); backgro'
    'und: var(--subtle); font-size: 14px; margin: 0; }\ndetails { min-width: 0; }\nsummary { cursor: pointe'
    'r; font-weight: 600; min-height: 44px; padding: 10px 0; overflow-wrap: anywhere; }\nsummary:hover { c'
    'olor: var(--accent); }\n.receipt { margin-top: 8px; }\n.receipt summary { font-size: 13px; min-height:'
    ' 32px; padding: 6px 0; }\n.receipt-content { margin-top: 10px; }\n.facts { display: grid; grid-templat'
    'e-columns: minmax(110px, .35fr) minmax(0, 1fr); gap: 10px 20px; margin: 16px 0; font-size: 14px; }\n.'
    'facts dt { font-size: 13px; margin: 0; }\n.facts dd { min-width: 0; }\n.json, .command { margin: 12px '
    '0 0; padding: 12px; font: 12px/1.5 Consolas, ui-monospace, monospace; background: var(--subtle); bor'
    'der: 1px solid var(--line); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 28rem; overf'
    'low: auto; }\n.technical-inventory { margin-top: 28px; border-top: 1px solid var(--line); }\n.findings'
    '-group { margin-top: 28px; }\n.finding { border-top: 1px solid var(--line); }\n.finding:last-child { b'
    'order-bottom: 1px solid var(--line); }\n.finding summary { padding: 18px 0; font-weight: 400; }\n.find'
    'ing-title { font-weight: 600; font-size: 16px; margin-left: 10px; }\n.finding-reference { display: bl'
    'ock; font-size: 13px; color: var(--muted); margin-top: 8px; overflow-wrap: anywhere; }\n.finding-body'
    ' { padding: 0 0 24px; }\n.finding-body h4 { margin-top: 24px; }\n.evidence-list { margin: 0; padding-l'
    'eft: 24px; }\n.evidence-list li { padding: 12px 0; }\n.evidence-list .facts { margin-top: 0; }\n.eviden'
    'ce-list pre { margin-top: 8px; }\n.checks-disclosure { margin-top: 32px; padding-top: 12px; border-to'
    'p: 1px solid var(--line); }\n.checks-disclosure > summary { font-size: 18px; }\n.finops-baseline { mar'
    'gin: 24px 0; border: 1px solid var(--line); }\n.finops-values { display: grid; grid-template-columns:'
    ' minmax(0, 1fr) minmax(0, 1.1fr); }\n.finops-value { padding: 24px; background: var(--subtle); min-wi'
    'dth: 0; }\n.finops-value.savings { background: var(--accent-wash); border-left: 1px solid var(--line)'
    '; }\n.finops-value h3 { font-size: 18px; }\n.financial-amount { display: block; font-size: 38px; font-'
    'weight: 600; line-height: 1.25; color: var(--cover-ink); font-variant-numeric: tabular-nums; margin:'
    ' 12px 0; }\n.financial-amount.unknown { font-size: 32px; color: var(--muted); }\n.finops-value p:last-'
    'child { margin-bottom: 0; }\n.finops-baseline > .facts { margin: 0; padding: 20px 24px; border-top: 1'
    'px solid var(--line); }\n.finops-baseline > .qualification { margin: 0; padding: 16px 24px; border-to'
    'p: 1px solid var(--line); }\n.finops-notes { padding-left: 24px; margin: 16px 0 24px; max-width: 75ch'
    '; }\n.finops-notes li { padding: 6px 0; }\n.architecture-tabs { display: flex; flex-wrap: wrap; gap: 1'
    '2px; margin: 24px 0; }\n.architecture-tabs button[aria-selected="true"] { background: var(--accent); '
    'border-color: var(--accent); color: var(--on-accent); }\n.architecture-note { padding: 16px 20px; bac'
    'kground: var(--subtle); border: 1px solid var(--line); margin: 0 0 24px; }\n.architecture-note p:last'
    '-child { margin-bottom: 0; }\n.architecture-layout { display: grid; grid-template-columns: minmax(0, '
    '1fr) 250px; gap: 24px; align-items: start; }\n.architecture-map { margin: 0; border: 1px solid var(--'
    'line); background: var(--subtle); padding: 16px; min-width: 0; }\n.architecture-map > svg { display: '
    'block; width: 100%; height: auto; }\n.architecture-map figcaption { padding-top: 12px; font-size: 13p'
    'x; color: var(--muted); }\n.graph-line { fill: none; stroke: var(--accent); stroke-width: 1.5; }\n.gra'
    'ph-box { fill: var(--surface); stroke: var(--field-line); }\n.graph-box.selected { fill: var(--accent'
    '-wash); stroke: var(--accent); stroke-width: 2; }\n.graph-label { fill: var(--ink); font: 600 13px "S'
    'egoe UI", system-ui, sans-serif; }\n.graph-kind { fill: var(--muted); font: 11px "Segoe UI", system-u'
    'i, sans-serif; }\n.graph-node { cursor: pointer; }\n.graph-node:focus-visible { outline: none; }\n.grap'
    'h-node:focus-visible .graph-box { stroke-width: 3; stroke: var(--accent); }\n.architecture-tree { bor'
    'der: 1px solid var(--line); padding: 16px; min-width: 0; max-height: 34rem; overflow: auto; }\n.archi'
    'tecture-tree h3 { font-size: 18px; }\n.architecture-tree summary { font-size: 14px; }\n.architecture-t'
    'ree ul { list-style: none; padding: 0; margin: 0 0 12px; }\n.architecture-tree button { display: bloc'
    'k; width: 100%; padding: 8px; min-height: 40px; text-align: left; font-size: 13px; font-weight: 400;'
    ' border: 0; background: transparent; overflow-wrap: anywhere; }\n.architecture-tree button[aria-press'
    'ed="true"] { background: var(--accent-wash); color: var(--accent); font-weight: 600; }\n.architecture'
    '-tree button:hover { background: var(--wash); }\n.architecture-controls { display: grid; grid-templat'
    'e-columns: minmax(0, .7fr) minmax(0, 1.3fr); gap: 16px; margin: 20px 0; }\n.architecture-properties {'
    ' margin-top: 24px; }\n.architecture-properties > h3 { color: var(--cover-ink); }\n.architecture-proper'
    'ties .facts { border-top: 1px solid var(--line); padding-top: 16px; }\n.architecture-product { displa'
    'y: flex; align-items: center; gap: 12px; min-width: 0; }\n.architecture-product img { width: 40px; he'
    'ight: 40px; flex: 0 0 40px; object-fit: contain; }\n.architecture-product strong { font-size: 15px; }'
    '\n.architecture-product .cell-note { font-size: 13px; }\n.reference-diagram { margin: 24px 0; }\n.refer'
    'ence-entry { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px; padding: 20'
    'px; border: 1px dashed var(--field-line); }\n.reference-entry > div + div { border-left: 1px solid va'
    'r(--line); padding-left: 24px; }\n.reference-link { margin: 0; padding: 12px; text-align: center; fon'
    't-size: 14px; color: var(--accent); }\n.reference-network { padding: 24px; border: 1px dashed var(--a'
    'ccent); background: var(--accent-wash); }\n.reference-network > h3 { margin-bottom: 20px; }\n.referenc'
    'e-lanes { display: grid; grid-template-columns: minmax(0, .7fr) minmax(0, 1.3fr); gap: 24px; }\n.refe'
    'rence-lane { background: var(--surface); border: 1px solid var(--line); padding: 20px; min-width: 0;'
    ' }\n.reference-lane h4 { margin-bottom: 16px; }\n.reference-workloads { display: grid; grid-template-c'
    'olumns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 20px; }\n.reference-workloads p { border-to'
    'p: 1px solid var(--line); padding-top: 12px; font-size: 14px; margin: 0; }\n.reference-platform { dis'
    'play: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 24px; padding: 24px 0; border-bot'
    'tom: 1px solid var(--line); }\n.reference-diagram figcaption { font-size: 14px; margin-top: 16px; }\n.'
    'reference-decisions { margin-top: 32px; }\n.action-bucket { margin-top: 32px; }\n.action-bucket h3 { m'
    'argin-bottom: 8px; }\n.action-bucket > p { font-size: 14px; }\n.source-errors { margin: 8px 0 0; paddi'
    'ng-left: 20px; font-size: 13px; }\n.method-block { margin-top: 32px; }\n.method-block .qualification {'
    ' margin-top: 12px; }\n.query { padding: 16px 0; border-top: 1px solid var(--line); }\n.query h4 { over'
    'flow-wrap: anywhere; }\n.source-url { overflow-wrap: anywhere; font-size: 13px; }\n.report-footer { ma'
    'rgin-top: 56px; padding-top: 24px; border-top: 1px solid var(--line); font-size: 13px; color: var(--'
    'muted); }\n.report-footer p { margin-bottom: 8px; }\n.sr-only { position: absolute; width: 1px; height'
    ': 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; bord'
    'er: 0; }\n.print-only { display: none; }\n@media (max-width: 1180px) {\n  .shell { grid-template-column'
    's: 182px minmax(0, 1fr); }\n  .contents { padding-left: 20px; padding-right: 16px; }\n  .document { pa'
    'dding: 36px 32px; }\n  .cover-head { grid-template-columns: minmax(0, 1fr) 180px; gap: 24px; }\n  .bra'
    'nd-plaque { width: 180px; }\n  h1 { font-size: 32px; }\n  .filters { grid-template-columns: repeat(2, '
    'minmax(0, 1fr)); }\n  .architecture-layout { grid-template-columns: minmax(0, 1fr); }\n  .architecture'
    '-tree { max-height: 20rem; }\n  .reference-platform { grid-template-columns: repeat(2, minmax(0, 1fr)'
    '); }\n}\n@media (max-width: 900px) {\n  .shell { display: block; }\n  .contents { display: none; }\n  .mo'
    'bile-contents { display: flex; align-items: center; gap: 12px; padding: 16px 24px; border-bottom: 1p'
    'x solid var(--line); }\n  .mobile-contents label { font-size: 14px; font-weight: 600; }\n  .mobile-con'
    'tents select { min-width: 0; flex: 1; font-size: 14px; }\n  .document { border-left: 0; padding: 36px'
    ' 32px; }\n}\n@media (max-width: 600px) {\n  .toolbar-inner { padding: 16px; display: block; }\n  .toolba'
    'r-title { margin-bottom: 12px; display: block; }\n  .toolbar-controls { gap: 8px; }\n  .theme-field { '
    'width: 100%; display: grid; grid-template-columns: 56px minmax(0, 1fr); }\n  .toolbar button { flex: '
    '1; min-width: 0; }\n  .theme-note { padding: 0 16px 12px; }\n  .mobile-contents { padding: 12px 16px; '
    '}\n  .document { padding: 28px 16px 24px; }\n  .opening-state { margin: 20px 16px; padding: 20px; }\n  '
    '.cover-head { grid-template-columns: 1fr; gap: 24px; padding: 16px; }\n  h1 { font-size: 28px; line-h'
    'eight: 1.3; max-width: 23ch; margin-bottom: 16px; }\n  h2 { font-size: 24px; }\n  h3 { font-size: 18px'
    '; }\n  .cover-head .introduction { font-size: 16px; }\n  .brand { grid-template-columns: minmax(0, 150'
    'px) minmax(0, 1fr); align-items: center; gap: 16px; }\n  .brand-plaque { width: 150px; min-height: 64'
    'px; padding: 12px; }\n  .brand-plaque img { max-height: 40px; }\n  .brand-placeholder { font-size: 16p'
    'x; }\n  .client-name { font-size: 16px; }\n  .provenance { margin-top: 24px; }\n  .cover-facts { grid-t'
    'emplate-columns: repeat(2, minmax(0, 1fr)); gap: 20px 16px; padding: 24px 0; }\n  .cover-facts dd { f'
    'ont-size: 14px; }\n  .cover-facts .reference-fact { grid-column: 1 / -1; }\n  .snapshot-note { padding'
    ': 16px; }\n  .chapter { padding-top: 32px; margin-top: 32px; }\n  .chapter-head { margin-bottom: 16px;'
    ' gap: 12px; }\n  .chapter-head button { width: 100%; }\n  .score-layout { grid-template-columns: 1fr; '
    'gap: 24px; margin: 24px 0; }\n  .global-index { padding: 0 0 20px; border-right: 0; border-bottom: 1p'
    'x solid var(--line); }\n  .score-value { font-size: 36px; margin: 12px 0; }\n  .score-value.unknown { '
    'font-size: 24px; }\n  .pillar-scores { grid-template-columns: 1fr; }\n  .score-row-head { font-size: 1'
    '4px; }\n  .filters, .extra-filters .filter-extra-grid { grid-template-columns: 1fr; gap: 12px; }\n  .f'
    'ilter-actions button { width: 100%; }\n  .filter-section { padding: 24px 0; margin-top: 28px; }\n  .fa'
    'cts { grid-template-columns: 1fr; gap: 4px; }\n  .facts dd { margin-bottom: 10px; }\n  .inventory-sect'
    'ion { margin-top: 24px; }\n  .table-footer { gap: 10px; }\n  .finding-title { display: block; margin: '
    '10px 0 0; }\n  .evidence-list { padding-left: 20px; }\n  .distribution-legend { gap: 10px 16px; }\n  .a'
    'ssessment-chart { grid-template-columns: 1fr; gap: 20px; padding: 16px; margin: 24px 0; }\n  .assessm'
    'ent-chart svg { width: 180px; }\n  .assessment-chart figcaption { grid-column: auto; }\n  .cluster-ove'
    'rview, .cluster-error { padding: 16px; }\n  .finops-values, .reference-entry, .reference-lanes, .refe'
    'rence-platform, .architecture-controls { grid-template-columns: 1fr; }\n  .finops-value { padding: 20'
    'px; }\n  .finops-value.savings { border-left: 0; border-top: 1px solid var(--line); }\n  .finops-basel'
    'ine > .facts { padding: 16px 20px; }\n  .financial-amount { font-size: 32px; }\n  .financial-amount.un'
    'known { font-size: 28px; }\n  .architecture-tabs button { width: 100%; }\n  .architecture-note, .refer'
    'ence-network, .reference-lane, .reference-entry { padding: 16px; }\n  .reference-entry > div + div { '
    'border-left: 0; border-top: 1px solid var(--line); padding-left: 0; padding-top: 20px; }\n  .referenc'
    'e-workloads { grid-template-columns: 1fr; }\n  .architecture-tree { max-height: 24rem; }\n  .architect'
    'ure-map { overflow: auto; }\n  .architecture-map > svg { min-width: 640px; }\n  .report-footer { margi'
    'n-top: 36px; }\n}\n@media (prefers-reduced-motion: reduce) {\n  *, *::before, *::after { scroll-behavio'
    'r: auto !important; transition: none !important; animation: none !important; }\n}\n@media (forced-colo'
    'rs: active) {\n  .score-fill, .distribution-segment { forced-color-adjust: none; background: CanvasTe'
    'xt !important; }\n  .score-track, .distribution-track, .severity { border: 1px solid CanvasText; }\n  '
    '.assessment-chart circle { stroke: CanvasText; }\n  .graph-box { fill: Canvas; stroke: CanvasText; }\n'
    '  .graph-label, .graph-kind { fill: CanvasText; }\n  .graph-line { stroke: CanvasText; }\n  :focus-vis'
    'ible { outline-color: Highlight; }\n}\n@page { size: A4; margin: 16mm 14mm; }\n@media print {\n  :root, '
    ':root[data-theme="dark"] {\n    color-scheme: light;\n    --canvas: #ffffff; --surface: #ffffff; --sub'
    'tle: #fafafa; --wash: #f0f0f0;\n    --ink: #242424; --muted: #505050; --line: #b3b3b3; --accent: #0f6'
    'cbd;\n    --critical: #a4262c; --critical-wash: #fde7e9; --high: #8a3700; --high-wash: #fff1e5;\n    -'
    '-medium: #795b00; --medium-wash: #fff8d6; --low: #505050; --low-wash: #f0f0f0;\n    --cover: #eff6fc;'
    ' --cover-ink: #074b83; --cover-muted: #39576e;\n  }\n  body { background: #ffffff; font-size: 10pt; pr'
    'int-color-adjust: exact; -webkit-print-color-adjust: exact; }\n  .no-print, .toolbar, .contents, .mob'
    'ile-contents, .skip, button, input, select, .table-footer { display: none !important; }\n  .print-onl'
    'y { display: block; }\n  .shell { display: block; max-width: none; }\n  .document { border: 0; padding'
    ': 0; }\n  h1 { font-size: 26pt; }\n  h2 { font-size: 20pt; }\n  h3 { font-size: 14pt; }\n  h4 { font-siz'
    'e: 11pt; }\n  h1, h2, h3, h4, summary { break-after: avoid; }\n  .cover-head { grid-template-columns: '
    'minmax(0, 1fr) 170px; }\n  .brand-plaque { width: 170px; }\n  .brand { display: grid; grid-template-co'
    'lumns: 1fr; }\n  .cover-facts { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }\n  .cov'
    'er-facts dd, .cover-head .introduction { font-size: 11pt; }\n  .chapter { break-before: page; padding'
    '-top: 12px; margin-top: 0; }\n  .filter-section { padding: 20px 0 0; margin-top: 24px; }\n  .filter-se'
    'ction > h2, .filter-section > .qualification, .extra-filters, .scope-path { display: none; }\n  .scor'
    'e-layout { grid-template-columns: 180px minmax(0, 1fr); gap: 24px; }\n  .global-index { border-right:'
    ' 1px solid var(--line); border-bottom: 0; padding-right: 20px; }\n  .score-value { font-size: 30pt; }'
    '\n  .score-value.unknown { font-size: 20pt; }\n  .pillar-scores { grid-template-columns: repeat(2, min'
    'max(0, 1fr)); }\n  .score-row-head, .score-caption, .qualification, .facts, table, .snapshot-note { f'
    'ont-size: 9pt; }\n  .table-scroll { overflow: visible; border-radius: 0; }\n  .wide-table { min-width:'
    ' 0; table-layout: fixed; }\n  th, td { padding: 7px 8px; max-width: none; font-size: 8pt; }\n  thead {'
    ' display: table-header-group; }\n  tr { break-inside: avoid; }\n  caption { font-size: 8pt; }\n  .text-'
    'button { display: inline !important; color: var(--ink); font-weight: 600; }\n  details { break-inside'
    ': auto; }\n  details > summary { list-style: none; }\n  details > summary::-webkit-details-marker { di'
    'splay: none; }\n  details:not([open]) > :not(summary) { display: block !important; }\n  .finding summa'
    'ry { padding: 12px 0; }\n  .finding-title { display: inline; margin-left: 8px; font-size: 11pt; }\n  .'
    'finding-body { padding-bottom: 16px; }\n  .facts { grid-template-columns: 120px minmax(0, 1fr); gap: '
    '6px 12px; }\n  .facts dd { margin-bottom: 0; }\n  .json, .command { max-height: none; overflow: visibl'
    'e; font-size: 8pt; padding: 8px; }\n  .priority-list li, .executive-messages li, .score-row, .query {'
    ' break-inside: avoid; }\n  .report-footer { margin-top: 24px; font-size: 8pt; }\n  .assessment-chart {'
    ' grid-template-columns: 150px minmax(0, 1fr); gap: 20px; padding: 16px; break-inside: avoid; }\n  .as'
    'sessment-chart svg { width: 140px; }\n  .assessment-chart table, .assessment-chart figcaption { font-'
    'size: 8pt; }\n  .finops-values { grid-template-columns: repeat(2, minmax(0, 1fr)); }\n  .financial-amo'
    'unt { font-size: 24pt; }\n  .financial-amount.unknown { font-size: 20pt; }\n  .architecture-tabs, .arc'
    'hitecture-controls, .architecture-tree { display: none !important; }\n  .architecture-layout { displa'
    'y: block; }\n  .architecture-panel[hidden] { display: block !important; margin-top: 24px; }\n  .archit'
    'ecture-map { break-inside: avoid; }\n  .architecture-map > svg { min-width: 0; }\n  .reference-entry, '
    '.reference-lanes { grid-template-columns: repeat(2, minmax(0, 1fr)); }\n  .reference-platform { grid-'
    'template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }\n  .architecture-product img { width: 30px;'
    ' height: 30px; flex-basis: 30px; }\n  .reference-network, .reference-lane, .reference-entry { padding'
    ': 16px; }\n  .reference-diagram, .finops-baseline { break-inside: avoid; }\n}\n</style>\n</head>\n<body d'
    'ata-status="loading">\n<a class="skip" href="#main">Saltar al informe</a>\n<header class="toolbar no-p'
    'rint">\n  <div class="toolbar-inner">\n    <span class="toolbar-title">Revisión AKS</span>\n    <div cl'
    'ass="toolbar-controls">\n      <label class="theme-field" for="theme-mode">Tema\n        <select id="t'
    'heme-mode"><option value="auto">Sistema</option><option value="light">Claro</option><option value="d'
    'ark">Oscuro</option></select>\n      </label>\n      <button id="export-json" disabled>JSON completo</'
    'button>\n      <button id="print-report" class="primary" disabled>Imprimir / PDF</button>\n    </div>\n'
    '  </div>\n  <p id="theme-note" class="theme-note qualification" hidden></p>\n  <div class="brand-ribbo'
    'n" aria-hidden="true"><span class="red"></span><span class="green"></span><span class="blue"></span>'
    '<span class="yellow"></span></div>\n</header>\n<div id="opening-state" class="opening-state" role="sta'
    'tus">\n  <h1>Revisión de configuración AKS</h1>\n  <p id="opening-message">Abriendo la instantánea de '
    'evaluación.</p>\n  <p id="opening-detail" class="qualification">Los datos se validan antes de mostrar'
    ' resultados.</p>\n</div>\n<noscript><div class="opening-state">Este informe necesita JavaScript local '
    'para validar y presentar la instantánea. No realiza llamadas de red. Abra el archivo con JavaScript '
    'habilitado.</div></noscript>\n<div id="report-shell" class="shell" hidden>\n  <aside class="contents n'
    'o-print">\n    <h2>Contenido</h2>\n    <nav aria-label="Secciones del informe">\n      <ol>\n        <li'
    '><a href="#cover">Portada y alcance</a></li>\n        <li><a href="#executive">Resumen ejecutivo</a><'
    '/li>\n        <li><a href="#inventory">Inventario observado</a></li>\n        <li><a href="#architectu'
    're">Arquitectura</a></li>\n        <li><a href="#findings">Hallazgos y cobertura</a></li>\n        <li'
    '><a href="#finops">FinOps</a></li>\n        <li><a href="#actions">Plan de acción</a></li>\n        <l'
    'i><a href="#methodology">Método y fuentes</a></li>\n      </ol>\n    </nav>\n    <p class="contents-not'
    'e">Documento de consulta.<br>Las actuaciones requieren revisión y aprobación humanas.</p>\n  </aside>'
    '\n  <main id="main" class="document" tabindex="-1">\n    <nav class="mobile-contents no-print" aria-la'
    'bel="Navegación del informe">\n      <label for="jump-section">Contenido</label>\n      <select id="ju'
    'mp-section">\n        <option value="cover">Portada y alcance</option><option value="executive">Resum'
    'en ejecutivo</option>\n        <option value="inventory">Inventario observado</option><option value="'
    'findings">Hallazgos y cobertura</option>\n        <option value="architecture">Arquitectura</option>\n'
    '        <option value="finops">FinOps</option><option value="actions">Plan de acción</option><option'
    ' value="methodology">Método y fuentes</option>\n      </select>\n    </nav>\n    <section id="cluster-o'
    'verview" class="cluster-overview" aria-labelledby="cluster-overview-title" hidden>\n      <h2 id="clu'
    'ster-overview-title">Clústeres incluidos en esta instantánea</h2>\n      <p class="qualification">La '
    'selección cambia la lectura local; no autentica, recopila ni despliega otro clúster. Una ejecución f'
    'allida conserva su error y no hereda datos de otro clúster.</p>\n      <div id="cluster-aggregate"></'
    'div>\n      <div class="field cluster-picker no-print"><label for="cluster-selector">Clúster de la le'
    'ctura detallada</label><select id="cluster-selector"></select></div>\n      <div id="cluster-runs-tab'
    'le"></div>\n    </section>\n    <section id="cluster-error" class="cluster-error" role="alert" hidden>'
    '</section>\n    <section id="cover" aria-labelledby="report-title">\n      <div class="cover-head">\n  '
    '      <div>\n          <h1 id="report-title">Revisión de arquitectura y configuración AKS</h1>\n      '
    '    <p class="introduction">Buenas prácticas, seguridad, FinOps y optimización del clúster, sus carg'
    'as de trabajo y los recursos asociados.</p>\n        </div>\n        <div class="brand">\n          <di'
    'v id="brand-plaque" class="brand-plaque"></div>\n          <p id="client-name" class="client-name"></'
    'p>\n        </div>\n      </div>\n      <div id="provenance" class="provenance" aria-label="Procedencia'
    ' y entorno"></div>\n      <dl id="cover-facts" class="cover-facts"></dl>\n      <div id="cover-id" cla'
    'ss="cover-id"></div>\n      <div class="snapshot-note">\n        <strong>Instantánea de consulta, no s'
    'upervisión en tiempo real.</strong>\n        La ausencia de datos se indica como <strong>No evaluado<'
    '/strong>; no demuestra que una configuración sea correcta.\n        Los índices son locales y no cons'
    'tituyen una evaluación oficial de Azure, WARA o Polaris.\n      </div>\n    </section>\n    <section id'
    '="executive" class="chapter" aria-labelledby="executive-title">\n      <div class="chapter-head">\n   '
    '     <div><h2 id="executive-title">Resumen ejecutivo</h2><p class="qualification">La conclusión se i'
    'nterpreta junto a la cobertura y los límites de las fuentes.</p></div>\n      </div>\n      <div class'
    '="score-layout">\n        <div id="global-index" class="global-index"></div>\n        <div><h3>Índice '
    'local por área</h3><div id="area-scores"></div></div>\n      </div>\n      <figure id="assessment-char'
    't" class="assessment-chart" aria-labelledby="assessment-chart-title"></figure>\n      <h3>Lectura por'
    ' pilar Well-Architected</h3>\n      <div id="pillar-scores" class="pillar-scores"></div>\n      <div i'
    'd="global-coverage" class="coverage-note"></div>\n      <div id="severity-distribution" class="severi'
    'ty-distribution"></div>\n      <h3 id="critical-title">Prioridades críticas</h3>\n      <div id="criti'
    'cal-findings"></div>\n      <ul id="executive-messages" class="executive-messages"></ul>\n    </sectio'
    'n>\n    <section id="scope-filters" class="filter-section" aria-labelledby="filter-title">\n      <h2 '
    'id="filter-title">Explorar el alcance</h2>\n      <p class="qualification">Clúster → área → namespace'
    ' → carga de trabajo. Los filtros no recalculan los índices del resumen: estos corresponden a la inst'
    'antánea completa.</p>\n      <form id="filters-form" class="no-print">\n        <div class="filters">\n'
    '          <div class="field"><label for="filter-search">Buscar evidencia</label><input id="filter-se'
    'arch" type="search" placeholder="Nombre, ID, comprobación o evidencia" autocomplete="off"></div>\n   '
    '       <div class="field"><label for="filter-area">Área</label><select id="filter-area"><option valu'
    'e="">Todas las áreas</option></select></div>\n          <div class="field"><label for="filter-namespa'
    'ce">Namespace</label><select id="filter-namespace"><option value="">Todos los namespaces</option></s'
    'elect></div>\n          <div class="field"><label for="filter-workload">Carga de trabajo</label><sele'
    'ct id="filter-workload"><option value="">Todas las cargas</option></select></div>\n        </div>\n   '
    '     <details class="extra-filters" data-detail-key="extra-filters">\n          <summary>Más filtros:'
    ' nivel, estado y severidad</summary>\n          <div class="filter-extra-grid">\n            <div clas'
    's="field"><label for="filter-level">Nivel</label><select id="filter-level"><option value="">Todos lo'
    's niveles</option></select></div>\n            <div class="field"><label for="filter-status">Estado d'
    'e la comprobación</label><select id="filter-status"><option value="">Todos los estados</option></sel'
    'ect></div>\n            <div class="field"><label for="filter-severity">Severidad del hallazgo</label'
    '><select id="filter-severity"><option value="">Todas las severidades</option></select></div>\n       '
    '   </div>\n        </details>\n        <div class="filter-actions"><button type="button" id="reset-fil'
    'ters">Restablecer filtros</button><button type="button" id="export-filtered-json">JSON del alcance c'
    'ompleto</button></div>\n      </form>\n      <nav id="scope-path" class="scope-path no-print" aria-lab'
    'el="Ruta de exploración"></nav>\n      <p id="filter-count" class="filter-count" role="status" aria-l'
    'ive="polite" aria-atomic="true"></p>\n      <p id="print-scope" class="print-only"></p>\n    </section'
    '>\n    <section id="inventory" class="chapter" aria-labelledby="inventory-title">\n      <div class="c'
    'hapter-head">\n        <div><h2 id="inventory-title">Inventario observado</h2><p class="qualification'
    '">Recuento de registros incorporados, no certificación de exhaustividad. La fuente y la fecha acompa'
    'ñan a cada recurso.</p></div>\n        <button id="export-inventory" class="no-print">Exportar invent'
    'ario CSV</button>\n      </div>\n      <div id="inventory-counts" class="inventory-counts"></div>\n    '
    '  <div class="inventory-section"><h3 id="node-pools-title">Pools de nodos</h3><div id="node-pools"><'
    '/div></div>\n      <div class="inventory-section"><h3 id="namespaces-title">Namespaces</h3><div id="n'
    'amespaces"></div></div>\n      <div class="inventory-section"><h3 id="workloads-title">Cargas de trab'
    'ajo</h3><div id="workloads"></div></div>\n      <div class="inventory-section"><h3 id="related-resour'
    'ces-title">Recursos asociados</h3><div id="related-resources"></div></div>\n      <details id="techni'
    'cal-inventory" class="technical-inventory" data-detail-key="technical-inventory"><summary>Inventario'
    ' técnico adicional</summary><div id="technical-collections"></div></details>\n    </section>\n    <sec'
    'tion id="architecture" class="chapter" aria-labelledby="architecture-title">\n      <div class="chapt'
    'er-head">\n        <div><h2 id="architecture-title">Arquitectura: lo observado y el diseño de referen'
    'cia</h2><p class="qualification">Las relaciones observadas proceden de IDs y campos concretos. La pr'
    'opuesta Microsoft es una alternativa de diseño, no infraestructura detectada ni desplegada.</p></div'
    '>\n      </div>\n      <div class="architecture-tabs no-print" role="tablist" aria-label="Tipo de arqu'
    'itectura">\n        <button id="architecture-observed-tab" role="tab" aria-selected="true" aria-contr'
    'ols="architecture-observed" tabindex="0">Arquitectura observada</button>\n        <button id="archite'
    'cture-reference-tab" role="tab" aria-selected="false" aria-controls="architecture-reference" tabinde'
    'x="-1">Propuesta de referencia - NO desplegada</button>\n      </div>\n      <div id="architecture-obs'
    'erved" class="architecture-panel" role="tabpanel" aria-labelledby="architecture-observed-tab">\n     '
    '   <div id="architecture-observed-note" class="architecture-note"></div>\n        <div class="archite'
    'cture-controls no-print">\n          <div class="field"><label for="architecture-kind">Tipo de recurs'
    'o</label><select id="architecture-kind"><option value="">Todos los tipos</option></select></div>\n   '
    '       <div class="field"><label for="architecture-node">Recurso / objeto observado</label><select i'
    'd="architecture-node"></select></div>\n        </div>\n        <div class="architecture-layout">\n     '
    '     <div><figure id="architecture-map" class="architecture-map" aria-label="Relaciones confirmadas '
    'del recurso seleccionado"></figure><div id="architecture-properties" class="architecture-properties"'
    '></div></div>\n          <aside id="architecture-tree" class="architecture-tree" aria-label="Árbol de'
    ' recursos observados"></aside>\n        </div>\n        <details class="technical-inventory" data-deta'
    'il-key="architecture-nodes"><summary>Nodos observados e identificación completos</summary><div id="a'
    'rchitecture-nodes"></div></details>\n        <details class="technical-inventory" data-detail-key="ar'
    'chitecture-edges"><summary>Relaciones, campos de unión y evidencia completos</summary><div id="archi'
    'tecture-edges"></div></details>\n        <details class="technical-inventory" data-detail-key="archit'
    'ecture-unknowns"><summary>Relaciones no evaluadas y límites de la topología</summary><div id="archit'
    'ecture-unknowns"></div></details>\n      </div>\n      <div id="architecture-reference" class="archite'
    'cture-panel" role="tabpanel" aria-labelledby="architecture-reference-tab" hidden></div>\n    </sectio'
    'n>\n    <section id="findings" class="chapter" aria-labelledby="findings-title">\n      <div class="ch'
    'apter-head">\n        <div><h2 id="findings-title">Hallazgos y cobertura</h2><p id="findings-count" c'
    'lass="qualification"></p></div>\n        <button id="export-findings" class="no-print">Exportar halla'
    'zgos CSV</button>\n      </div>\n      <p class="qualification">Cada hallazgo procede de una comprobac'
    'ión fallida del modelo. Su severidad no se atribuye a una fuente ausente ni demuestra por sí sola un'
    'a causa raíz o un incumplimiento certificado.</p>\n      <div id="findings-list"></div>\n      <div id'
    '="findings-pagination"></div>\n      <details class="checks-disclosure" data-detail-key="checks-discl'
    'osure">\n        <summary>Comprobaciones evaluadas, no evaluadas y no aplicables</summary>\n        <p'
    ' class="qualification">Incluye también las comprobaciones superadas. No evaluado conserva su motivo;'
    ' no aplicable no entra en el índice.</p>\n        <button id="export-checks" class="no-print">Exporta'
    'r comprobaciones CSV</button>\n        <div id="checks-table" class="inventory-section"></div>\n      '
    '</details>\n    </section>\n    <section id="finops" class="chapter" aria-labelledby="finops-title">\n '
    '     <div class="chapter-head">\n        <div><h2 id="finops-title">FinOps: evidencia antes del ahorr'
    'o</h2><p class="qualification">Utilización, recursos huérfanos, costes, Spot, reservas y autoescalad'
    'o: solo se presentan observaciones y candidatos aportados por el motor.</p></div>\n        <button id'
    '="export-finops" class="no-print">Exportar candidatos CSV</button>\n      </div>\n      <div id="finop'
    's-baseline" class="finops-baseline"></div>\n      <p class="qualification">Un candidato requiere revi'
    'sión de la ventana, el tamaño de muestra y la necesidad de negocio. Si no se aporta una estimación, '
    'el ahorro está <strong>Por estimar / No calculado</strong>, no es cero ni una promesa de retorno.</p'
    '>\n      <ul id="finops-notes" class="finops-notes"></ul>\n      <h3 id="finops-candidates-title">Cand'
    'idatos del alcance seleccionado</h3>\n      <div id="finops-candidates"></div>\n      <details class="'
    'technical-inventory" data-detail-key="finops-receipts"><summary>Registros de coste y utilización inc'
    'orporados</summary><div id="finops-receipts"></div></details>\n    </section>\n    <section id="action'
    's" class="chapter" aria-labelledby="actions-title">\n      <div class="chapter-head">\n        <div><h'
    '2 id="actions-title">Plan de acción para revisión humana</h2><p class="qualification">Horizontes ori'
    'entativos, no compromisos de entrega. El esfuerzo y el impacto requieren validación del responsable;'
    ' no se ejecuta ninguna actuación desde este informe.</p></div>\n        <button id="export-actions" c'
    'lass="no-print">Exportar plan CSV</button>\n      </div>\n      <div id="action-plan"></div>\n    </sec'
    'tion>\n    <section id="methodology" class="chapter" aria-labelledby="methodology-title">\n      <h2 i'
    'd="methodology-title">Método, fuentes y límites de lectura</h2>\n      <div class="method-block">\n   '
    '     <h3>Cómo interpretar el índice local</h3>\n        <p class="prose">Índice local = 100 × comprob'
    'aciones superadas / comprobaciones evaluadas. La cobertura compara las evaluadas con las evaluadas m'
    'ás las no evaluadas. Las no aplicables quedan excluidas de ambos denominadores.</p>\n        <p class'
    '="prose">El motor identifica cada comprobación por un <code>check_id</code> único y no duplica su co'
    'ntribución al índice global al navegar por namespaces. Esta interfaz presenta los índices recibidos,'
    ' sin volver a evaluar controles ni generar recomendaciones.</p>\n        <p class="prose"><strong>No '
    'es una puntuación oficial de Azure Assessment, Azure Well-Architected, WARA o Polaris.</strong> Las '
    'referencias orientan la revisión; no acreditan cumplimiento, ausencia de riesgo, SLA o causa raíz.</'
    'p>\n        <div id="method-scope"></div>\n      </div>\n      <div class="method-block"><h3 id="source'
    's-title">Registro de fuentes</h3><div id="sources-table"></div></div>\n      <details class="technica'
    'l-inventory" data-detail-key="source-data">\n        <summary>Observaciones adicionales conservadas</'
    'summary>\n        <p class="qualification">Recibos adicionales del modelo completo. No se convierten '
    'en nuevas comprobaciones ni se presentan como configuraciones conformes.</p>\n        <div id="source'
    '-data-receipts"></div>\n      </details>\n      <div class="method-block"><h3>Catálogo y referencias f'
    'ijadas</h3><div id="catalog-pins"></div></div>\n      <div class="method-block"><h3>Consultas y límit'
    'es incorporados</h3><div id="queries"></div></div>\n      <div class="method-block"><h3>Limitaciones '
    'declaradas</h3><div id="limitations"></div></div>\n    </section>\n    <footer class="report-footer">\n'
    '      <p id="footer-reference"></p>\n      <p>Archivo autocontenido y de solo lectura. Los datos y la'
    's exportaciones permanecen locales; compartir el archivo requiere revisar las identidades y la evide'
    'ncia que contiene.</p>\n      <p>Las capturas de pruebas de interfaz utilizan datos expresamente simu'
    'lados, no observaciones de un cliente.</p>\n    </footer>\n  </main>\n</div>\n<script type="application/'
    'json" id="report-data">__REPORT_DATA_JSON__</script>\n<script>\n"use strict";\n(() => {\n  const AREA = '
    'new Map([["best_practices", "Buenas prácticas"], ["security", "Seguridad"], ["finops", "FinOps"], ["'
    'optimization", "Optimización"]]);\n  const PILLAR = new Map([["reliability", "Fiabilidad"], ["securit'
    'y", "Seguridad"], ["cost", "Optimización de costes"], ["operations", "Excelencia operativa"], ["perf'
    'ormance", "Eficiencia del rendimiento"]]);\n  const LEVEL = new Map([["cluster", "Clúster"], ["worklo'
    'ad", "Carga de trabajo"], ["resource", "Recurso asociado"]]);\n  const STATUS = new Map([["passed", "'
    'Superada"], ["failed", "Hallazgo"], ["not_evaluated", "No evaluado"], ["not_applicable", "No aplicab'
    'le"]]);\n  const SEVERITY = new Map([["critical", "Crítica"], ["high", "Alta"], ["medium", "Media"], '
    '["low", "Baja"]]);\n  const EFFORT = new Map([["low", "Bajo"], ["medium", "Medio"], ["high", "Alto"]]'
    ');\n  const SOURCE_STATUS = new Map([["complete", "Completa"], ["partial", "Parcial"], ["denied", "Ac'
    'ceso denegado"], ["error", "Error de lectura"], ["not_configured", "No configurada"], ["not_applicab'
    'le", "No aplicable"]]);\n  const COLLECTION = new Map([["node_pools", "Pools de nodos"], ["namespaces'
    '", "Namespaces"], ["workloads", "Cargas de trabajo"], ["related_resources", "Recursos asociados"], ['
    '"nodes", "Nodos"], ["pods", "Pods"], ["services", "Servicios"], ["storage_classes", "Clases de almac'
    'enamiento"], ["pvcs", "Solicitudes de volumen persistente (PVC)"], ["pvs", "Volúmenes persistentes ('
    'PV)"], ["pdbs", "Presupuestos de interrupción (PDB)"], ["hpas", "Autoescaladores de cargas (HPA)"], '
    '["network_policies", "Políticas de red"], ["resource_quotas", "Cuotas de recursos"], ["metrics", "Mé'
    'tricas"]]);\n  const BUCKET = new Map([["quick_win", ["Acciones rápidas / menos de una semana", "Prio'
    'ridad de planificación orientativa; el plazo no está garantizado."]], ["short_term", ["Corto plazo /'
    ' de uno a tres meses", "Coordinar dependencias, responsables y ventanas de cambio."]], ["medium_term'
    '", ["Medio plazo / horizonte a acordar", "Confirmar diseño, necesidad de negocio y secuencia con el '
    'responsable."]]]);\n  const PAGE_SIZE = 25;\n  const MAX_BYTES = 4800000;\n  const RUN_STATUS = new Map'
    '([["ok", "Recopilada"], ["partial", "Parcial"], ["error", "Error de recopilación"]]);\n  const GRAPH_'
    'KINDS = new Map([["aks", "Azure Kubernetes Service (AKS)"], ["cluster", "Azure Kubernetes Service (A'
    'KS)"],\n    ["node_pool", "Pool de nodos"], ["namespace", "Namespace"], ["workload", "Carga de trabaj'
    'o"], ["service", "Service de Kubernetes"],\n    ["load_balancer", "Azure Load Balancer"], ["public_lo'
    'ad_balancer", "Azure Load Balancer público (ELB)"],\n    ["private_load_balancer", "Azure Load Balanc'
    'er privado (ILB)"], ["subnet", "Subnet"], ["vnet", "Azure Virtual Network"],\n    ["nsg", "Network Se'
    'curity Group"], ["disk", "Azure Managed Disk"], ["pvc", "PVC de Kubernetes"], ["pv", "PV de Kubernet'
    'es"],\n    ["acr", "Azure Container Registry"], ["key_vault", "Azure Key Vault"], ["identity", "Manag'
    'ed Identity"],\n    ["storage", "Azure Storage"], ["ingress", "Ingress de Kubernetes"]]);\n  for (cons'
    't [key, label] of [["agent_pool", "Pool de nodos AKS"], ["managed_cluster", "Azure Kubernetes Servic'
    'e (AKS)"],\n    ["azure_load_balancer", "Azure Load Balancer"], ["load_balancer_frontend_public", "Az'
    'ure Load Balancer / frontend público (ELB)"],\n    ["load_balancer_frontend_private", "Azure Load Bal'
    'ancer / frontend privado (ILB)"], ["load_balancer_backend_pool", "Azure Load Balancer / backend pool'
    '"],\n    ["load_balancer_probe", "Azure Load Balancer / probe"], ["load_balancer_rule", "Azure Load B'
    'alancer / regla"],\n    ["managed_disk", "Azure Managed Disk"], ["virtual_machine_scale_set", "Azure '
    'Virtual Machine Scale Set"],\n    ["virtual_network", "Azure Virtual Network"], ["network_security_gr'
    'oup", "Network Security Group"],\n    ["public_ip_address", "Azure Public IP Address"], ["container_r'
    'egistry", "Azure Container Registry"],\n    ["persistent_volume", "PV de Kubernetes"], ["persistent_v'
    'olume_claim", "PVC de Kubernetes"],\n    ["replica_set", "ReplicaSet de Kubernetes"], ["pod", "Pod de'
    ' Kubernetes"], ["node", "Nodo de Kubernetes"]]) GRAPH_KINDS.set(key, label);\n  /* OFFICIAL_AZURE_ICO'
    'N_DATA_START */\n  const AZURE_ICONS = new Map([["aks","data:image/svg+xml;base64,PHN2ZyBpZD0iYWY2YTJ'
    'jNDItYmQ0OC00ODU3LWE0NzktYWVjZjhiM2RlNGY2IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI'
    'xOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImI3MGM5Y2YxLWJhYjg'
    'tNDdlMC1iYmRiLWNlMWNkNjY0ZDI2OCIgeDE9IjIuOTQiIHkxPSIzLjc0IiB4Mj0iOC42NyIgeTI9IjMuNzQiIGdyYWRpZW50VW5'
    'pdHM9InVzZXJTcGFjZU9uVXNlIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiNiNzdhZjQiIC8+PHN0b3Agb2Zmc2V0PSI'
    'xIiBzdG9wLWNvbG9yPSIjNzczYWRjIiAvPjwvbGluZWFyR3JhZGllbnQ+PGxpbmVhckdyYWRpZW50IGlkPSJiZWI2OTk1My1iZDk'
    '2LTQ1MTUtODg0My1hYzEyNTQ2YWY5MzYiIHgxPSI5LjEzIiB5MT0iMy43OSIgeDI9IjE0Ljg1IiB5Mj0iMy43OSIgZ3JhZGllbnR'
    'Vbml0cz0idXNlclNwYWNlT25Vc2UiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iI2I3N2FmNCIgLz48c3RvcCBvZmZzZXQ'
    '9IjEiIHN0b3AtY29sb3I9IiM3NzNhZGMiIC8+PC9saW5lYXJHcmFkaWVudD48bGluZWFyR3JhZGllbnQgaWQ9ImEzOWM3NmU4LTU'
    '0N2UtNGViNC1iYzI1LWQ4MWMwZjhjZGE2MiIgeDE9IjAuMDEiIHkxPSI5LjEyIiB4Mj0iNS43MyIgeTI9IjkuMTIiIGdyYWRpZW5'
    '0VW5pdHM9InVzZXJTcGFjZU9uVXNlIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiNiNzdhZjQiIC8+PHN0b3Agb2Zmc2V'
    '0PSIxIiBzdG9wLWNvbG9yPSIjNzczYWRjIiAvPjwvbGluZWFyR3JhZGllbnQ+PGxpbmVhckdyYWRpZW50IGlkPSJmMGEyYTQ5MS0'
    'xN2RjLTRiYjgtYmJmYy1lZTU4YTVjZjQ3ZGEiIHgxPSI2LjE4IiB5MT0iOS4wOCIgeDI9IjExLjkiIHkyPSI5LjA4IiBncmFkaWV'
    'udFVuaXRzPSJ1c2VyU3BhY2VPblVzZSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjYjc3YWY0IiAvPjxzdG9wIG9mZnN'
    'ldD0iMSIgc3RvcC1jb2xvcj0iIzc3M2FkYyIgLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJHcmFkaWVudCBpZD0iZWZjNmE1NmQ'
    'tODU4NS00MTdkLTkzMWEtMWRhYzIxMTRjY2QwIiB4MT0iMTIuMzUiIHkxPSI5LjEzIiB4Mj0iMTguMDgiIHkyPSI5LjEzIiBncmF'
    'kaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjYjc3YWY0IiAvPjxzdG9wIG9'
    'mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzc3M2FkYyIgLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJHcmFkaWVudCBpZD0iZTM5OWF'
    'hOTMtMzQxZi00ZGYyLTljMDItNjAzYjgyYjQ4NGMyIiB4MT0iMi44NyIgeTE9IjE0LjU2IiB4Mj0iOC42IiB5Mj0iMTQuNTYiIGd'
    'yYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVXNlIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiNiNzdhZjQiIC8+PHN0b3A'
    'gb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjNzczYWRjIiAvPjwvbGluZWFyR3JhZGllbnQ+PGxpbmVhckdyYWRpZW50IGlkPSJhMTU'
    'yYmJhMC1iYTJiLTQ4M2EtYjhjMS0wYWU3ZGUzNTU5OTAiIHgxPSI5LjA1IiB5MT0iMTQuNiIgeDI9IjE0Ljc4IiB5Mj0iMTQuNiI'
    'gZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iI2I3N2FmNCIgLz48c3R'
    'vcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiM3NzNhZGMiIC8+PC9saW5lYXJHcmFkaWVudD48L2RlZnM+PHRpdGxlPkljb24tY29'
    'tcHV0ZS0yMzwvdGl0bGU+PHBvbHlnb24gcG9pbnRzPSI1LjggMS4yMiAyLjk0IDEuNzUgMi45NCA1LjY1IDUuOCA2LjI2IDguNjc'
    'gNS4xMSA4LjY3IDIuMiA1LjggMS4yMiIgZmlsbD0idXJsKCNiNzBjOWNmMS1iYWI4LTQ3ZTAtYmJkYi1jZTFjZDY2NGQyNjgpIiA'
    'vPjxwYXRoIGQ9Ik01LjkxLDYuMiw4LjUzLDUuMTRBLjIuMiwwLDAsMCw4LjY1LDVWMi4zNmEuMjEuMjEsMCwwLDAtLjEzLS4xOGw'
    'tMi42NS0uOUg1Ljc1bC0yLjYuNDhBLjIuMiwwLDAsMCwzLDEuOTRWNS40N2EuMTkuMTksMCwwLDAsLjE1LjE5bDIuNjMuNTVBLjM'
    'yLjMyLDAsMCwwLDUuOTEsNi4yWiIgZmlsbD0ibm9uZSIgLz48cGF0aCBkPSJNMi45NCwxLjc1djMuOWwyLjg5LjYxdi01Wm0xLjI'
    'yLDMuNi0uODEtLjE2di0zbC44MS0uMTNabTEuMjYuMjMtLjkzLS4xNVYybC45My0uMTZaIiBmaWxsPSIjMzQxYTZlIiAvPjxwb2x'
    '5Z29uIHBvaW50cz0iMTEuOTkgMS4yNyA5LjEzIDEuOCA5LjEzIDUuNyAxMS45OSA2LjMxIDE0Ljg1IDUuMTUgMTQuODUgMi4yNSA'
    'xMS45OSAxLjI3IiBmaWxsPSJ1cmwoI2JlYjY5OTUzLWJkOTYtNDUxNS04ODQzLWFjMTI1NDZhZjkzNikiIC8+PHBhdGggZD0iTTk'
    'uMTMsMS44VjUuN0wxMiw2LjMxdi01Wm0xLjIxLDMuNi0uODEtLjE2di0zbC44MS0uMTNabTEuMjYuMjMtLjkzLS4xNVYyLjA1bC4'
    '5My0uMTdaIiBmaWxsPSIjMzQxYTZlIiAvPjxwb2x5Z29uIHBvaW50cz0iMi44NyA2LjYgMC4wMSA3LjEzIDAuMDEgMTEuMDMgMi4'
    '4NyAxMS42NCA1Ljc0IDEwLjQ5IDUuNzQgNy41OCAyLjg3IDYuNiIgZmlsbD0idXJsKCNhMzljNzZlOC01NDdlLTRlYjQtYmMyNS1'
    'kODFjMGY4Y2RhNjIpIiAvPjxwYXRoIGQ9Ik0wLDcuMTNWMTFsMi44OS42MXYtNVptMS4yMSwzLjYxLS44MS0uMTd2LTNsLjgxLS4'
    'xNFpNMi40OCwxMWwtLjkzLS4xNVY3LjM4bC45My0uMTZaIiBmaWxsPSIjMzQxYTZlIiAvPjxwb2x5Z29uIHBvaW50cz0iOS4wNCA'
    '2LjU2IDYuMTggNy4wOSA2LjE4IDEwLjk5IDkuMDQgMTEuNjEgMTEuOSAxMC40NSAxMS45IDcuNTQgOS4wNCA2LjU2IiBmaWxsPSJ'
    '1cmwoI2YwYTJhNDkxLTE3ZGMtNGJiOC1iYmZjLWVlNThhNWNmNDdkYSkiIC8+PHBhdGggZD0iTTYuMTgsNy4wOVYxMWwyLjg4LjY'
    'xdi01Wk03LjM5LDEwLjdsLS44MS0uMTd2LTNsLjgxLS4xNFptMS4yNi4yMi0uOTMtLjE1VjcuMzRsLjkzLS4xNloiIGZpbGw9IiM'
    'zNDFhNmUiIC8+PHBvbHlnb24gcG9pbnRzPSIxNS4yMSA2LjYxIDEyLjM1IDcuMTQgMTIuMzUgMTEuMDQgMTUuMjEgMTEuNjUgMTg'
    'uMDggMTAuNSAxOC4wOCA3LjU5IDE1LjIxIDYuNjEiIGZpbGw9InVybCgjZWZjNmE1NmQtODU4NS00MTdkLTkzMWEtMWRhYzIxMTR'
    'jY2QwKSIgLz48cGF0aCBkPSJNMTIuMzUsNy4xNFYxMWwyLjg5LjYxdi01Wm0xLjIyLDMuNjEtLjgxLS4xN3YtM2wuODEtLjE0Wm0'
    'xLjI2LjIyLS45My0uMTVWNy4zOWwuOTMtLjE2WiIgZmlsbD0iIzM0MWE2ZSIgLz48cG9seWdvbiBwb2ludHM9IjUuNzMgMTIuMDQ'
    'gMi44NyAxMi41NiAyLjg3IDE2LjQ2IDUuNzMgMTcuMDggOC42IDE1LjkyIDguNiAxMy4wMiA1LjczIDEyLjA0IiBmaWxsPSJ1cmw'
    'oI2UzOTlhYTkzLTM0MWYtNGRmMi05YzAyLTYwM2I4MmI0ODRjMikiIC8+PHBhdGggZD0iTTUuODQsMTcsOC40NSwxNmEuMTguMTg'
    'sMCwwLDAsLjEyLS4xOHYtMi42QS4yLjIsMCwwLDAsOC40NCwxM0w1LjgsMTIuMWEuMTcuMTcsMCwwLDAtLjEyLDBsLTIuNi40N2E'
    'uMTkuMTksMCwwLDAtLjE2LjE5djMuNTRhLjE5LjE5LDAsMCwwLC4xNS4xOUw1LjcsMTdBLjIzLjIzLDAsMCwwLDUuODQsMTdaIiB'
    'maWxsPSJub25lIiAvPjxwYXRoIGQ9Ik0yLjg3LDEyLjU2djMuOWwyLjg5LjYyVjEyWm0xLjIyLDMuNjFMMy4yOCwxNlYxM2wuODE'
    'tLjE0Wm0xLjI2LjIzLS45My0uMTVWMTIuODFsLjkzLS4xNloiIGZpbGw9IiMzNDFhNmUiIC8+PHBvbHlnb24gcG9pbnRzPSIxMS4'
    '5MSAxMi4wOCA5LjA1IDEyLjYxIDkuMDUgMTYuNTEgMTEuOTEgMTcuMTIgMTQuNzggMTUuOTcgMTQuNzggMTMuMDYgMTEuOTEgMTI'
    'uMDgiIGZpbGw9InVybCgjYTE1MmJiYTAtYmEyYi00ODNhLWI4YzEtMGFlN2RlMzU1OTkwKSIgLz48cGF0aCBkPSJNOS4wNSwxMi4'
    '2MXYzLjlsMi44OS42MXYtNVptMS4yMiwzLjYxLS44MS0uMTd2LTNsLjgxLS4xNFptMS4yNi4yMi0uOTMtLjE1VjEyLjg2bC45My0'
    'uMTZaIiBmaWxsPSIjMzQxYTZlIiAvPjwvc3ZnPg=="],["load_balancer","data:image/svg+xml;base64,PHN2ZyBpZD0i'
    'ZTFlNzFlMjktMDY1YS00NmY3LWFkNGItNTJhZjM2YTBiZmNiIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdp'
    'ZHRoPSIxOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImVmODQ2NjNh'
    'LWE3YjYtNDZhNy1hMjc1LTFlNDE5ZjVlYWU2MiIgeDE9IjkiIHkxPSIxOS44NSIgeDI9IjkiIHkyPSItMS4wMiIgZ3JhZGllbnRV'
    'bml0cz0idXNlclNwYWNlT25Vc2UiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzVlOTYyNCIgLz48c3RvcCBvZmZzZXQ9'
    'IjAuMDIiIHN0b3AtY29sb3I9IiM1Zjk3MjQiIC8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjNzZiYzJkIiAvPjwvbGlu'
    'ZWFyR3JhZGllbnQ+PC9kZWZzPjx0aXRsZT5JY29uLW5ldHdvcmtpbmctNjI8L3RpdGxlPjxwYXRoIGQ9Ik0uMTgsOC41Nyw4LjU3'
    'LjE4YS42LjYsMCwwLDEsLjg2LDBsOC4zOSw4LjM5YS42LjYsMCwwLDEsMCwuODZsLTguNCw4LjRhLjYuNiwwLDAsMS0uODQsMEwu'
    'MTgsOS40M0EuNi42LDAsMCwxLC4xOCw4LjU3WiIgZmlsbD0idXJsKCNlZjg0NjYzYS1hN2I2LTQ2YTctYTI3NS0xZTQxOWY1ZWFl'
    'NjIpIiAvPjxwYXRoIGQ9Ik0xMS4yLDQsOS4wOCwxLjg5YS4xMi4xMiwwLDAsMC0uMTYsMEw2LjgsNGEuMS4xLDAsMCwwLC4wOC4x'
    'OEg4LjEyYS4xMS4xMSwwLDAsMSwuMTEuMTF2MmEuMTEuMTEsMCwwLDAsLjExLjExSDkuNjZhLjExLjExLDAsMCwwLC4xMS0uMTF2'
    'LTJhLjExLjExLDAsMCwxLC4xMS0uMTFoMS4yNEEuMS4xLDAsMCwwLDExLjIsNFoiIGZpbGw9IiNiNGVjMzYiIC8+PHBhdGggZD0i'
    'TTQsNi42MSwxLjksOC43NGEuMTEuMTEsMCwwLDAsMCwuMTVMNCwxMWEuMTEuMTEsMCwwLDAsLjE5LS4wOFY5LjY5YS4xMS4xMSww'
    'LDAsMSwuMTEtLjExaDJhLjEuMSwwLDAsMCwuMS0uMTFWOC4xNUEuMS4xLDAsMCwwLDYuMzMsOGgtMmEuMS4xLDAsMCwxLS4xMS0u'
    'MVY2LjY5QS4xMS4xMSwwLDAsMCw0LDYuNjFaIiBmaWxsPSIjYjRlYzM2IiAvPjxwYXRoIGQ9Ik0xNC4wOCwxMWwyLjEzLTIuMTJh'
    'LjExLjExLDAsMCwwLDAtLjE1TDE0LjA4LDYuNjFhLjExLjExLDAsMCwwLS4xOC4wOFY3Ljk0YS4xLjEsMCwwLDEtLjExLjFoLTJh'
    'LjEuMSwwLDAsMC0uMS4xMVY5LjQ3YS4xLjEsMCwwLDAsLjEuMTFoMmEuMTEuMTEsMCwwLDEsLjExLjExdjEuMjRBLjExLjExLDAs'
    'MCwwLDE0LjA4LDExWiIgZmlsbD0iI2I0ZWMzNiIgLz48cGF0aCBkPSJNMTEuNzksOWEyLjc5LDIuNzksMCwxLDAtMy41NCwyLjY3'
    'di45NWExLjcxLDEuNzEsMCwxLDAsMS41Nywwdi0xQTIuNzcsMi43NywwLDAsMCwxMS43OSw5WiIgZmlsbD0iI2ZmZiIgLz48Y2ly'
    'Y2xlIGlkPSJlOTljMzM4Ny0xNWMzLTRmMjgtYmQ0Yi1jYjIwOWI0MzBlMDYiIGN4PSI5LjAxIiBjeT0iOC45OSIgcj0iMS42MiIg'
    'ZmlsbD0iIzVlYTBlZiIgLz48L3N2Zz4="],["virtual_network","data:image/svg+xml;base64,PHN2ZyBpZD0iYTE2MDZ'
    'hNTItZmIxNC00NjM3LTg3ZGUtNGQ1MjRiYmExODI5IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI'
    'xOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImYwMjBmYjdlLTIyNDM'
    'tNDUwMS04MTUzLTVkNjliZDNjMzRmNyIgeDE9IjkuODgiIHkxPSI4LjU5IiB4Mj0iMTEuNTIiIHkyPSIxMC4yMyIgZ3JhZGllbnR'
    'UcmFuc2Zvcm09InRyYW5zbGF0ZSgyLjAzIC0wLjQpIHJvdGF0ZSgtMC4wOCkiIGdyYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVXN'
    'lIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiM4NmQ2MzMiIC8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjNWU'
    '5NjI0IiAvPjwvbGluZWFyR3JhZGllbnQ+PGxpbmVhckdyYWRpZW50IGlkPSJiM2MyYmM5NC04YzdjLTQ4YTItOTcxOS02ZWU4OTl'
    'hNDNhOTciIHgxPSI2LjE4IiB5MT0iOC41OSIgeDI9IjcuODEiIHkyPSIxMC4yMyIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25'
    'Vc2UiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzg2ZDYzMyIgLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiM'
    '1ZTk2MjQiIC8+PC9saW5lYXJHcmFkaWVudD48bGluZWFyR3JhZGllbnQgaWQ9ImU5MDBjMzVkLTA5NTgtNGQ0Zi1iMjhmLTMwMmV'
    'lYWFmNTJmNSIgeDE9IjIuNDgiIHkxPSI4LjU5IiB4Mj0iNC4xMSIgeTI9IjEwLjIzIiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2V'
    'PblVzZSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjODZkNjMzIiAvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0'
    'iIzVlOTYyNCIgLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48dGl0bGU+SWNvbi1uZXR3b3JraW5nLTYxPC90aXRsZT48Y2lyY2x'
    'lIGN4PSIxMi43NCIgY3k9IjguOTkiIHI9IjEuMTYiIGZpbGw9InVybCgjZjAyMGZiN2UtMjI0My00NTAxLTgxNTMtNWQ2OWJkM2M'
    'zNGY3KSIgLz48Y2lyY2xlIGN4PSI5LjA0IiBjeT0iOSIgcj0iMS4xNiIgZmlsbD0idXJsKCNiM2MyYmM5NC04YzdjLTQ4YTItOTc'
    'xOS02ZWU4OTlhNDNhOTcpIiAvPjxjaXJjbGUgY3g9IjUuMzQiIGN5PSI5IiByPSIxLjE2IiBmaWxsPSJ1cmwoI2U5MDBjMzVkLTA'
    '5NTgtNGQ0Zi1iMjhmLTMwMmVlYWFmNTJmNSkiIC8+PHBhdGggZD0iTTIuNjEsNy4yOGguOTRhLjMuMywwLDAsMSwuMy4zdjYuOTR'
    'hLjYuNiwwLDAsMS0uNi42SDIuMzFhMCwwLDAsMCwxLDAsMFY3LjU4YS4zLjMsMCwwLDEsLjMtLjNaIiB0cmFuc2Zvcm09InRyYW5'
    'zbGF0ZSgxMy4xOCAxNi45Mykgcm90YXRlKDEzNC45MikiIGZpbGw9IiM1MGU2ZmYiIC8+PHBhdGggZD0iTTIuNTYsMi45MUgzLjV'
    'hLjMuMywwLDAsMSwuMy4zdjcuNDNhMCwwLDAsMCwxLDAsMEgyLjg2YS42LjYsMCwwLDEtLjYtLjZWMy4yMWEuMy4zLDAsMCwxLC4'
    'zLS4zWiIgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoNS42NiAtMC4xNikgcm90YXRlKDQ0LjkyKSIgZmlsbD0iIzE0OTBkZiIgLz48cGF'
    '0aCBkPSJNMTQuMTUsNy4yOGguOTRhLjYuNiwwLDAsMSwuNi42djYuOTRhLjMuMywwLDAsMS0uMy4zaC0uOTRhLjMuMywwLDAsMS0'
    'uMy0uM1Y3LjI4YTAsMCwwLDAsMSwwLDBaIiB0cmFuc2Zvcm09InRyYW5zbGF0ZSgxMi4zMiAtNy4yOCkgcm90YXRlKDQ1LjA4KSI'
    'gZmlsbD0iIzUwZTZmZiIgLz48cGF0aCBkPSJNMTQuODEsMi45MWguOTRhMCwwLDAsMCwxLDAsMHY3LjQzYS4zLjMsMCwwLDEtLjM'
    'uM0gxNC41YS4zLjMsMCwwLDEtLjMtLjNWMy41MWEuNi42LDAsMCwxLC42LS42WiIgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMzAuMzY'
    'gMC45OSkgcm90YXRlKDEzNS4wOCkiIGZpbGw9IiMxNDkwZGYiIC8+PC9zdmc+"],["network_security_group","data:imag'
    'e/svg+xml;base64,PHN2ZyBpZD0iYjc1OTQzYmEtMWEzZi00ODVkLWI0NWQtM2ZlZTAwZWQ3ZTVlIiB4bWxucz0iaHR0cDovL3d'
    '3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48bGluZWF'
    'yR3JhZGllbnQgaWQ9ImZlNWNlNDNmLTI4MjMtNDYxNy1iYjU0LTU3ZGY5ZTc3ODM3MiIgeDE9IjkuMDEiIHkxPSIwLjc1IiB4Mj0'
    'iOS4wMSIgeTI9IjE3LjI1IiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9'
    'yPSIjNWVhMGVmIiAvPjxzdG9wIG9mZnNldD0iMC4xOCIgc3RvcC1jb2xvcj0iIzU1OWNlYyIgLz48c3RvcCBvZmZzZXQ9IjAuNDc'
    'iIHN0b3AtY29sb3I9IiMzYzkxZTUiIC8+PHN0b3Agb2Zmc2V0PSIwLjg0IiBzdG9wLWNvbG9yPSIjMTM4MGRhIiAvPjxzdG9wIG9'
    'mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzAwNzhkNCIgLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48dGl0bGU+SWNvbi1uZXR3b3J'
    'raW5nLTY3PC90aXRsZT48cGF0aCBkPSJNMTYuMzYsOC40YzAsNC44NC01Ljg1LDguNzQtNy4xMiw5LjUzYS40Ni40NiwwLDAsMS0'
    'uNDgsMGMtMS4yNy0uNzktNy4xMi00LjY5LTcuMTItOS41M1YyLjU4YS40Ni40NiwwLDAsMSwuNDUtLjQ2QzYuNjQsMiw1LjU5LDA'
    'sOSwwczIuMzYsMiw2LjkxLDIuMTJhLjQ2LjQ2LDAsMCwxLC40NS40NloiIGZpbGw9IiMwMDc4ZDQiIC8+PHBhdGggZD0iTTE1Ljc'
    '1LDguNDVjMCw0LjQ0LTUuMzYsOC02LjUzLDguNzRhLjQzLjQzLDAsMCwxLS40NCwwYy0xLjE3LS43Mi02LjUzLTQuMy02LjUzLTg'
    'uNzRWMy4xMWEuNDIuNDIsMCwwLDEsLjQxLS40MkM2LjgzLDIuNTgsNS44Ny43NSw5LC43NXMyLjE3LDEuODMsNi4zNCwxLjk0YS4'
    '0Mi40MiwwLDAsMSwuNDEuNDJaIiBmaWxsPSIjNmJiOWYyIiAvPjxwYXRoIGQ9Ik05LDlWLjc1YzMuMTMsMCwyLjE3LDEuODMsNi4'
    'zNCwxLjk0YS40My40MywwLDAsMSwuNDEuNDNWOC40NmE0Ljg5LDQuODksMCwwLDEsMCwuNTRaTTksOUgyLjI4Yy40LDQuMTgsNS4'
    'zOCw3LjUsNi41LDguMTlhLjM5LjM5LDAsMCwwLC4xOC4wNkg5WiIgZmlsbD0idXJsKCNmZTVjZTQzZi0yODIzLTQ2MTctYmI1NC0'
    '1N2RmOWU3NzgzNzIpIiAvPjxwYXRoIGQ9Ik0yLjY2LDIuNjlDNi44MywyLjU4LDUuODcuNzUsOSwuNzVWOUgyLjI4YTQuODksNC4'
    '4OSwwLDAsMSwwLS41NFYzLjEyQS40My40MywwLDAsMSwyLjY2LDIuNjlaIiBmaWxsPSIjNTBlNmZmIiAvPjxwYXRoIGQ9Ik0xNS4'
    '3Miw5SDl2OC4yNUg5YS4zOS4zOSwwLDAsMCwuMTgtLjA2QzEwLjM0LDE2LjUsMTUuMzIsMTMuMTgsMTUuNzIsOVoiIGZpbGw9IiM'
    '1MGU2ZmYiIC8+PC9zdmc+"],["disk","data:image/svg+xml;base64,PHN2ZyBpZD0iZWEzMTdiMzEtYjMxYS00NGZkLWE3Y'
    'zQtMjYwNzc0NTc1MzYzIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxOCIgaGVpZ2h0PSIxOCIgd'
    'mlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImEzYWVjYjVjLTExZjUtNGVjZS05OWY3LTk2NzJmM'
    'zIzNjE1NiIgeDE9IjE1LjM1IiB5MT0iMTMuNDEiIHgyPSIxNS4zNyIgeTI9IjEzLjQxIiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY'
    '2VPblVzZSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjMDA3OGQ0IiAvPjxzdG9wIG9mZnNldD0iMC4xNCIgc3RvcC1jb'
    '2xvcj0iIzE3ODJkYSIgLz48c3RvcCBvZmZzZXQ9IjAuMzciIHN0b3AtY29sb3I9IiMzNjhmZTMiIC8+PHN0b3Agb2Zmc2V0PSIwL'
    'jU5IiBzdG9wLWNvbG9yPSIjNGM5OGVhIiAvPjxzdG9wIG9mZnNldD0iMC44IiBzdG9wLWNvbG9yPSIjNTk5ZWVlIiAvPjxzdG9wI'
    'G9mZnNldD0iMC45OSIgc3RvcC1jb2xvcj0iIzVlYTBlZiIgLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJHcmFkaWVudCBpZD0iY'
    'jRkZmIzYWItMjVmZS00MTZjLWE2ZmEtZjMzZmNmYmYyMmRjIiB4MT0iNS41MSIgeTE9IjgwMi41IiB4Mj0iMTIuOTQiIHkyPSI4M'
    'TAuNTgiIGdyYWRpZW50VHJhbnNmb3JtPSJ0cmFuc2xhdGUoMCAtMTI3OC43Nikgc2NhbGUoMSAxLjU5KSIgZ3JhZGllbnRVbml0c'
    'z0idXNlclNwYWNlT25Vc2UiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzAwNzhkNCIgLz48c3RvcCBvZmZzZXQ9IjAuM'
    'DIiIHN0b3AtY29sb3I9IiMwZDdlZDgiIC8+PHN0b3Agb2Zmc2V0PSIwLjA4IiBzdG9wLWNvbG9yPSIjMmI4YWUwIiAvPjxzdG9wI'
    'G9mZnNldD0iMC4xNSIgc3RvcC1jb2xvcj0iIzQxOTRlNyIgLz48c3RvcCBvZmZzZXQ9IjAuMjIiIHN0b3AtY29sb3I9IiM1MTliZ'
    'WIiIC8+PHN0b3Agb2Zmc2V0PSIwLjI5IiBzdG9wLWNvbG9yPSIjNWI5ZmVlIiAvPjxzdG9wIG9mZnNldD0iMC40IiBzdG9wLWNvb'
    'G9yPSIjNWVhMGVmIiAvPjxzdG9wIG9mZnNldD0iMC41NSIgc3RvcC1jb2xvcj0iIzViOWZlZSIgLz48c3RvcCBvZmZzZXQ9IjAuN'
    'jgiIHN0b3AtY29sb3I9IiM1MDlhZWIiIC8+PHN0b3Agb2Zmc2V0PSIwLjgiIHN0b3AtY29sb3I9IiMzZjkyZTYiIC8+PHN0b3Agb'
    '2Zmc2V0PSIwLjkxIiBzdG9wLWNvbG9yPSIjMjY4OGRmIiAvPjxzdG9wIG9mZnNldD0iMC45OSIgc3RvcC1jb2xvcj0iIzEyN2ZkO'
    'SIgLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48dGl0bGU+SWNvbi1jb21wdXRlLTMyPC90aXRsZT48ZWxsaXBzZSBjeD0iOC45N'
    'iIgY3k9IjExLjgyIiByeD0iNy41IiByeT0iMy4wOCIgZmlsbD0iIzc2YmMyZCIgLz48ZWxsaXBzZSBjeD0iOS4xNSIgY3k9IjExL'
    'jc5IiByeD0iMi40OSIgcnk9IjAuNjgiIGZpbGw9IiM1ZTk2MjQiIC8+PHBhdGggZD0iTTE1LjM1LDEzLjRsMCwwWiIgZmlsbD0id'
    'XJsKCNhM2FlY2I1Yy0xMWY1LTRlY2UtOTlmNy05NjcyZjMyMzYxNTYpIiAvPjxlbGxpcHNlIGN4PSI4Ljk2IiBjeT0iNC45OSIgc'
    'ng9IjcuNSIgcnk9IjMuMDgiIGZpbGw9InVybCgjYjRkZmIzYWItMjVmZS00MTZjLWE2ZmEtZjMzZmNmYmYyMmRjKSIgLz48ZWxsa'
    'XBzZSBjeD0iOS4xNSIgY3k9IjQuOTUiIHJ4PSIyLjQ5IiByeT0iMC42OCIgZmlsbD0iIzAwNWJhMSIgLz48cGF0aCBkPSJNOSwxN'
    'C44NGM0LjE1LDAsNy41LTEuMzgsNy41LTMuMDhoMHYxLjgyaDBjLS4xNCwxLjY1LTMuNDQsMy03LjUsM3MtNy41LTEuMzgtNy41L'
    'TMuMDhWMTEuNzZDMS40NiwxMy40Niw0LjgyLDE0Ljg0LDksMTQuODRaIiBmaWxsPSIjNWU5NjI0IiAvPjxwYXRoIGQ9Ik0xNS4zN'
    'SwxMy40bDAsMFoiIGZpbGw9InVybCgjYTNhZWNiNWMtMTFmNS00ZWNlLTk5ZjctOTY3MmYzMjM2MTU2KSIgLz48cGF0aCBkPSJNO'
    'Sw4YzQuMTUsMCw3LjUtMS4zOCw3LjUtMy4wOGgwVjYuNzRoMGMtLjE0LDEuNjUtMy40NCwzLTcuNSwzcy03LjUtMS4zOC03LjUtM'
    'y4wOFY0LjkyQzEuNDYsNi42Miw0LjgyLDgsOSw4WiIgZmlsbD0iIzAwNzhkNCIgLz48L3N2Zz4="],["container_registry",'
    '"data:image/svg+xml;base64,PHN2ZyBpZD0iZTlmN2JhNzMtNTZiOC00ODY0LTk2ZmYtZGNlODBiZWY3MjlhIiB4bWxucz0ia'
    'HR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmc'
    'z48bGluZWFyR3JhZGllbnQgaWQ9ImJkZjI3ZGM2LTAyNWMtNDgwNS04YmZkLTNhMDVkZWZkM2ZiZCIgeDE9IjguNjM3IiB5MT0iL'
    'TEuOTkxIiB4Mj0iOC42MzciIHkyPSIxNi43MzkiIGdyYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVXNlIj48c3RvcCBvZmZzZXQ9I'
    'jAiIHN0b3AtY29sb3I9IiM1ZWEwZWYiIC8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjMDA3OGQ0IiAvPjwvbGluZWFyR'
    '3JhZGllbnQ+PGxpbmVhckdyYWRpZW50IGlkPSJmY2Q2YzZjNy1lMmViLTRhMzItYjkxNC0zYTg0ODU1ODVhMWIiIHgxPSIxMi45N'
    'iIgeTE9IjguNTYxIiB4Mj0iMTIuOTYiIHkyPSI2LjE0MSIgZ3JhZGllbnRUcmFuc2Zvcm09Im1hdHJpeCgxLCAwLCAwLCAtMSwgM'
    'CwgMjApIiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjMzMzMTMyI'
    'iAvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzViNWE1YyIgLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48cGF0aCBkP'
    'SJNNy40Myw4LjE3OGwyLjU4OS0xLjU3Myw3LjI1NSwyLjczMUEzLjY2NCwzLjY2NCwwLDAsMCwxNi4yMyw3LjQ5bC0uMDEtLjA1Q'
    'TQuMTk0LDQuMTk0LDAsMCwwLDE0LDYuMzJhNC45MSw0LjkxLDAsMCwwLTUuMS00LjdBNS4wNzEsNS4wNzEsMCwwLDAsNC4wNiw0L'
    'jkxLDQuNjIxLDQuNjIxLDAsMCwwLDAsOS4zOWE0LjczLDQuNzMsMCwwLDAsNC44OSw0LjU0SDcuNDNaIiBmaWxsPSJ1cmwoI2JkZ'
    'jI3ZGM2LTAyNWMtNDgwNS04YmZkLTNhMDVkZWZkM2ZiZCkiIC8+PHBvbHlnb24gcG9pbnRzPSIxMC4wNyA3LjE1OSAxMC4wOCAxM'
    'S40MzkgMTcuOTkgMTMuMTE5IDE3Ljk5IDEwLjEzOSAxMC4wNyA3LjE1OSIgZmlsbD0iIzc2NzY3NiIgLz48cG9seWxpbmUgcG9pb'
    'nRzPSIxMC4wNyA3LjE1OSA3LjkzIDguNDU5IDcuOTMgMTIuNDM5IDEwLjA4IDExLjQzOSIgZmlsbD0iIzk5OSIgLz48cG9seWdvb'
    'iBwb2ludHM9IjEzLjY4IDExLjQ5OSAxNC40IDExLjY5OSAxNC40IDkuMzY5IDEzLjY4IDkuMTE5IDEzLjY4IDExLjQ5OSIgZmlsb'
    'D0iI2EzYTNhMyIgLz48cG9seWdvbiBwb2ludHM9IjEyLjk2IDguODg5IDEyLjI0IDguNjI5IDEyLjI0IDExLjExOSAxMi45NiAxM'
    'S4zMTkgMTIuOTYgOC44ODkiIGZpbGw9IiNhM2EzYTMiIC8+PHBvbHlnb24gcG9pbnRzPSIxNS4xMiAxMS44ODkgMTUuODIgMTIuM'
    'Dc5IDE1Ljg0IDkuODU5IDE1LjEyIDkuNjE5IDE1LjEyIDExLjg4OSIgZmlsbD0iI2EzYTNhMyIgLz48cG9seWdvbiBwb2ludHM9I'
    'jEwLjgxIDEwLjc0OSAxMS41MyAxMC45MjkgMTEuNTMgOC4zOTkgMTAuODEgOC4xNTkgMTAuODEgMTAuNzQ5IiBmaWxsPSIjYTNhM'
    '2EzIiAvPjxwb2x5Z29uIHBvaW50cz0iMTcuMjcgMTAuMzQ5IDE2LjU1IDEwLjA5OSAxNi41NSAxMi4yNjkgMTcuMjcgMTIuNDY5I'
    'DE3LjI3IDEwLjM0OSIgZmlsbD0iI2EzYTNhMyIgLz48cGF0aCBpZD0iZWYwZDFiNTQtYTFlNy00Y2I5LWE0ZTUtOGE4NTE4ZTdjM'
    'TI3IiBkPSJNOC42NiwxMS4zNjlsLS4zNi4yMVY4Ljc0OWwuMzYtLjE5Wm0uNzEtMy4yMkw5LDguMzg5djIuNzVsLjM3LS4yWiIgZ'
    'mlsbD0iI2IzYjNiMyIgLz48cG9seWdvbiBwb2ludHM9IjE3Ljk5IDEzLjExOSAxNS44MyAxMy44NTkgNy45MyAxMi40MzkgMTAuM'
    'DggMTEuNDM5IDE3Ljk5IDEzLjExOSIgZmlsbD0idXJsKCNmY2Q2YzZjNy1lMmViLTRhMzItYjkxNC0zYTg0ODU1ODVhMWIpIiAvP'
    'jxwb2x5Z29uIHBvaW50cz0iMTcuOTkgMTYuMTY5IDEwLjA0IDE3LjY3OSAxMC4wOCAxMi4wODkgMTcuOTkgMTMuNTU5IDE3Ljk5I'
    'DE2LjE2OSIgZmlsbD0iIzc2NzY3NiIgLz48cG9seWdvbiBwb2ludHM9IjEwLjgxIDE2Ljc1OSAxMC44MSAxMy4yMDkgMTEuNTMgM'
    'TMuMjk5IDExLjUzIDE2LjYzOSAxMC44MSAxNi43NTkiIGZpbGw9IiNhM2EzYTMiIC8+PHBvbHlnb24gcG9pbnRzPSIxMi45NiAxN'
    'i4zOTkgMTIuMjQgMTYuNTI5IDEyLjI0IDEzLjM4OSAxMi45NiAxMy40OTkgMTIuOTYgMTYuMzk5IDEyLjk2IDE2LjM5OSIgZmlsb'
    'D0iI2EzYTNhMyIgLz48cG9seWdvbiBwb2ludHM9IjEzLjY4IDE2LjI4OSAxMy42OCAxMy41NjkgMTQuNCAxMy42NTkgMTQuNCAxN'
    'i4xNTkgMTMuNjggMTYuMjg5IiBmaWxsPSIjYTNhM2EzIiAvPjxwb2x5Z29uIHBvaW50cz0iMTUuODMgMTUuOTA5IDE1LjEyIDE2L'
    'jAzOSAxNS4xMiAxMy43NDkgMTUuODMgMTMuODU5IDE1LjgzIDE1LjkwOSIgZmlsbD0iI2EzYTNhMyIgLz48cG9seWdvbiBwb2lud'
    'HM9IjE3LjI5IDE1LjY3OSAxNi41NSAxNS44MDkgMTYuNTUgMTMuOTI5IDE3LjI0IDE0LjAxOSAxNy4yOSAxNS42NzkiIGZpbGw9I'
    'iNhM2EzYTMiIC8+PHBhdGggaWQ9ImI5ZjI1ZWI0LTRjODgtNDVjMi1iYzRkLWY5OTI3NTdkN2UwZSIgZD0iTTcuOTMsMTYuNHYtM'
    'y4yNmwyLjE2LTF2NS42WiIgZmlsbD0iIzk5OSIgLz48cGF0aCBpZD0iZWI5MjAwYTctNDY5My00NDI3LWJkYWUtYjMzY2U5MGZmN'
    '2YyIiBkPSJNOC42MSwxNi4zODlsLS4zMi0uMTZ2LTIuNzZsLjMyLS4xNVptLjc3LTMuNDUtLjM4LjE5djMuNDhsLjM3LjE5di0zL'
    'jg2WiIgZmlsbD0iI2IzYjNiMyIgLz48L3N2Zz4="],["key_vault","data:image/svg+xml;base64,PHN2ZyBpZD0iYmM5Mz'
    'E2OWEtMjRjNC00Njg2LTlmMzctYzdlNGY1M2IzZDM0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPS'
    'IxOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48cmFkaWFsR3JhZGllbnQgaWQ9ImZiMTE0YzkzLTdjMT'
    'YtNDU0MC04OTlhLTA3OTYwNDE2MjllNiIgY3g9IjkiIGN5PSI5IiByPSI4LjUiIGdyYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVX'
    'NlIj48c3RvcCBvZmZzZXQ9IjAuMTgiIHN0b3AtY29sb3I9IiM1ZWEwZWYiIC8+PHN0b3Agb2Zmc2V0PSIwLjU2IiBzdG9wLWNvbG'
    '9yPSIjNWM5ZmVlIiAvPjxzdG9wIG9mZnNldD0iMC42OSIgc3RvcC1jb2xvcj0iIzU1OWNlZCIgLz48c3RvcCBvZmZzZXQ9IjAuNz'
    'giIHN0b3AtY29sb3I9IiM0YTk3ZTkiIC8+PHN0b3Agb2Zmc2V0PSIwLjg2IiBzdG9wLWNvbG9yPSIjMzk5MGU0IiAvPjxzdG9wIG'
    '9mZnNldD0iMC45MyIgc3RvcC1jb2xvcj0iIzIzODdkZSIgLz48c3RvcCBvZmZzZXQ9IjAuOTkiIHN0b3AtY29sb3I9IiMwODdiZD'
    'YiIC8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjMDA3OGQ0IiAvPjwvcmFkaWFsR3JhZGllbnQ+PHJhZGlhbEdyYWRpZW'
    '50IGlkPSJlYzQ3OGJkMy01YjM4LTRlN2YtYjY2MS0xMWYzNTU1NzhiZmYiIGN4PSIzOC45NSIgY3k9IjE4Mi4wNyIgcj0iOS44OC'
    'IgZ3JhZGllbnRUcmFuc2Zvcm09InRyYW5zbGF0ZSgtMjguNzEgLTE2My4yNCkgc2NhbGUoMC45NCAwLjk0KSIgZ3JhZGllbnRVbm'
    'l0cz0idXNlclNwYWNlT25Vc2UiPjxzdG9wIG9mZnNldD0iMC4yNyIgc3RvcC1jb2xvcj0iI2ZmZDcwZiIgLz48c3RvcCBvZmZzZX'
    'Q9IjAuNDkiIHN0b3AtY29sb3I9IiNmZmNiMTIiIC8+PHN0b3Agb2Zmc2V0PSIwLjg4IiBzdG9wLWNvbG9yPSIjZmVhYzE5IiAvPj'
    'xzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iI2ZlYTExYiIgLz48L3JhZGlhbEdyYWRpZW50PjwvZGVmcz48dGl0bGU+SWNvbi'
    '1zZWN1cml0eS0yNDU8L3RpdGxlPjxwYXRoIGlkPSJhYWNmODMxMS1hMzE3LTQwMGYtYjYxMy03NGJkMDBkYTVjZTMiIGQ9Ik05LC'
    '41QTguNSw4LjUsMCwxLDAsMTcuNSw5LDguNTEsOC41MSwwLDAsMCw5LC41Wk05LDE2LjM0QTcuMzQsNy4zNCwwLDEsMSwxNi4zNC'
    'w5LDcuMzQsNy4zNCwwLDAsMSw5LDE2LjM0WiIgZmlsbD0idXJsKCNmYjExNGM5My03YzE2LTQ1NDAtODk5YS0wNzk2MDQxNjI5ZT'
    'YpIiAvPjxjaXJjbGUgY3g9IjkiIGN5PSI5IiByPSI3LjM0IiBmaWxsPSIjZmZmIiAvPjxnPjxwYXRoIGlkPSJiZDk4M2M0MS1kYj'
    'czLTQwZWMtYTRmNC1kYTEyMTNmYzk5MjQiIGQ9Ik0xMy40NCw3LjMzYTEuODQsMS44NCwwLDAsMCwwLTIuNTloMEwxMC4yOSwxLj'
    'U4YTEuODMsMS44MywwLDAsMC0yLjU4LDBoMEw0LjU2LDQuNzRhMS44NCwxLjg0LDAsMCwwLDAsMi41OUw3LjE4LDEwYS41MS41MS'
    'wwLDAsMSwuMTUuMzZ2NC44OGEuNjMuNjMsMCwwLDAsLjE4LjQ0bDEuMiwxLjJhLjQxLjQxLDAsMCwwLC41OCwwbDEuMTYtMS4xNm'
    'gwbC42OC0uNjhhLjI1LjI1LDAsMCwwLDAtLjM0bC0uNDktLjQ5YS4yNy4yNywwLDAsMSwwLS4zN2wuNDktLjQ5YS4yNS4yNSwwLD'
    'AsMCwwLS4zNGwtLjQ5LS40OWEuMjcuMjcsMCwwLDEsMC0uMzdsLjQ5LS40OWEuMjUuMjUsMCwwLDAsMC0uMzRsLS42OC0uNjl2LS'
    '4yNVpNOSwyLjM1QTEsMSwwLDAsMSw5LDQuNDIsMSwxLDAsMSwxLDksMi4zNVoiIGZpbGw9InVybCgjZWM0NzhiZDMtNWIzOC00ZT'
    'dmLWI2NjEtMTFmMzU1NTc4YmZmKSIgLz48cGF0aCBpZD0iYWQyZTdlNDQtOWJmZC00Nzc1LThiZjQtOGVmMjZhNTQ0OTIxIiBkPS'
    'JNOC4xOCwxNS4zaDBhLjIzLjIzLDAsMCwwLC4zOC0uMTd2LTRhLjI0LjI0LDAsMCwwLS4xMS0uMmgwYS4yMi4yMiwwLDAsMC0uMz'
    'QuMnY0QS4yOC4yOCwwLDAsMCw4LjE4LDE1LjNaIiBmaWxsPSIjZmY5MzAwIiBvcGFjaXR5PSIwLjc1IiAvPjxyZWN0IGlkPSJhOT'
    'k2ZmJmZi0zOTM2LTRiMjQtYjQwNy0zNjkzMzZjMTQzYmEiIHg9IjYuNDgiIHk9IjUuNzkiIHdpZHRoPSI1LjE3IiBoZWlnaHQ9Ij'
    'AuNjEiIHJ4PSIwLjI4IiBmaWxsPSIjZmY5MzAwIiBvcGFjaXR5PSIwLjc1IiAvPjxyZWN0IGlkPSJiYjEyZDMxYy0zMzUyLTRhMT'
    'gtODdkZC1hMTY5YTRhODcyMWQiIHg9IjYuNDgiIHk9IjYuNzgiIHdpZHRoPSI1LjE3IiBoZWlnaHQ9IjAuNjEiIHJ4PSIwLjI4Ii'
    'BmaWxsPSIjZmY5MzAwIiBvcGFjaXR5PSIwLjc1IiAvPjwvZz48L3N2Zz4="],["managed_identity","data:image/svg+xml'
    ';base64,PHN2ZyBpZD0iZjgwN2NkZGUtMThmMi00NTc0LWJhMTQtZjBiMWVlZjk0YTRhIiB4bWxucz0iaHR0cDovL3d3dy53My5v'
    'cmcvMjAwMC9zdmciIHdpZHRoPSIxOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48bGluZWFyR3JhZGll'
    'bnQgaWQ9ImJiNjdiMTU0LWFmOGYtNGE1My1hOWNlLWFhMDQ2YjkzOThlOSIgeDE9IjEzLjE4IiB5MT0iMTMuMDEiIHgyPSI4LjYz'
    'IiB5Mj0iNC4zOCIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiPjxzdG9wIG9mZnNldD0iMCIgc3RvcC1jb2xvcj0iIzE5'
    'ODhkOSIgLz48c3RvcCBvZmZzZXQ9IjAuOSIgc3RvcC1jb2xvcj0iIzU0YWVmMCIgLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJH'
    'cmFkaWVudCBpZD0iYTQ3ZDcxMzgtMDg0YS00NzFkLThkNjMtNzQ0ZTc1YjllOGEzIiB4MT0iMTEuMjIiIHkxPSIxMC41IiB4Mj0i'
    'MTQuMzciIHkyPSIxNS45MyIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiPjxzdG9wIG9mZnNldD0iMC4xIiBzdG9wLWNv'
    'bG9yPSIjNTRhZWYwIiAvPjxzdG9wIG9mZnNldD0iMC4yOSIgc3RvcC1jb2xvcj0iIzRmYWJlZSIgLz48c3RvcCBvZmZzZXQ9IjAu'
    'NTEiIHN0b3AtY29sb3I9IiM0MWEyZTkiIC8+PHN0b3Agb2Zmc2V0PSIwLjc0IiBzdG9wLWNvbG9yPSIjMmE5M2UwIiAvPjxzdG9w'
    'IG9mZnNldD0iMC44OCIgc3RvcC1jb2xvcj0iIzE5ODhkOSIgLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48dGl0bGU+SWNvbi1p'
    'ZGVudGl0eS0yMjc8L3RpdGxlPjxnPjxwb2x5Z29uIHBvaW50cz0iMS4xNSAxMC4yMiA4LjkzIDE1LjI3IDE2Ljg1IDEwLjIxIDE3'
    'Ljg1IDExLjM2IDguOTMgMTcuMTEgMC4xNSAxMS4zNyAxLjE1IDEwLjIyIiBmaWxsPSIjNTBlNmZmIiAvPjxwb2x5Z29uIHBvaW50'
    'cz0iMS43MyA5LjU4IDguOTMgMSAxNi4yOCA5LjU5IDguOTMgMTQuMjMgMS43MyA5LjU4IiBmaWxsPSIjZmZmIiAvPjxwb2x5Z29u'
    'IHBvaW50cz0iOC45MyAxIDguOTMgMTQuMjMgMS43MyA5LjU4IDguOTMgMSIgZmlsbD0iIzUwZTZmZiIgLz48cG9seWdvbiBwb2lu'
    'dHM9IjguOTMgMSA4LjkzIDE0LjIzIDE2LjI4IDkuNTkgOC45MyAxIiBmaWxsPSJ1cmwoI2JiNjdiMTU0LWFmOGYtNGE1My1hOWNl'
    'LWFhMDQ2YjkzOThlOSkiIC8+PHBvbHlnb24gcG9pbnRzPSI4LjkzIDcuODMgMTYuMjggOS41OSA4LjkzIDE0LjIzIDguOTMgNy44'
    'MyIgZmlsbD0iIzUzYjFlMCIgLz48cG9seWdvbiBwb2ludHM9IjguOTMgMTQuMjMgMS43MyA5LjU4IDguOTMgNy44MyA4LjkzIDE0'
    'LjIzIiBmaWxsPSIjOWNlYmZmIiAvPjxwb2x5Z29uIHBvaW50cz0iOC45MyAxNy4xMSAxNy44NSAxMS4zNiAxNi44NSAxMC4yMSA4'
    'LjkzIDE1LjI3IDguOTMgMTcuMTEiIGZpbGw9InVybCgjYTQ3ZDcxMzgtMDg0YS00NzFkLThkNjMtNzQ0ZTc1YjllOGEzKSIgLz48'
    'Zz48cGF0aCBpZD0iZTE4OWE2MWYtNjMxYS00MTIyLWJiMmItMDc5ZTg2MDA0ZDk2IiBkPSJNMTYuMzIsMTEuODhhMS4xNiwxLjE2'
    'LDAsMCwwLDAtMS42NWgwbC0yLTJhMS4xNiwxLjE2LDAsMCwwLTEuNjQsMGgwbC0yLDJhMS4xOCwxLjE4LDAsMCwwLDAsMS42NWwx'
    'LjY3LDEuNjdhLjMyLjMyLDAsMCwxLC4wOS4yM3YzLjFhLjM2LjM2LDAsMCwwLC4xMi4yOGwuNzYuNzZhLjI1LjI1LDAsMCwwLC4z'
    'NywwbC43My0uNzNoMGwuNDQtLjQ0YS4xNS4xNSwwLDAsMCwwLS4yMWwtLjMxLS4zMWEuMTYuMTYsMCwwLDEsMC0uMjRsLjMxLS4z'
    'MWEuMTYuMTYsMCwwLDAsMC0uMjJsLS4zMS0uMzFhLjE1LjE1LDAsMCwxLDAtLjIzbC4zMS0uMzJhLjE1LjE1LDAsMCwwLDAtLjIx'
    'bC0uNDQtLjQ0di0uMTZaTTEzLjUsOC43MWEuNjYuNjYsMCwxLDEtLjY2LjY2LjY2LjY2LDAsMCwxLC42Ni0uNjZaIiBmaWxsPSIj'
    'ZmZjYTAwIiAvPjxwYXRoIGlkPSJmYjcwMzNlZS00NjUwLTQ4ODYtODBjNy1lZGRmNjI1NjBmODUiIGQ9Ik0xMywxN2gwYS4xNC4x'
    'NCwwLDAsMCwuMjQtLjExVjE0LjMzYS4xNi4xNiwwLDAsMC0uMDYtLjEzaDBhLjE0LjE0LDAsMCwwLS4yMi4xM3YyLjUxQS4xNS4x'
    'NSwwLDAsMCwxMywxN1oiIGZpbGw9IiNmZjkzMDAiIG9wYWNpdHk9IjAuNzUiIC8+PHJlY3QgaWQ9ImIxNjZiOGMyLWI5ZWUtNGMz'
    'ZC1iZjdhLTc3YmQ1Y2UwNTcwYiIgeD0iMTEuOSIgeT0iMTAuOSIgd2lkdGg9IjMuMjkiIGhlaWdodD0iMC4zOSIgcng9IjAuMTgi'
    'IGZpbGw9IiNmZjkzMDAiIG9wYWNpdHk9IjAuNzUiIC8+PHJlY3QgaWQ9ImJhZjQzN2ZlLTM0ODMtNGEyNS05NGJhLTFlODRhNjRj'
    'ZDJlYiIgeD0iMTEuOSIgeT0iMTEuNTMiIHdpZHRoPSIzLjI5IiBoZWlnaHQ9IjAuMzkiIHJ4PSIwLjE4IiBmaWxsPSIjZmY5MzAw'
    'IiBvcGFjaXR5PSIwLjc1IiAvPjwvZz48L2c+PC9zdmc+"],["storage_account","data:image/svg+xml;base64,PHN2ZyB'
    'pZD0iZjJmMDQzNDktOGFlZS00NDEzLTg0YzktYTkwNTM2MTFiMzE5IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmc'
    'iIHdpZHRoPSIxOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImFkNGM'
    '0Zjk2LTA5YWEtNGY5MS1iYTEwLTVjYjhhZDUzMGY3NCIgeDE9IjkiIHkxPSIxNS44MyIgeDI9IjkiIHkyPSI1Ljc5IiBncmFkaWV'
    'udFVuaXRzPSJ1c2VyU3BhY2VPblVzZSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjYjNiM2IzIiAvPjxzdG9wIG9mZnN'
    'ldD0iMC4yNiIgc3RvcC1jb2xvcj0iI2MxYzFjMSIgLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiNlNmU2ZTYiIC8+PC9'
    'saW5lYXJHcmFkaWVudD48L2RlZnM+PHRpdGxlPkljb24tc3RvcmFnZS04NjwvdGl0bGU+PHBhdGggZD0iTS41LDUuNzloMTdhMCw'
    'wLDAsMCwxLDAsMHY5LjQ4YS41Ny41NywwLDAsMS0uNTcuNTdIMS4wN2EuNTcuNTcsMCwwLDEtLjU3LS41N1Y1Ljc5QTAsMCwwLDA'
    'sMSwuNSw1Ljc5WiIgZmlsbD0idXJsKCNhZDRjNGY5Ni0wOWFhLTRmOTEtYmExMC01Y2I4YWQ1MzBmNzQpIiAvPjxwYXRoIGQ9Ik0'
    'xLjA3LDIuMTdIMTYuOTNhLjU3LjU3LDAsMCwxLC41Ny41N1Y1Ljc5YTAsMCwwLDAsMSwwLDBILjVhMCwwLDAsMCwxLDAsMFYyLjc'
    'zQS41Ny41NywwLDAsMSwxLjA3LDIuMTdaIiBmaWxsPSIjMzdjMmIxIiAvPjxwYXRoIGQ9Ik0yLjgxLDYuODlIMTUuMThhLjI3LjI'
    '3LDAsMCwxLC4yNi4yN3YxLjRhLjI3LjI3LDAsMCwxLS4yNi4yN0gyLjgxYS4yNy4yNywwLDAsMS0uMjYtLjI3VjcuMTZBLjI3LjI'
    '3LDAsMCwxLDIuODEsNi44OVoiIGZpbGw9IiNmZmYiIC8+PHBhdGggZD0iTTIuODIsOS42OEgxNS4xOWEuMjcuMjcsMCwwLDEsLjI'
    '2LjI3djEuNDFhLjI3LjI3LDAsMCwxLS4yNi4yN0gyLjgyYS4yNy4yNywwLDAsMS0uMjYtLjI3VjEwQS4yNy4yNywwLDAsMSwyLjg'
    'yLDkuNjhaIiBmaWxsPSIjMzdjMmIxIiAvPjxwYXRoIGQ9Ik0yLjgyLDEyLjVIMTUuMTlhLjI3LjI3LDAsMCwxLC4yNi4yN3YxLjQ'
    'xYS4yNy4yNywwLDAsMS0uMjYuMjdIMi44MmEuMjcuMjcsMCwwLDEtLjI2LS4yN1YxMi43N0EuMjcuMjcsMCwwLDEsMi44MiwxMi4'
    '1WiIgZmlsbD0iIzI1ODI3NyIgLz48L3N2Zz4="],["application_gateway","data:image/svg+xml;base64,PHN2ZyBpZD'
    '0iYWNhYzVhMzQtYTAzMi00OWY5LTkzOTctMzdmNGYxYjRhZTlhIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH'
    'dpZHRoPSIxOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImY2ZDUzOT'
    'liLWE1Y2ItNGMwOS05NmNjLTllM2JiMWFmMjM4NiIgeDE9IjkiIHkxPSIxOS4yNSIgeDI9IjkiIHkyPSItMC40NiIgZ3JhZGllbn'
    'RUcmFuc2Zvcm09InRyYW5zbGF0ZSg5IC0zLjczKSByb3RhdGUoNDUpIiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSI+PH'
    'N0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjNWU5NjI0IiAvPjxzdG9wIG9mZnNldD0iMC41NSIgc3RvcC1jb2xvcj0iIzZkYW'
    'QyYSIgLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiM3NmJjMmQiIC8+PC9saW5lYXJHcmFkaWVudD48bGluZWFyR3JhZG'
    'llbnQgaWQ9ImE0MzI0MmEyLTRkOTUtNGY2ZC04ODcwLTI3MDJjZWI2YWViYyIgeDE9IjcuNTQiIHkxPSI2LjQ0IiB4Mj0iNy41My'
    'IgeTI9IjUuMTgiIGdyYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVXNlIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiNjY2'
    'MiIC8+PHN0b3Agb2Zmc2V0PSIwLjEyIiBzdG9wLWNvbG9yPSIjZDdkN2Q3IiAvPjxzdG9wIG9mZnNldD0iMC40MiIgc3RvcC1jb2'
    'xvcj0iI2ViZWJlYiIgLz48c3RvcCBvZmZzZXQ9IjAuNzIiIHN0b3AtY29sb3I9IiNmOGY4ZjgiIC8+PHN0b3Agb2Zmc2V0PSIxIi'
    'BzdG9wLWNvbG9yPSIjZmNmY2ZjIiAvPjwvbGluZWFyR3JhZGllbnQ+PGxpbmVhckdyYWRpZW50IGlkPSJlZWRjZTZkZC03MDZhLT'
    'QzYzQtYWM2Ni02NmI3MzU3ODIyMWQiIHgxPSI5LjM2IiB5MT0iNy42OSIgeDI9IjkuMzYiIHkyPSI2LjU3IiBocmVmPSIjYTQzMj'
    'QyYTItNGQ5NS00ZjZkLTg4NzAtMjcwMmNlYjZhZWJjIiAvPjwvZGVmcz48cmVjdCB4PSIyLjgyIiB5PSIyLjgyIiB3aWR0aD0iMT'
    'IuMzUiIGhlaWdodD0iMTIuMzUiIHJ4PSIwLjU3IiB0cmFuc2Zvcm09InRyYW5zbGF0ZSgtMy43MyA5KSByb3RhdGUoLTQ1KSIgZm'
    'lsbD0idXJsKCNmNmQ1Mzk5Yi1hNWNiLTRjMDktOTZjYy05ZTNiYjFhZjIzODYpIiAvPjxnPjxwYXRoIGQ9Ik0xMC44OSwxMC41MW'
    'gyLjg0YS4xMS4xMSwwLDAsMCwuMS0uMTFWNy41N2EuMS4xLDAsMCwwLS4xOC0uMDdsLS43OC43OCwwLC4wNWEuMS4xLDAsMCwxLS'
    '4xNCwwTDkuMjYsNC45MWEuMS4xLDAsMCwwLS4xNCwwbC0uODguODhhLjExLjExLDAsMCwwLDAsLjE1bDMuNDEsMy40MWEuMTEuMT'
    'EsMCwwLDEsMCwuMTVsLS4wNS4wNS0uNzguNzhBLjExLjExLDAsMCwwLDEwLjg5LDEwLjUxWiIgZmlsbD0iI2ZmZiIgLz48cGF0aC'
    'BkPSJNNi45MiwxMC41MUg0LjA4QS4xMS4xMSwwLDAsMSw0LDEwLjRWNy41N2EuMS4xLDAsMCwxLC4xNy0uMDdsLjc5Ljc4LDAsLj'
    'A1YS4xLjEsMCwwLDAsLjE0LDBMOC41NSw0LjkxYS4xLjEsMCwwLDEsLjE0LDBsLjg4Ljg4YS4xMS4xMSwwLDAsMSwwLC4xNUw2Lj'
    'E2LDkuMzVhLjExLjExLDAsMCwwLDAsLjE1bDAsLjA1Ljc4Ljc4QS4xMS4xMSwwLDAsMSw2LjkyLDEwLjUxWiIgZmlsbD0iI2ZmZi'
    'IgLz48cGF0aCBkPSJNNi44MiwxMy4wN2wyLDJhLjEuMSwwLDAsMCwuMTQsMGwyLTJhLjEuMSwwLDAsMC0uMDctLjE3SDkuNzJhLj'
    'ExLjExLDAsMCwxLS4xLS4xVjhhLjEuMSwwLDAsMC0uMS0uMTFIOC4yOEEuMTEuMTEsMCwwLDAsOC4xNyw4VjEyLjhhLjEuMSwwLD'
    'AsMS0uMS4xSDYuODlBLjEuMSwwLDAsMCw2LjgyLDEzLjA3WiIgZmlsbD0iI2ZmZiIgLz48L2c+PHBhdGggaWQ9ImFjMGE5YzBjLT'
    'JlOGMtNDg0My1iMDJiLTJmYWRmNzZiMjdkMCIgZD0iTTEwLjY1LDcuNzhBMi43MSwyLjcxLDAsMCwxLDcuMzMsMy41bDAsMGEyLj'
    'cxLDIuNzEsMCwwLDEsMy4yOSw0LjMiIGZpbGw9IiMwMDc4ZDQiIC8+PGNpcmNsZSBjeD0iNy41MyIgY3k9IjUuNTUiIHI9IjAuOD'
    'ciIGZpbGw9InVybCgjYTQzMjQyYTItNGQ5NS00ZjZkLTg4NzAtMjcwMmNlYjZhZWJjKSIgLz48Zz48cGF0aCBkPSJNNi42Nyw3bC'
    '4xOS4zTDcsNy41YTUuOTQsNS45NCwwLDAsMSwuMzQtMS4xLjg1Ljg1LDAsMCwxLS40NS0uMjNBNi43MSw2LjcxLDAsMCwwLDYuNj'
    'csN1oiIGZpbGw9IiNmMmYyZjIiIG9wYWNpdHk9IjAuNTUiIC8+PHBhdGggZD0iTTcuMjQsNC43M0E0LDQsMCwwLDEsNywzLjhhMi'
    '40OCwyLjQ4LDAsMCwwLS4zNC40NkEzLjUxLDMuNTEsMCwwLDAsNi44Niw1LDEsMSwwLDAsMSw3LjI0LDQuNzNaIiBmaWxsPSIjZj'
    'JmMmYyIiBvcGFjaXR5PSIwLjU1IiAvPjxwYXRoIGQ9Ik04LjE3LDYuMTRhLjg3Ljg3LDAsMCwxLS40OC4yNiw0LjYyLDQuNjIsMC'
    'wwLDAsLjQ2LjQxLDQuMTQsNC4xNCwwLDAsMCwuNjUuMzlWNy4xM0EuNTMuNTMsMCwwLDEsOSw2LjczLDMuNiwzLjYsMCwwLDEsOC'
    '4xNyw2LjE0WiIgZmlsbD0iI2YyZjJmMiIgb3BhY2l0eT0iMC41NSIgLz48cGF0aCBkPSJNMTAuNzksNy4yYTQuMTQsNC4xNCwwLD'
    'AsMS0uODctLjF2MGEuNTUuNTUsMCwwLDEtLjE2LjM5LDMuODQsMy44NCwwLDAsMCwxLjA4LjA4LDIuOTMsMi45MywwLDAsMCwuMz'
    'ctLjQyWiIgZmlsbD0iI2YyZjJmMiIgb3BhY2l0eT0iMC41NSIgLz48L2c+PGNpcmNsZSBjeD0iOS4zNiIgY3k9IjcuMTMiIHI9Ij'
    'AuNTYiIGZpbGw9InVybCgjZWVkY2U2ZGQtNzA2YS00M2M0LWFjNjYtNjZiNzM1NzgyMjFkKSIgLz48Y2lyY2xlIGN4PSIxMC42NC'
    'IgY3k9IjUuNzQiIHI9IjAuNiIgZmlsbD0iI2YyZjJmMiIgLz48Zz48cGF0aCBkPSJNOS4yMSw0LjQyQTQuMjksNC4yOSwwLDAsMS'
    'wxMS4wOSw0YTIuMjQsMi4yNCwwLDAsMC0uNDctLjQ1LDQuNSw0LjUsMCwwLDAtMS40OS4zNWMtLjEsMC0uMTkuMTEtLjI5LjE2aD'
    'BhNS44NSw1Ljg1LDAsMCwxLS42My0uOTNsLS4zNC4xM2E1LjM1LDUuMzUsMCwwLDAsLjY1LDEsMy45MiwzLjkyLDAsMCwwLS42Ny'
    '41NS44Ny44NywwLDAsMSwuNDMuMzUsMy4yOSwzLjI5LDAsMCwxLC42MS0uNDgsOS4xNiw5LjE2LDAsMCwwLDEuMTcsMSwuNTcuNT'
    'csMCwwLDEsLjItLjMyQTguNDQsOC40NCwwLDAsMSw5LjIxLDQuNDJaIiBmaWxsPSIjZjJmMmYyIiBvcGFjaXR5PSIwLjU1IiAvPj'
    'xwYXRoIGQ9Ik0xMS41NSw2LjA5aDBsLS4xLDBoMGwtLjA4LDBoMGwtLjEtLjA2YS42MS42MSwwLDAsMS0uMi4zbC4xMi4wN2gwbC'
    '4xMS4wNmgwbC4yOC4xNGgwYTMuNTgsMy41OCwwLDAsMCwuMDktLjM1WiIgZmlsbD0iI2YyZjJmMiIgb3BhY2l0eT0iMC41NSIgLz'
    '48L2c+PGNpcmNsZSBjeD0iNy41MyIgY3k9IjUuNTUiIHI9IjAuODciIGZpbGw9IiNmMmYyZjIiIC8+PGNpcmNsZSBjeD0iOS4zNi'
    'IgY3k9IjcuMTMiIHI9IjAuNTYiIGZpbGw9IiNmMmYyZjIiIC8+PC9zdmc+"],["monitor","data:image/svg+xml;base64,P'
    'HN2ZyBpZD0iZTRlNzY0Y2QtMmU3My00NzJmLWI4OGEtYmY4YmZjMmMyOTk0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwM'
    'C9zdmciIHdpZHRoPSIxOCIgaGVpZ2h0PSIxOCIgdmlld0JveD0iMCAwIDE4IDE4Ij48ZGVmcz48cmFkaWFsR3JhZGllbnQgaWQ9I'
    'mFjOGIzMmRhLTMwY2EtNGI5Mi04ZDBmLThkMmQ1ZWYyNzhhMCIgY3g9IjUuNzIiIGN5PSI3LjQ1IiByPSI4LjQyIiBncmFkaWVud'
    'FRyYW5zZm9ybT0idHJhbnNsYXRlKDMuMjMgMS41MSkgc2NhbGUoMS4wMSAxLjAxKSIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT'
    '25Vc2UiPjxzdG9wIG9mZnNldD0iMC4xOCIgc3RvcC1jb2xvcj0iIzVlYTBlZiIgLz48c3RvcCBvZmZzZXQ9IjAuNTYiIHN0b3AtY'
    '29sb3I9IiM1YzlmZWUiIC8+PHN0b3Agb2Zmc2V0PSIwLjY5IiBzdG9wLWNvbG9yPSIjNTU5Y2VkIiAvPjxzdG9wIG9mZnNldD0iM'
    'C43OCIgc3RvcC1jb2xvcj0iIzRhOTdlOSIgLz48c3RvcCBvZmZzZXQ9IjAuODYiIHN0b3AtY29sb3I9IiMzOTkwZTQiIC8+PHN0b'
    '3Agb2Zmc2V0PSIwLjkzIiBzdG9wLWNvbG9yPSIjMjM4N2RlIiAvPjxzdG9wIG9mZnNldD0iMC45OSIgc3RvcC1jb2xvcj0iIzA4N'
    '2JkNiIgLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiMwMDc4ZDQiIC8+PC9yYWRpYWxHcmFkaWVudD48cmFkaWFsR3JhZ'
    'GllbnQgaWQ9ImE5OTU0ODIwLWNiMzgtNDk0My05ODQyLWYwYTU2OGQyYjAxNSIgY3g9IjI4LjE4IiBjeT0iMjAyLjI5IiByPSIyL'
    'jciIGdyYWRpZW50VHJhbnNmb3JtPSJ0cmFuc2xhdGUoLTE3Ljc3IC0xODUuMDEpIHNjYWxlKDAuOTUpIiBncmFkaWVudFVuaXRzP'
    'SJ1c2VyU3BhY2VPblVzZSI+PHN0b3Agb2Zmc2V0PSIwLjE5IiBzdG9wLWNvbG9yPSIjOGM4ZTkwIiAvPjxzdG9wIG9mZnNldD0iM'
    'C4zNSIgc3RvcC1jb2xvcj0iIzg0ODY4OCIgLz48c3RvcCBvZmZzZXQ9IjAuNiIgc3RvcC1jb2xvcj0iIzZlNzA3MSIgLz48c3Rvc'
    'CBvZmZzZXQ9IjAuOTEiIHN0b3AtY29sb3I9IiM0YTRiNGMiIC8+PHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjM2UzZjNmI'
    'iAvPjwvcmFkaWFsR3JhZGllbnQ+PC9kZWZzPjx0aXRsZT5JY29uLW1hbmFnZS0zMTc8L3RpdGxlPjxlbGxpcHNlIGN4PSI5IiBje'
    'T0iOSIgcng9IjguNSIgcnk9IjguNDciIGZpbGw9InVybCgjYWM4YjMyZGEtMzBjYS00YjkyLThkMGYtOGQyZDVlZjI3OGEwKSIgL'
    'z48ZWxsaXBzZSBjeD0iOSIgY3k9IjkiIHJ4PSI3LjQiIHJ5PSI3LjM3IiBmaWxsPSIjZmZmIiAvPjxwYXRoIGQ9Ik0yLjcyLDkuN'
    'DRhNi4yNCw2LjI0LDAsMCwwLDEuODIsNGwyLTJhMy41MywzLjUzLDAsMCwxLTEtMloiIGZpbGw9IiM5Y2ViZmYiIC8+PHBhdGggZ'
    'D0iTTEzLjEzLDQuMjdBNi4yNSw2LjI1LDAsMCwwLDkuNDQsMi43NFY1LjUzYTMuNDEsMy40MSwwLDAsMSwxLjcxLjdaIiBmaWxsP'
    'SIjMzJiZWRkIiAvPjxwYXRoIGQ9Ik00Ljg3LDQuMjdsMiwyYTMuNDEsMy40MSwwLDAsMSwxLjcxLS43VjIuNzRBNi4yNSw2LjI1L'
    'DAsMCwwLDQuODcsNC4yN1oiIGZpbGw9IiMzMmJlZGQiIC8+PHBhdGggZD0iTTExLjc4LDYuODVhMy42LDMuNiwwLDAsMSwuNzEsM'
    'S43MWgyLjc5YTYuMTYsNi4xNiwwLDAsMC0xLjUzLTMuNjdaIiBmaWxsPSIjMzJiZWRkIiAvPjxwYXRoIGQ9Ik02LjIyLDYuODVsL'
    'TItMkE2LjE2LDYuMTYsMCwwLDAsMi43Miw4LjU2SDUuNTFBMy42LDMuNiwwLDAsMSw2LjIyLDYuODVaIiBmaWxsPSIjNTBlNmZmI'
    'iAvPjxwYXRoIGQ9Ik0xNC4xNCw3YS40NS40NSwwLDAsMC0uNTctLjI1TDkuNDUsOC40MWwuMzIuODEsNC4xMi0xLjYzQS40NC40N'
    'CwwLDAsMCwxNC4xNCw3WiIgZmlsbD0iI2YwNDA0OSIgLz48ZWxsaXBzZSBjeD0iOSIgY3k9IjkiIHJ4PSIxLjIiIHJ5PSIxLjIiI'
    'GZpbGw9InVybCgjYTk5NTQ4MjAtY2IzOC00OTQzLTk4NDItZjBhNTY4ZDJiMDE1KSIgLz48L3N2Zz4="]]);\n  /* OFFICIAL_A'
    'ZURE_ICON_DATA_END */\n  const byId = id => document.getElementById(id);\n  const isObject = value => '
    'value !== null && typeof value === "object" && !Array.isArray(value);\n  const text = value => value '
    '=== null || value === undefined || value === "" ? "No evaluado" : (typeof value === "object" ? JSON.'
    'stringify(value) : String(value));\n  const node = (tag, value, className) => {\n    const result = do'
    'cument.createElement(tag);\n    if (value !== undefined) result.textContent = text(value);\n    if (cl'
    'assName) result.className = className;\n    return result;\n  };\n  const add = (parent, ...children) ='
    '> { parent.append(...children); return parent; };\n  const svgNode = (tag, attributes = {}, value) =>'
    ' {\n    const result = document.createElementNS("http://www.w3.org/2000/svg", tag);\n    for (const [k'
    'ey, item] of Object.entries(attributes)) result.setAttribute(key, String(item));\n    if (value !== u'
    'ndefined) result.textContent = String(value);\n    return result;\n  };\n  const empty = message => nod'
    'e("p", message, "empty");\n  const percentage = value => value === null ? "No evaluado" : `${value.to'
    'LocaleString("es-ES", {maximumFractionDigits: 1})} / 100`;\n  const utc = value => {\n    if (value =='
    '= null || value === undefined || value === "") return "No evaluado";\n    const date = new Date(value'
    ');\n    if (!Number.isFinite(date.getTime())) return `${text(value)} (fecha no normalizada)`;\n    ret'
    'urn `${new Intl.DateTimeFormat("es-ES", {year: "numeric", month: "short", day: "2-digit", hour: "2-d'
    'igit", minute: "2-digit", second: "2-digit", timeZone: "UTC"}).format(date)} UTC`;\n  };\n  const samp'
    'leWindowText = value => `${utc(value.timestamp)} / duración observada: ${text(value.duration)}`;\n  c'
    'onst windowText = value => {\n    if (value === null || value === undefined) return "No evaluado";\n  '
    '  if (Array.isArray(value)) return value.length ? `Ventanas de muestra (${value.length}): ${value.ma'
    'p(sampleWindowText).join("; ")}` : "No evaluado: sin ventanas de muestra incorporadas.";\n    if (Obj'
    'ect.hasOwn(value, "start") || Object.hasOwn(value, "end")) return `${utc(value.start)} - ${utc(value'
    '.end)}`;\n    return `Ventana de muestra: ${sampleWindowText(value)}`;\n  };\n  const assert = (conditi'
    'on, message) => { if (!condition) throw new Error(message); };\n  const string = (value, path, nullab'
    'le = false) => assert((nullable && (value === null || value === undefined)) || typeof value === "str'
    'ing", `${path}: se esperaba texto.`);\n  const array = (value, path, nullable = false) => assert((nul'
    'lable && value === null) || Array.isArray(value), `${path}: se esperaba una colección.`);\n  const co'
    'unt = (value, path, nullable = false) => assert((nullable && (value === null || value === undefined)'
    ') || (Number.isSafeInteger(value) && value >= 0), `${path}: recuento no válido.`);\n  const object = '
    '(value, path) => assert(isObject(value), `${path}: estructura no válida.`);\n  const enumValue = (val'
    'ue, labels, path) => assert(labels.has(value), `${path}: valor no admitido.`);\n  const armIdentity ='
    ' (value, path) => {\n    string(value, path);\n    const parts = value.split("/");\n    assert(value.le'
    'ngth <= 2048 && parts.length >= 9 && parts.length % 2 === 1 && parts[0] === "" &&\n      parts[1].toL'
    'owerCase() === "subscriptions" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(parts[2]) &&'
    '\n      parts[3].toLowerCase() === "resourcegroups" && parts[5].toLowerCase() === "providers" &&\n    '
    '  parts.slice(2).every(part => /^[A-Za-z0-9_.()-]+$/.test(part) && ![".", ".."].includes(part)), `${'
    'path}: ID ARM completo no válido.`);\n    return value.toLowerCase();\n  };\n  const sampleWindow = (va'
    'lue, path) => {\n    object(value, path);\n    assert(Object.hasOwn(value, "timestamp") || Object.hasO'
    'wn(value, "duration"), `${path}: faltan los campos de la ventana de muestra.`);\n    string(value.tim'
    'estamp, `${path}.timestamp`, true); string(value.duration, `${path}.duration`, true);\n  };\n  const o'
    'bservedWindow = (value, path) => {\n    if (value === null || value === undefined) return;\n    if (Ar'
    'ray.isArray(value)) {\n      for (const [index, item] of value.entries()) sampleWindow(item, `${path}'
    '[${index}]`);\n      return;\n    }\n    object(value, path);\n    if (Object.hasOwn(value, "start") || '
    'Object.hasOwn(value, "end")) {\n      string(value.start, `${path}.start`, true); string(value.end, `'
    '${path}.end`, true);\n    } else sampleWindow(value, path);\n  };\n  const recordIdentity = (row, path)'
    ' => {\n    object(row, path);\n    for (const key of ["resource_id", "source_id", "observed_at", "clus'
    'ter_name", "namespace", "name", "title", "uid", "kind", "workload_name"]) string(row[key], `${path}.'
    '${key}`, true);\n  };\n  const canonical = value => JSON.stringify(value, (_key, item) => {\n    if (!i'
    'sObject(item)) return item;\n    const result = Object.create(null);\n    for (const key of Object.key'
    's(item).sort()) result[key] = item[key];\n    return result;\n  });\n  const validateTree = root => {\n '
    '   const pending = [[root, 0]];\n    while (pending.length) {\n      const [value, depth] = pending.po'
    'p();\n      assert(depth <= 40, "El modelo supera la profundidad de lectura admitida.");\n      if (va'
    'lue !== null && typeof value === "object") {\n        for (const item of Object.values(value)) pendin'
    'g.push([item, depth + 1]);\n      }\n    }\n  };\n  const validateScore = (score, path) => {\n    object('
    'score, path);\n    for (const key of ["passed", "failed", "evaluated", "not_evaluated", "not_applicab'
    'le", "total"]) count(score[key], `${path}.${key}`);\n    assert(score.evaluated === score.passed + sc'
    'ore.failed, `${path}: recuento de evaluadas incoherente.`);\n    assert(score.total === score.evaluat'
    'ed + score.not_evaluated + score.not_applicable, `${path}: total incoherente.`);\n    assert(score.ev'
    'aluated === 0 ? score.score === null : (Number.isFinite(score.score) && score.score >= 0 && score.sc'
    'ore <= 100), `${path}: índice ausente o no válido; sin evaluadas debe ser null.`);\n    const eligibl'
    'e = score.evaluated + score.not_evaluated;\n    assert(eligible === 0 ? score.coverage_percent === nu'
    'll : (Number.isFinite(score.coverage_percent) && score.coverage_percent >= 0 && score.coverage_perce'
    'nt <= 100), `${path}: cobertura no válida.`);\n  };\n  const validateScoreCounts = (score, checks, pat'
    'h) => {\n    const counts = new Map([...STATUS.keys()].map(key => [key, 0]));\n    for (const check of'
    ' checks) counts.set(check.status, counts.get(check.status) + 1);\n    assert(score.total === checks.l'
    'ength && score.passed === counts.get("passed") && score.failed === counts.get("failed") &&\n      sco'
    're.not_evaluated === counts.get("not_evaluated") && score.not_applicable === counts.get("not_applica'
    'ble"), `${path}: los recuentos no coinciden con los registros aportados.`);\n  };\n  const validateChe'
    'ck = (check, path, sources) => {\n    object(check, path);\n    for (const key of ["check_id", "title"'
    ', "resource_id"]) { string(check[key], `${path}.${key}`); assert(check[key].length > 0, `${path}.${k'
    'ey}: dato vacío.`); }\n    for (const key of ["rule_id", "checklist_id", "namespace", "workload_name"'
    ', "business_impact", "recommendation", "learn_url", "checklist_url", "kubernetes_url", "reason"]) st'
    'ring(check[key], `${path}.${key}`, true);\n    if (check.kubernetes_ref !== null && check.kubernetes_'
    'ref !== undefined) {\n      object(check.kubernetes_ref, `${path}.kubernetes_ref`);\n      for (const '
    'key of ["uid", "kind", "namespace", "name"]) string(check.kubernetes_ref[key], `${path}.kubernetes_r'
    'ef.${key}`, true);\n    }\n    enumValue(check.status, STATUS, `${path}.status`); enumValue(check.leve'
    'l, LEVEL, `${path}.level`);\n    enumValue(check.area, AREA, `${path}.area`); enumValue(check.pillar,'
    ' PILLAR, `${path}.pillar`);\n    enumValue(check.severity, SEVERITY, `${path}.severity`); enumValue(c'
    'heck.effort, EFFORT, `${path}.effort`);\n    array(check.evidence, `${path}.evidence`);\n    assert(ch'
    'eck.status !== "failed" || check.evidence.length > 0, `${path}: un hallazgo necesita evidencia aport'
    'ada.`);\n    for (const [index, evidence] of check.evidence.entries()) {\n      object(evidence, `${pa'
    'th}.evidence[${index}]`);\n      string(evidence.source_id, `${path}.evidence.source_id`);\n      asse'
    'rt(sources.has(evidence.source_id), `${path}: fuente de evidencia no registrada.`);\n      string(evi'
    'dence.resource_id, `${path}.evidence.resource_id`, true);\n      string(evidence.observed_at, `${path'
    '}.evidence.observed_at`, true);\n      assert(Object.hasOwn(evidence, "value"), `${path}: falta el va'
    'lor observado.`);\n    }\n  };\n  const validate = model => {\n    object(model, "Informe"); validateTre'
    'e(model);\n    assert(model.schema_version === "1.0", "Versión de esquema no compatible; se requiere '
    '1.0.");\n    assert(["LIVE", "SIMULATED"].includes(model.provenance), "Procedencia de los datos no vá'
    'lida.");\n    assert(["LAB", "PRODUCTION", "UNSPECIFIED"].includes(model.environment), "El entorno de'
    'be estar declarado.");\n    string(model.reference_utc, "reference_utc");\n    assert(/^\\d{4}-\\d{2}-\\d'
    '{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z$/.test(model.reference_utc) && Number.isFinite(Date.parse(model.ref'
    'erence_utc)), "La referencia debe ser una fecha UTC RFC3339.");\n    assert(new Date(model.reference_'
    'utc).toISOString().slice(0, 19) === model.reference_utc.slice(0, 19), "La referencia UTC contiene un'
    'a fecha u hora inexistente.");\n    object(model.client, "client"); string(model.client.name, "client'
    '.name");\n    assert(model.client.name.trim().length > 0, "Debe identificarse el cliente.");\n    stri'
    'ng(model.client.logo_data_uri, "client.logo_data_uri", true); string(model.client.logo_alt, "client.'
    'logo_alt", true);\n    if (model.client.logo_data_uri) assert(model.client.logo_data_uri.length <= 10'
    '00000 && /^data:image\\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(model.client.logo_data_uri), '
    '"El logo debe ser un PNG o JPEG inline acotado.");\n    object(model.cluster, "cluster");\n    for (co'
    'nst key of ["id", "name"]) { string(model.cluster[key], `cluster.${key}`); assert(model.cluster[key]'
    '.length > 0, `cluster.${key}: dato vacío.`); }\n    const clusterIdentity = armIdentity(model.cluster'
    '.id, "cluster.id");\n    for (const key of ["subscription_id", "subscription_name", "resource_group",'
    ' "region", "version", "power_state", "provisioning_state", "sku_tier", "mode"]) string(model.cluster'
    '[key], `cluster.${key}`, true);\n    array(model.sources, "sources");\n    const sources = new Set();\n'
    '    for (const source of model.sources) {\n      object(source, "source"); string(source.id, "source.'
    'id");\n      assert(source.id.length > 0 && !sources.has(source.id), "ID de fuente vacío o duplicado.'
    '"); sources.add(source.id);\n      enumValue(source.status, SOURCE_STATUS, "source.status"); string(s'
    'ource.time, "source.time", true);\n      count(source.records, "source.records", true); count(source.'
    'pages, "source.pages", true); array(source.errors, "source.errors");\n      string(source.note, "sour'
    'ce.note", true); observedWindow(source.window, "source.window");\n      for (const error of source.er'
    'rors) {\n        object(error, "source.error"); string(error.code, "source.error.code", true); string'
    '(error.message, "source.error.message", true);\n        count(error.http_status, "source.error.http_s'
    'tatus", true);\n      }\n    }\n    object(model.inventory, "inventory");\n    if (model.source_data !=='
    ' undefined && model.source_data !== null) object(model.source_data, "source_data");\n    for (const k'
    'ey of ["node_pools", "namespaces", "workloads", "related_resources"]) assert(Object.hasOwn(model.inv'
    'entory, key), `Falta inventory.${key}.`);\n    for (const [key, rows] of Object.entries(model.invento'
    'ry)) {\n      array(rows, `inventory.${key}`, true);\n      if (rows !== null) for (const row of rows)'
    ' recordIdentity(row, `inventory.${key}.registro`);\n    }\n    array(model.checks, "checks");\n    cons'
    't checks = new Map();\n    for (const check of model.checks) {\n      validateCheck(check, "check", so'
    'urces);\n      assert(!checks.has(check.check_id), "El modelo duplica un check_id; no puede presentar'
    'se el índice global.");\n      checks.set(check.check_id, check);\n    }\n    array(model.findings, "fi'
    'ndings");\n    const findings = new Set();\n    for (const finding of model.findings) {\n      const ch'
    'eck = checks.get(finding.check_id);\n      assert(check && check.status === "failed" && !findings.has'
    '(finding.check_id) && canonical(check) === canonical(finding), "Los hallazgos deben ser exactamente '
    'las comprobaciones fallidas, sin duplicados.");\n      findings.add(finding.check_id);\n    }\n    asse'
    'rt(findings.size === model.checks.filter(check => check.status === "failed").length, "Faltan hallazg'
    'os de comprobaciones fallidas.");\n    object(model.scores, "scores"); validateScore(model.scores.glo'
    'bal, "scores.global"); validateScore(model.scores.coverage, "scores.coverage");\n    validateScoreCou'
    'nts(model.scores.global, model.checks, "scores.global"); validateScoreCounts(model.scores.coverage, '
    'model.checks, "scores.coverage");\n    object(model.scores.areas, "scores.areas"); object(model.score'
    's.pillars, "scores.pillars");\n    for (const key of AREA.keys()) { validateScore(model.scores.areas['
    'key], `scores.areas.${key}`); validateScoreCounts(model.scores.areas[key], model.checks.filter(check'
    ' => check.area === key), `scores.areas.${key}`); }\n    for (const key of PILLAR.keys()) { validateSc'
    'ore(model.scores.pillars[key], `scores.pillars.${key}`); validateScoreCounts(model.scores.pillars[ke'
    'y], model.checks.filter(check => check.pillar === key), `scores.pillars.${key}`); }\n    object(model'
    '.finops, "finops");\n    array(model.finops.sources, "finops.sources");\n    for (const id of model.fi'
    'nops.sources) string(id, "finops.sources.registro");\n    for (const key of ["candidates", "cost_rows'
    '", "metrics"]) {\n      array(model.finops[key], `finops.${key}`, true);\n      if (model.finops[key] '
    '!== null) for (const row of model.finops[key]) recordIdentity(row, `finops.${key}.registro`);\n    }\n'
    '    array(model.finops.notes, "finops.notes");\n    for (const note of model.finops.notes) string(not'
    'e, "finops.notes.registro");\n    for (const key of ["actual_cost", "estimated_savings"]) assert(mode'
    'l.finops[key] === null || Number.isFinite(model.finops[key]), `finops.${key}: importe no válido.`);\n'
    '    string(model.finops.currency, "finops.currency", true);\n    observedWindow(model.finops.window, '
    '"finops.window");\n    for (const row of model.finops.candidates || []) {\n      string(row.id, "finop'
    's.candidate.id"); string(row.title, "finops.candidate.title");\n      string(row.basis, "finops.candi'
    'date.basis", true); string(row.unit, "finops.candidate.unit", true);\n      string(row.recommendation'
    ', "finops.candidate.recommendation", true); count(row.sample_count, "finops.candidate.sample_count",'
    ' true);\n      observedWindow(row.window, "finops.candidate.window");\n      assert(row.estimated_savi'
    'ngs === null || Number.isFinite(row.estimated_savings), "finops.candidate.estimated_savings: importe'
    ' no válido.");\n    }\n    array(model.actions, "actions");\n    const actions = new Set();\n    for (co'
    'nst action of model.actions) {\n      object(action, "action"); string(action.action_id, "action.acti'
    'on_id"); string(action.check_id, "action.check_id");\n      assert(action.action_id.length > 0 && !ac'
    'tions.has(action.action_id) && checks.get(action.check_id)?.status === "failed", "Una acción debe te'
    'ner ID único y referir un hallazgo aportado."); actions.add(action.action_id);\n      for (const key '
    'of ["title", "resource_id", "namespace", "workload_name", "recommendation", "learn_url", "owner"]) s'
    'tring(action[key], `action.${key}`, true);\n      enumValue(action.planning_bucket, BUCKET, "action.p'
    'lanning_bucket"); enumValue(action.effort, EFFORT, "action.effort");\n      enumValue(action.area, AR'
    'EA, "action.area"); enumValue(action.pillar, PILLAR, "action.pillar"); enumValue(action.severity, SE'
    'VERITY, "action.severity");\n    }\n    array(model.executive_messages, "executive_messages");\n    for'
    ' (const message of model.executive_messages) string(message, "executive_messages.registro");\n    obj'
    'ect(model.methodology, "methodology");\n    assert(model.methodology.score_kind === "local_assessment'
    '_indicator" && model.methodology.official_waf_score === false && model.methodology.read_only === tru'
    'e, "El método debe declarar índice local no oficial y solo lectura.");\n    array(model.methodology.l'
    'imitations, "methodology.limitations");\n    for (const item of model.methodology.limitations) string'
    '(item, "methodology.limitations.registro");\n    if (model.methodology.queries !== undefined) array(m'
    'odel.methodology.queries, "methodology.queries", true);\n    if (model.methodology.source_pins !== un'
    'defined) array(model.methodology.source_pins, "methodology.source_pins", true);\n    for (const query'
    ' of model.methodology.queries || []) {\n      object(query, "methodology.query");\n      for (const ke'
    'y of ["source_id", "description", "command"]) string(query[key], `methodology.query.${key}`, true);\n'
    '    }\n    for (const pin of model.methodology.source_pins || []) object(pin, "methodology.source_pin'
    '");\n    if (model.architecture !== undefined && model.architecture !== null) {\n      const graph = m'
    'odel.architecture; object(graph, "architecture");\n      for (const key of ["nodes", "edges", "unknow'
    'ns"]) array(graph[key], `architecture.${key}`);\n      const nodeIds = new Set(), edgeIds = new Set()'
    ';\n      const graphIdentity = (row, path) => {\n        object(row, path);\n        for (const key of '
    '["cluster_id", "provenance", "observed_at"]) string(row[key], `${path}.${key}`);\n        assert(armI'
    'dentity(row.cluster_id, `${path}.cluster_id`) === clusterIdentity && row.provenance === model.proven'
    'ance, `${path}: la relación no pertenece a este clúster o procedencia.`);\n      };\n      for (const '
    'row of graph.nodes) {\n        graphIdentity(row, "architecture.node");\n        for (const key of ["i'
    'd", "resource_id", "cluster_name", "kind", "label", "source_id"]) {\n          string(row[key], `arch'
    'itecture.node.${key}`); assert(row[key].length > 0, `architecture.node.${key}: dato vacío.`);\n      '
    '  }\n        assert(row.cluster_name === model.cluster.name && sources.has(row.source_id) && !nodeIds'
    '.has(row.id), "architecture.node: identidad duplicada, clúster incoherente o fuente no registrada.")'
    ';\n        assert(isObject(row.evidence) || Array.isArray(row.evidence), "architecture.node: falta la'
    ' evidencia estructurada.");\n        nodeIds.add(row.id);\n      }\n      for (const row of graph.edges'
    ') {\n        graphIdentity(row, "architecture.edge");\n        for (const key of ["id", "from", "to", '
    '"relation"]) { string(row[key], `architecture.edge.${key}`); assert(row[key].length > 0, `architectu'
    're.edge.${key}: dato vacío.`); }\n        array(row.source_ids, "architecture.edge.source_ids");\n    '
    '    assert(row.source_ids.length > 0 && row.source_ids.every(id => sources.has(id)), "architecture.e'
    'dge: fuente no registrada.");\n        assert(!edgeIds.has(row.id) && nodeIds.has(row.from) && nodeId'
    's.has(row.to), "architecture.edge: ID duplicado o extremos sin evidencia.");\n        assert(isObject'
    '(row.evidence) || Array.isArray(row.evidence), "architecture.edge: faltan los campos de unión.");\n  '
    '      edgeIds.add(row.id);\n      }\n      for (const row of graph.unknowns) {\n        graphIdentity(r'
    'ow, "architecture.unknown");\n        for (const key of ["code", "kind", "message", "cluster_name"]) '
    'string(row[key], `architecture.unknown.${key}`);\n        string(row.resource_id, "architecture.unkno'
    'wn.resource_id", true);\n        array(row.source_ids, "architecture.unknown.source_ids");\n        as'
    'sert(row.status === "not_evaluated" && row.cluster_name === model.cluster.name && row.source_ids.eve'
    'ry(id => sources.has(id)), "architecture.unknown: estado, clúster o fuente no válido.");\n      }\n   '
    ' }\n    if (model.available_clusters !== undefined) validateRoster(model.available_clusters);\n    if '
    '(model.selected_cluster_names !== undefined) {\n      array(model.selected_cluster_names, "selected_c'
    'luster_names");\n      assert(model.selected_cluster_names.length === 1 && model.selected_cluster_nam'
    'es[0] === model.cluster.name, "selected_cluster_names: una instantánea simple debe identificar su cl'
    'úster.");\n    }\n    return model;\n  };\n  const validateRoster = roster => {\n    array(roster, "avail'
    'able_clusters");\n    assert(roster.length >= 1 && roster.length <= 5, "available_clusters: se requie'
    're un catálogo fijo de uno a cinco clústeres.");\n    const names = new Set(), ids = new Set();\n    f'
    'or (const row of roster) {\n      object(row, "available_clusters.registro");\n      for (const key of'
    ' ["cluster_id", "cluster_name", "resource_group"]) { string(row[key], `available_clusters.${key}`); '
    'assert(row[key].length > 0, `available_clusters.${key}: dato vacío.`); }\n      const identity = armI'
    'dentity(row.cluster_id, "available_clusters.cluster_id");\n      assert(["LAB", "PRODUCTION", "UNSPEC'
    'IFIED"].includes(row.environment), "available_clusters: entorno no admitido.");\n      assert(!names.'
    'has(row.cluster_name) && !ids.has(identity), "available_clusters: identidad duplicada.");\n      name'
    's.add(row.cluster_name); ids.add(identity);\n    }\n  };\n  const validateDocument = documentModel => {'
    '\n    if (documentModel.schema_version === "1.0") return validate(documentModel);\n    object(document'
    'Model, "Informe"); validateTree(documentModel);\n    assert(documentModel.schema_version === "aks-waf'
    '-multi-cluster-v1", "Versión de esquema no compatible.");\n    validateRoster(documentModel.available'
    '_clusters);\n    string(documentModel.reference_utc, "reference_utc");\n    assert(/^\\d{4}-\\d{2}-\\d{2}'
    'T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?Z$/.test(documentModel.reference_utc) && Number.isFinite(Date.parse(docu'
    'mentModel.reference_utc)), "La referencia conjunta debe ser UTC RFC3339.");\n    assert(new Date(docu'
    'mentModel.reference_utc).toISOString().slice(0, 19) === documentModel.reference_utc.slice(0, 19), "L'
    'a referencia conjunta contiene una fecha inexistente.");\n    assert(["LIVE", "SIMULATED"].includes(d'
    'ocumentModel.provenance), "Procedencia conjunta no válida.");\n    object(documentModel.client, "clie'
    'nt"); string(documentModel.client.name, "client.name");\n    assert(documentModel.client.name.trim().'
    'length > 0, "Debe identificarse el cliente.");\n    array(documentModel.selected_cluster_names, "sele'
    'cted_cluster_names"); array(documentModel.cluster_runs, "cluster_runs");\n    const roster = new Map('
    'documentModel.available_clusters.map(row => [row.cluster_name, row]));\n    const selected = new Set('
    'documentModel.selected_cluster_names);\n    assert(selected.size >= 1 && selected.size <= 5 && select'
    'ed.size === documentModel.selected_cluster_names.length && [...selected].every(name => roster.has(na'
    'me)), "La selección conjunta no coincide con el catálogo fijo.");\n    assert(documentModel.cluster_r'
    'uns.length === selected.size, "Faltan resultados de clústeres seleccionados.");\n    const seen = new'
    ' Set();\n    for (const run of documentModel.cluster_runs) {\n      object(run, "cluster_run"); enumVa'
    'lue(run.status, RUN_STATUS, "cluster_run.status");\n      string(run.cluster_name, "cluster_run.clust'
    'er_name"); string(run.cluster_id, "cluster_run.cluster_id"); string(run.reference_utc, "cluster_run.'
    'reference_utc");\n      const identity = armIdentity(run.cluster_id, "cluster_run.cluster_id");\n     '
    ' assert(selected.has(run.cluster_name) && !seen.has(run.cluster_name) &&\n        armIdentity(roster.'
    'get(run.cluster_name).cluster_id, "available_clusters.cluster_id") === identity, "cluster_run: ident'
    'idad fuera de la selección o duplicada.");\n      seen.add(run.cluster_name);\n      if (run.snapshot '
    '=== null) {\n        assert(run.status === "error" && isObject(run.error), "cluster_run: un resultado'
    ' sin instantánea debe conservar su error.");\n      } else {\n        validate(run.snapshot);\n        '
    'assert(run.status !== "error" && armIdentity(run.snapshot.cluster.id, "cluster.id") === identity && '
    'run.snapshot.cluster.name === run.cluster_name &&\n          run.snapshot.reference_utc === run.refer'
    'ence_utc && run.snapshot.provenance === documentModel.provenance &&\n          canonical(run.snapshot'
    '.client) === canonical(documentModel.client), "cluster_run: instantánea, fecha o identidad incoheren'
    'te.");\n      }\n    }\n    object(documentModel.aggregate, "aggregate"); object(documentModel.aggregat'
    'e.scores, "aggregate.scores");\n    validateScore(documentModel.aggregate.scores.global, "aggregate.s'
    'cores.global");\n    validateScore(documentModel.aggregate.scores.coverage, "aggregate.scores.coverag'
    'e");\n    for (const [field, labels] of [["areas", AREA], ["pillars", PILLAR]]) {\n      object(docume'
    'ntModel.aggregate.scores[field], `aggregate.scores.${field}`);\n      for (const key of labels.keys()'
    ') validateScore(documentModel.aggregate.scores[field][key], `aggregate.scores.${field}.${key}`);\n   '
    ' }\n    object(documentModel.aggregate.inventory_counts, "aggregate.inventory_counts");\n    for (cons'
    't [key, value] of Object.entries(documentModel.aggregate.inventory_counts)) count(value, `aggregate.'
    'inventory_counts.${key}`, true);\n    for (const key of ["findings_total", "critical_total"]) count(d'
    'ocumentModel.aggregate[key], `aggregate.${key}`);\n    object(documentModel.aggregate.cluster_status_'
    'counts, "aggregate.cluster_status_counts");\n    for (const status of RUN_STATUS.keys()) {\n      cons'
    't expected = documentModel.cluster_runs.filter(run => run.status === status).length;\n      if (Objec'
    't.hasOwn(documentModel.aggregate.cluster_status_counts, status)) {\n        count(documentModel.aggre'
    'gate.cluster_status_counts[status], `aggregate.cluster_status_counts.${status}`);\n        assert(doc'
    'umentModel.aggregate.cluster_status_counts[status] === expected, "aggregate: recuentos de ejecucione'
    's incoherentes.");\n      } else assert(expected === 0, "aggregate: falta un recuento de ejecuciones '
    'no nulo.");\n    }\n    return documentModel;\n  };\n  const preferences = {storage: true};\n  const medi'
    'aTheme = window.matchMedia("(prefers-color-scheme: dark)");\n  const themeNote = message => { byId("t'
    'heme-note").hidden = false; byId("theme-note").textContent = message; };\n  const storageError = erro'
    'r => {\n    if (!(error instanceof DOMException) || !["SecurityError", "QuotaExceededError"].includes'
    '(error.name)) throw error;\n    preferences.storage = false;\n    themeNote("El navegador no permite g'
    'uardar el tema. La selección se mantiene solo durante esta apertura.");\n  };\n  let savedTheme = "aut'
    'o";\n  try {\n    const saved = localStorage.getItem("aks-consulting-report-theme");\n    if (saved !=='
    ' null && ["auto", "light", "dark"].includes(saved)) savedTheme = saved;\n    else if (saved !== null)'
    ' themeNote("La preferencia de tema anterior no es válida; se utiliza el tema del sistema.");\n  } cat'
    'ch (error) { storageError(error); }\n  byId("theme-mode").value = savedTheme;\n  const applyTheme = ()'
    ' => { document.documentElement.dataset.theme = byId("theme-mode").value === "auto" ? (mediaTheme.mat'
    'ches ? "dark" : "light") : byId("theme-mode").value; };\n  applyTheme();\n  byId("theme-mode").addEven'
    'tListener("change", () => {\n    applyTheme();\n    if (preferences.storage) {\n      try { localStorag'
    'e.setItem("aks-consulting-report-theme", byId("theme-mode").value); } catch (error) { storageError(e'
    'rror); }\n    }\n  });\n  mediaTheme.addEventListener("change", () => { if (byId("theme-mode").value =='
    '= "auto") applyTheme(); });\n  let model, documentModel, embeddedJson, clusterRuns;\n  try {\n    embed'
    'dedJson = byId("report-data").textContent;\n    assert(new TextEncoder().encode(embeddedJson).length '
    '<= MAX_BYTES, "El JSON completo supera el límite de 4.800.000 bytes.");\n    documentModel = validate'
    'Document(JSON.parse(embeddedJson));\n    clusterRuns = documentModel.schema_version === "1.0" ? [{clu'
    'ster_id: documentModel.cluster.id, cluster_name: documentModel.cluster.name,\n      reference_utc: do'
    'cumentModel.reference_utc, status: "ok", error: null, snapshot: documentModel}] : documentModel.clus'
    'ter_runs;\n    model = clusterRuns[0].snapshot;\n  } catch (error) {\n    document.body.dataset.status '
    '= "error";\n    byId("opening-state").setAttribute("role", "alert");\n    byId("opening-message").text'
    'Content = "No se ha podido abrir el informe. No se presentan resultados parciales.";\n    byId("openi'
    'ng-detail").textContent = `Datos no válidos: ${error.message}. Solicite al generador una nueva insta'
    'ntánea completa compatible con el esquema 1.0 o aks-waf-multi-cluster-v1.`;\n    return;\n  }\n  const '
    'sourceMap = new Map();\n  const checkMap = new Map();\n  const workloadMap = new Map();\n  const worklo'
    'adResourceCounts = new Map();\n  const workloadIdentity = record => {\n    const reference = record.ku'
    'bernetes_ref || record;\n    return {resource_id: record.resource_id, namespace: record.namespace || '
    'reference.namespace, name: record.workload_name || reference.name,\n      kind: reference.kind || rec'
    'ord.kind, uid: reference.uid || record.uid};\n  };\n  const workloadKey = record => {\n    const identi'
    'ty = workloadIdentity(record);\n    if (identity.uid) return JSON.stringify([identity.resource_id, id'
    'entity.uid]);\n    if (workloadResourceCounts.get(identity.resource_id) === 1) return identity.resour'
    'ce_id;\n    return JSON.stringify([identity.resource_id, identity.namespace, identity.kind || "", ide'
    'ntity.name]);\n  };\n  const matchesWorkload = (record, selected) => {\n    if (Object.hasOwn(record, "'
    'check_id") && (record.level !== "workload" || !record.workload_name)) return false;\n    const identi'
    'ty = workloadIdentity(record), target = workloadIdentity(selected);\n    if (identity.resource_id !=='
    ' target.resource_id || identity.namespace !== target.namespace) return false;\n    if (identity.uid &'
    '& target.uid) return identity.uid === target.uid;\n    if (!identity.name || identity.name !== target'
    '.name) return false;\n    if (identity.kind && target.kind) return identity.kind === target.kind;\n   '
    ' return [...workloadMap.values()].filter(row => row.resource_id === target.resource_id && row.namesp'
    'ace === target.namespace && row.name === target.name).length <= 1;\n  };\n  const state = {pages: new '
    'Map(), print: false, openDetails: new Map(), graphNode: "", graphKind: "", activeCluster: clusterRun'
    's[0].cluster_name,\n    filters: {search: "", area: "", namespace: "", workload: "", level: "", statu'
    's: "", severity: ""}};\n  const detail = (label, key, content) => {\n    const result = node("details"'
    ', undefined, "receipt");\n    result.dataset.detailKey = key; add(result, node("summary", label), add'
    '(node("div", undefined, "receipt-content"), content));\n    return result;\n  };\n  const facts = entri'
    'es => {\n    const result = node("dl", undefined, "facts");\n    for (const [label, value] of entries)'
    ' add(result, node("dt", label), value instanceof Node ? add(node("dd"), value) : node("dd", text(val'
    'ue)));\n    return result;\n  };\n  const rawReceipt = (row, key) => detail("Evidencia e identificación'
    ' completas", key, node("pre", JSON.stringify(row, null, 2), "json"));\n  const severity = value => no'
    'de("span", SEVERITY.get(value), `severity ${value}`);\n  const permittedLink = (value, label) => {\n  '
    '  if (!value) return node("span", "No evaluado");\n    let url;\n    try { url = new URL(value); } cat'
    'ch (error) { if (!(error instanceof TypeError)) throw error; return node("span", `${text(value)} (en'
    'lace no habilitado)`); }\n    const allowed = url.protocol === "https:" && !url.username && !url.pass'
    'word && !url.port && (\n      (url.hostname === "learn.microsoft.com" && /^\\/(?:[a-z]{2}-[a-z]{2}\\/)?'
    '(?:azure|well-architected|security|troubleshoot)\\//i.test(url.pathname)) ||\n      (url.hostname === '
    '"portal.azure.com" && url.pathname === "/")\n    );\n    if (!allowed) return node("span", `${text(val'
    'ue)} (enlace no habilitado)`, "source-url");\n    const link = node("a", label); link.href = url.href'
    '; link.target = "_blank"; link.rel = "noopener noreferrer"; return link;\n  };\n  const sourceIdentity'
    ' = row => {\n    const source = sourceMap.get(row.source_id);\n    return `${text(row.source_id)} / ${'
    'source ? SOURCE_STATUS.get(source.status) : "No evaluado"} / ${utc(row.observed_at)}`;\n  };\n  const '
    'namedRecord = (row, collection) => {\n    const wrapper = node("div");\n    const name = text(row.name'
    ' || row.title || row.resource_id);\n    if ((collection === "namespaces" && row.name) || (collection '
    '=== "workloads" && row.name && row.resource_id)) {\n      const button = node("button", name, "text-b'
    'utton"); button.type = "button";\n      button.dataset.drilldown = collection; button.dataset.resourc'
    'eId = text(row.resource_id);\n      button.addEventListener("click", () => {\n        state.filters.na'
    'mespace = collection === "namespaces" ? (row.name || "") : (row.namespace || "");\n        state.filt'
    'ers.workload = collection === "workloads" ? workloadKey(row) : "";\n        byId("filter-namespace").'
    'value = state.filters.namespace; populateWorkloads(); byId("filter-workload").value = state.filters.'
    'workload;\n        filtersChanged(false); byId("findings").scrollIntoView(); byId("findings-title").t'
    'abIndex = -1; byId("findings-title").focus({preventScroll: true});\n      });\n      wrapper.append(bu'
    'tton);\n    } else wrapper.append(node("strong", name));\n    add(wrapper, node("span", sourceIdentity'
    '(row), "cell-note"), rawReceipt(row, `inventory:${collection}:${text(row.resource_id)}:${name}`));\n '
    '   return wrapper;\n  };\n  const scoreCaption = score => `${score.evaluated} evaluadas; ${score.not_e'
    'valuated} no evaluadas; ${score.not_applicable} no aplicables.`;\n  const scoreRow = (label, score) ='
    '> {\n    const wrapper = node("div", undefined, "score-row");\n    const head = add(node("div", undefi'
    'ned, "score-row-head"), node("span", label), node("span", percentage(score.score), "score-number"));'
    '\n    const track = node("div", undefined, "score-track");\n    if (score.score !== null) {\n      trac'
    'k.setAttribute("role", "meter"); track.setAttribute("aria-label", `Índice local: ${label}`);\n      t'
    'rack.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100"); track.setAttrib'
    'ute("aria-valuenow", String(score.score));\n      track.setAttribute("aria-valuetext", `${percentage('
    'score.score)}. ${scoreCaption(score)}`);\n      const fill = node("div", undefined, "score-fill"); fi'
    'll.style.width = `${score.score}%`; track.append(fill);\n    } else track.setAttribute("aria-hidden",'
    ' "true");\n    add(wrapper, head, track, node("p", scoreCaption(score), "score-caption"));\n    return'
    ' wrapper;\n  };\n  const money = value => value === null || value === undefined ? "No evaluado" : `${v'
    'alue.toLocaleString("es-ES", {maximumFractionDigits: 2})} ${model.finops.currency || "(divisa no inf'
    'ormada)"}`;\n  const renderCover = () => {\n    for (const id of ["brand-plaque", "provenance", "cover'
    '-facts", "cover-id"]) byId(id).replaceChildren();\n    byId("client-name").textContent = model.client'
    '.name;\n    const plaque = byId("brand-plaque");\n    if (model.client.logo_data_uri) {\n      const im'
    'age = node("img"); image.src = model.client.logo_data_uri; image.alt = model.client.logo_alt || `Log'
    'o de ${model.client.name}`;\n      plaque.append(image);\n    } else plaque.append(node("span", "CLIEN'
    'TE", "brand-placeholder"));\n    const provenance = model.provenance === "LIVE" ? "Datos observados /'
    ' instantánea" : "SIMULADO / prueba de interfaz, no datos en vivo";\n    const environment = {LAB: "LA'
    'B / entorno de pruebas", PRODUCTION: "Entorno de producción declarado", UNSPECIFIED: "Entorno no esp'
    'ecificado"}[model.environment];\n    add(byId("provenance"), node("span", provenance, "label"), node('
    '"span", environment, "label"));\n    const entries = [\n      ["Clúster", model.cluster.name], ["Suscr'
    'ipción", model.cluster.subscription_name || model.cluster.subscription_id],\n      ["Grupo de recurso'
    's", model.cluster.resource_group], ["Región", model.cluster.region], ["Versión de AKS", model.cluste'
    'r.version],\n      ["Referencia UTC", utc(model.reference_utc)], ["Estado de energía del clúster", mo'
    'del.cluster.power_state],\n      ["Aprovisionamiento del clúster", model.cluster.provisioning_state],'
    ' ["SKU / modalidad", `${text(model.cluster.sku_tier)} / ${text(model.cluster.mode)}`]\n    ];\n    for'
    ' (const [label, value] of entries) {\n      const wrapper = add(node("div"), node("dt", label), node('
    '"dd", value));\n      if (label === "Referencia UTC") wrapper.className = "reference-fact";\n      byI'
    'd("cover-facts").append(wrapper);\n    }\n    add(byId("cover-id"), node("p", `ID del clúster: ${model'
    '.cluster.id}`), node("p", `ID de suscripción: ${text(model.cluster.subscription_id)}`));\n    byId("c'
    'over-id").append(detail("Alcance e identidad completos del clúster", "cluster-identity", node("pre",'
    ' JSON.stringify(model.cluster, null, 2), "json")));\n    byId("footer-reference").textContent = `${mo'
    'del.client.name} / ${model.cluster.name} / ${environment} / ${provenance} / ${utc(model.reference_ut'
    'c)} / esquema ${model.schema_version}.`;\n  };\n  const renderExecutive = () => {\n    for (const id of'
    ' ["global-index", "area-scores", "pillar-scores", "global-coverage", "severity-distribution", "criti'
    'cal-findings", "executive-messages", "assessment-chart"]) byId(id).replaceChildren();\n    const scor'
    'e = model.scores.global;\n    add(byId("global-index"), node("h3", "Índice local global"), node("span'
    '", percentage(score.score), `score-value${score.score === null ? " unknown" : ""}`),\n      node("p",'
    ' scoreCaption(score)), node("p", `${score.passed} superadas / ${score.failed} con hallazgo.`, "muted'
    '"),\n      node("p", "Alcance completo. No es una calificación oficial.", "qualification"));\n    for '
    '(const [key, label] of AREA) byId("area-scores").append(scoreRow(label, model.scores.areas[key]));\n '
    '   for (const [key, label] of PILLAR) byId("pillar-scores").append(scoreRow(label, model.scores.pill'
    'ars[key]));\n    const coverage = model.scores.coverage;\n    add(byId("global-coverage"), add(node("p'
    '"), document.createTextNode("Cobertura recibida: "),\n      node("strong", coverage.coverage_percent '
    '=== null ? "No evaluado" : `${coverage.coverage_percent.toLocaleString("es-ES", {maximumFractionDigi'
    'ts: 1})} %`, "coverage-number"),\n      document.createTextNode(`. ${scoreCaption(coverage)}`)),\n    '
    '  node("p", `${coverage.total} comprobaciones únicas incorporadas. ${model.sources.length} fuentes r'
    'egistradas; consulte sus estados y límites.`, "muted"));\n    const chart = byId("assessment-chart");'
    '\n    const drawing = svgNode("svg", {viewBox: "0 0 220 220", role: "img", "aria-labelledby": "assess'
    'ment-svg-title assessment-svg-description"});\n    add(drawing, svgNode("title", {id: "assessment-svg'
    '-title"}, "Recuento de comprobaciones por estado; no es un gráfico de salud"),\n      svgNode("desc",'
    ' {id: "assessment-svg-description"}, `${score.passed} superadas; ${score.failed} con hallazgo; ${sco'
    're.not_evaluated} no evaluadas; ${score.not_applicable} no aplicables. ${score.total} en total.`),\n '
    '     svgNode("circle", {cx: 110, cy: 110, r: 84, fill: "none", stroke: "var(--line)", "stroke-width"'
    ': 20}));\n    let offset = 0;\n    const segments = [["passed", "Superadas", score.passed, "var(--acce'
    'nt)", "Evaluadas sin hallazgo local"],\n      ["failed", "Con hallazgo", score.failed, "var(--high)",'
    ' "Evaluadas con evidencia de fallo"],\n      ["not_evaluated", "No evaluadas", score.not_evaluated, "'
    'var(--field-line)", "Configuración o intención desconocida"],\n      ["not_applicable", "No aplicable'
    's", score.not_applicable, "var(--surface)", "Fuera de los denominadores"]];\n    for (const [, , numb'
    'er, color] of segments) {\n      if (score.total && number) {\n        const length = number / score.t'
    'otal * 100;\n        drawing.append(svgNode("circle", {cx: 110, cy: 110, r: 84, fill: "none", stroke:'
    ' color, "stroke-width": 20,\n          pathLength: 100, "stroke-dasharray": `${length} ${100 - length'
    '}`, "stroke-dashoffset": -offset, transform: "rotate(-90 110 110)"}));\n        offset += length;\n   '
    '   }\n    }\n    add(drawing, svgNode("text", {x: 110, y: 108, "text-anchor": "middle", class: "chart-'
    'total"}, score.total),\n      svgNode("text", {x: 110, y: 132, "text-anchor": "middle", class: "chart'
    '-unit"}, "comprobaciones"));\n    const chartReading = add(node("div"), node("h3", "Qué se ha podido '
    'evaluar", undefined));\n    chartReading.firstChild.id = "assessment-chart-title";\n    const table = '
    'node("table"); table.append(node("caption", "Datos del gráfico: alcance completo de este clúster"));'
    '\n    const header = node("tr");\n    for (const label of ["Estado", "Cantidad", "Lectura"]) { const c'
    'ell = node("th", label); cell.scope = "col"; header.append(cell); }\n    const body = node("tbody");\n'
    '    for (const [key, label, number, , meaning] of segments) {\n      const swatch = node("span", unde'
    'fined, `status-swatch ${key}`); swatch.setAttribute("aria-hidden", "true");\n      const status = add'
    '(node("th"), swatch, document.createTextNode(label)); status.scope = "row";\n      body.append(add(no'
    'de("tr"), status, node("td", number), node("td", meaning)));\n    }\n    add(table, add(node("thead"),'
    ' header), body); chartReading.append(table);\n    add(chart, drawing, chartReading, node("figcaption"'
    ', "El índice local describe solo las evaluadas; la cobertura muestra cuánto queda por conocer. No ev'
    'aluado no es un resultado superado, y No aplicable no mejora la cobertura."));\n    const sourceLink '
    '= node("a", "Consultar el registro de fuentes"); sourceLink.href = "#sources-title"; byId("global-co'
    'verage").append(sourceLink);\n    const distribution = byId("severity-distribution");\n    distributio'
    'n.append(node("h3", "Distribución de severidad de los hallazgos"));\n    const track = node("div", un'
    'defined, "distribution-track"); track.setAttribute("aria-hidden", "true");\n    const legend = node("'
    'div", undefined, "distribution-legend");\n    for (const [key, label] of SEVERITY) {\n      const numb'
    'er = model.findings.filter(finding => finding.severity === key).length;\n      if (number > 0) { cons'
    't segment = node("div", undefined, `distribution-segment ${key}`); segment.style.width = `${number /'
    ' model.findings.length * 100}%`; track.append(segment); }\n      add(legend, add(node("span"), severi'
    'ty(key), document.createTextNode(` ${number}`)));\n    }\n    add(distribution, track, legend, node("p'
    '", `${model.findings.length} hallazgos aportados. Los no evaluados no se representan como hallazgos '
    'ni como resultados sanos.`, "score-caption"));\n    const critical = model.findings.filter(finding =>'
    ' finding.severity === "critical");\n    byId("critical-title").textContent = `Prioridades críticas / '
    '${critical.length} registradas`;\n    if (!critical.length) byId("critical-findings").append(empty("0'
    ' hallazgos críticos registrados. No equivale a ausencia de riesgo; revise la cobertura y las fuentes'
    ' no evaluadas."));\n    else {\n      const list = node("ol", undefined, "priority-list");\n      for ('
    'const finding of critical.slice(0, 5)) {\n        const item = node("li");\n        add(item, node("st'
    'rong", `${finding.check_id} / ${finding.title}`), node("p", `${LEVEL.get(finding.level)} / ${text(fi'
    'nding.namespace)} / ${finding.resource_id}`, "muted"),\n          node("p", finding.business_impact),'
    ' node("p", finding.recommendation));\n        list.append(item);\n      }\n      add(byId("critical-fin'
    'dings"), list, node("p", `Se muestran ${Math.min(5, critical.length)} de ${critical.length}. La rela'
    'ción completa está en Hallazgos.`, "qualification"));\n    }\n    if (!model.executive_messages.length'
    ') byId("executive-messages").append(node("li", "No evaluado: el motor no ha incorporado mensajes eje'
    'cutivos."));\n    for (const message of model.executive_messages) byId("executive-messages").append(n'
    'ode("li", message));\n  };\n  const fillOptions = (select, labels) => { for (const [value, label] of l'
    'abels) { const option = node("option", label); option.value = value; select.append(option); } };\n  c'
    'onst namespaces = new Set();\n  const populateWorkloads = () => {\n    const select = byId("filter-wor'
    'kload"); select.replaceChildren(node("option", "Todas las cargas")); select.firstChild.value = "";\n '
    '   const rows = [...workloadMap.entries()].filter(([, row]) => !state.filters.namespace || row.names'
    'pace === state.filters.namespace);\n    rows.sort(([, left], [, right]) => `${left.namespace}/${left.'
    'name}/${text(left.kind)}`.localeCompare(`${right.namespace}/${right.name}/${text(right.kind)}`, "es"'
    '));\n    for (const [key, row] of rows) { const option = node("option", `${text(row.namespace)} / ${t'
    'ext(row.name)}${row.kind ? ` / ${row.kind}` : ""}`); option.value = key; select.append(option); }\n  '
    '  if (!rows.some(([key]) => key === state.filters.workload)) state.filters.workload = "";\n    select'
    '.value = state.filters.workload;\n  };\n  fillOptions(byId("filter-area"), AREA); fillOptions(byId("fi'
    'lter-level"), LEVEL); fillOptions(byId("filter-status"), STATUS); fillOptions(byId("filter-severity"'
    '), SEVERITY);\n  const containsSearch = value => !state.filters.search || JSON.stringify(value).toLoc'
    'aleLowerCase("es-ES").includes(state.filters.search.toLocaleLowerCase("es-ES"));\n  const matchedChec'
    'ks = () => model.checks.filter(check => {\n    const f = state.filters;\n    return (!f.area || check.'
    'area === f.area) && (!f.namespace || check.namespace === f.namespace) &&\n      (!f.workload || match'
    'esWorkload(check, workloadMap.get(f.workload))) && (!f.level || check.level === f.level) &&\n      (!'
    'f.status || check.status === f.status) && (!f.severity || (check.status === "failed" && check.severi'
    'ty === f.severity)) && containsSearch(check);\n  });\n  const currentScope = () => {\n    const checks '
    '= matchedChecks();\n    const ids = new Set(checks.map(check => check.resource_id));\n    const checkI'
    'ds = new Set(checks.map(check => check.check_id));\n    const namespaceIds = new Set(checks.map(check'
    ' => check.namespace).filter(Boolean));\n    const selectedWorkload = workloadMap.get(state.filters.wo'
    'rkload);\n    const filterRecord = (row, collection) => {\n      const f = state.filters;\n      const '
    'namespace = collection === "namespaces" ? row.name : row.namespace;\n      const namespaceScoped = Bo'
    'olean(namespace);\n      const idMatch = collection === "namespaces" ? namespaceIds.has(namespace) : '
    'namespaceScoped ? checks.some(check => matchesWorkload(check, row)) : ids.has(row.resource_id);\n    '
    '  if (f.namespace && namespace !== f.namespace && (namespaceScoped || !idMatch)) return false;\n     '
    ' if (f.workload) {\n        if (collection === "namespaces") { if (namespace !== selectedWorkload.nam'
    'espace) return false; }\n        else if (namespaceScoped || collection === "workloads") { if (!match'
    'esWorkload(row, selectedWorkload)) return false; }\n        else if (!idMatch) return false;\n      }\n'
    '      if ((f.area || f.level || f.status || f.severity) && !idMatch && !(collection === "namespaces"'
    ' && namespaceIds.has(namespace))) return false;\n      return containsSearch(row) || idMatch || (coll'
    'ection === "namespaces" && namespaceIds.has(namespace));\n    };\n    const inventory = Object.create('
    'null);\n    for (const [key, rows] of Object.entries(model.inventory)) inventory[key] = rows === null'
    ' ? null : rows.filter(row => filterRecord(row, key));\n    const findings = model.findings.filter(fin'
    'ding => checkIds.has(finding.check_id));\n    const candidates = model.finops.candidates === null ? n'
    'ull : model.finops.candidates.filter(row => (!state.filters.area || state.filters.area === "finops")'
    ' && filterRecord(row, "finops.candidates"));\n    const actions = model.actions.filter(action => chec'
    'kIds.has(action.check_id));\n    return {checks, findings, inventory, candidates, actions, filterReco'
    'rd};\n  };\n  const showLimit = key => state.print ? Infinity : (state.pages.get(key) || PAGE_SIZE);\n '
    ' const pagination = (container, key, total, shown, rerender) => {\n    const footer = node("div", und'
    'efined, "table-footer");\n    footer.append(node("span", `${shown} de ${total} registros coincidentes'
    '. La exportación incluye todos.`));\n    const button = node("button", shown < total ? `Mostrar ${Mat'
    'h.min(PAGE_SIZE, total - shown)} más` : "Todos los registros visibles");\n    button.type = "button";'
    ' button.disabled = shown >= total; button.dataset.moreCollection = key;\n    button.addEventListener('
    '"click", () => {\n      state.pages.set(key, showLimit(key) + PAGE_SIZE); rerender();\n      for (cons'
    't next of document.querySelectorAll("button[data-more-collection]")) if (next.dataset.moreCollection'
    ' === key) { next.focus({preventScroll: true}); break; }\n    });\n    add(footer, button); container.a'
    'ppend(footer);\n  };\n  const renderTable = (container, key, label, columns, rows, rerender) => {\n    '
    'container.replaceChildren();\n    if (rows === null) { container.append(empty(`No evaluado: no se ha '
    'incorporado la colección ${label.toLocaleLowerCase("es-ES")}. Consulte el registro de fuentes.`)); r'
    'eturn; }\n    if (!rows.length) { container.append(empty(`0 registros coincidentes en ${label.toLocal'
    'eLowerCase("es-ES")}. Un inventario sin registros incorporados no demuestra ausencia de recursos o d'
    'e riesgo.`)); return; }\n    const wrapper = node("div", undefined, "table-scroll"); wrapper.tabIndex'
    ' = 0; wrapper.setAttribute("role", "region"); wrapper.setAttribute("aria-label", `${label}: tabla de'
    'splazable`);\n    const table = node("table", undefined, "wide-table"); table.dataset.collection = ke'
    'y;\n    table.append(node("caption", `${label} / ${rows.length} registros coincidentes. Identidad, fu'
    'ente y fecha en las evidencias desplegables.`));\n    const header = node("tr");\n    for (const [titl'
    'e] of columns) { const cell = node("th", title); cell.scope = "col"; header.append(cell); }\n    tabl'
    'e.append(add(node("thead"), header));\n    const body = node("tbody"); const visible = rows.slice(0, '
    'showLimit(key));\n    for (const row of visible) {\n      const tr = node("tr");\n      for (const [, r'
    'ead] of columns) {\n        const value = read(row); const cell = node("td");\n        if (value insta'
    'nceof Node) cell.append(value); else cell.textContent = text(value);\n        tr.append(cell);\n      '
    '}\n      body.append(tr);\n    }\n    add(table, body); add(wrapper, table); container.append(wrapper);'
    ' pagination(container, key, rows.length, visible.length, rerender);\n  };\n  const renderInventory = s'
    'cope => {\n    byId("inventory-counts").replaceChildren();\n    for (const key of ["node_pools", "name'
    'spaces", "workloads", "related_resources"]) {\n      const rows = scope.inventory[key]; const item = '
    'node("span");\n      add(item, node("b", rows === null ? "No evaluado" : rows.length), document.creat'
    'eTextNode(` ${COLLECTION.get(key).toLocaleLowerCase("es-ES")}`)); byId("inventory-counts").append(it'
    'em);\n    }\n    renderTable(byId("node-pools"), "node_pools", "Pools de nodos", [\n      ["Pool / evid'
    'encia", row => namedRecord(row, "node_pools")], ["Modo / tamaño", row => `${text(row.mode)} / ${text'
    '(row.vm_size)}`],\n      ["Nodos / autoescalado", row => `${text(row.count)} / ${row.enable_auto_scal'
    'ing === true ? "Activado" : row.enable_auto_scaling === false ? "Desactivado" : "No evaluado"} (${te'
    'xt(row.min_count)} - ${text(row.max_count)})`],\n      ["Zonas / versión", row => `${text(row.availab'
    'ility_zones)} / ${text(row.version)}`], ["Aprovisionamiento del pool", row => row.provisioning_state'
    ']\n    ], scope.inventory.node_pools, renderFiltered);\n    renderTable(byId("namespaces"), "namespace'
    's", "Namespaces", [\n      ["Namespace / explorar", row => namedRecord(row, "namespaces")], ["Estado '
    'observado", row => row.phase ?? row.status],\n      ["ID de recurso", row => row.resource_id]\n    ], '
    'scope.inventory.namespaces, renderFiltered);\n    renderTable(byId("workloads"), "workloads", "Cargas'
    ' de trabajo", [\n      ["Carga / explorar", row => namedRecord(row, "workloads")], ["Namespace / tipo'
    '", row => `${text(row.namespace)} / ${text(row.kind)}`],\n      ["Réplicas listas / deseadas", row =>'
    ' `${text(row.ready_replicas)} / ${text(row.desired_replicas)}`],\n      ["Contenedores observados", r'
    'ow => row.containers === null || row.containers === undefined ? "No evaluado" : text(row.containers)'
    ']\n    ], scope.inventory.workloads, renderFiltered);\n    renderTable(byId("related-resources"), "rel'
    'ated_resources", "Recursos asociados", [\n      ["Recurso / evidencia", row => namedRecord(row, "rela'
    'ted_resources")], ["Tipo", row => row.type ?? row.resource_type ?? row.kind],\n      ["Grupo / región'
    '", row => `${text(row.resource_group)} / ${text(row.region)}`], ["ID de recurso", row => row.resourc'
    'e_id]\n    ], scope.inventory.related_resources, renderFiltered);\n    const additional = byId("techni'
    'cal-collections"); additional.replaceChildren();\n    for (const [key, rows] of Object.entries(scope.'
    'inventory)) {\n      if (["node_pools", "namespaces", "workloads", "related_resources"].includes(key)'
    ') continue;\n      const section = node("div", undefined, "inventory-section"); const label = COLLECT'
    'ION.get(key) || key;\n      const content = node("div"); add(section, node("h3", label), content); ad'
    'ditional.append(section);\n      renderTable(content, `technical:${key}`, label, [["Registro / eviden'
    'cia completa", row => namedRecord(row, key)], ["Namespace", row => row.namespace], ["ID de recurso",'
    ' row => row.resource_id]], rows, renderFiltered);\n    }\n    if (!additional.childElementCount) addit'
    'ional.append(empty("No evaluado: no se han incorporado colecciones técnicas adicionales."));\n  };\n  '
    'const renderEvidence = (evidence, key) => {\n    const item = node("li");\n    add(item, facts([["Fuen'
    'te", `${evidence.source_id} / ${sourceMap.has(evidence.source_id) ? SOURCE_STATUS.get(sourceMap.get('
    'evidence.source_id).status) : "No evaluado"}`],\n      ["Fecha de observación", utc(evidence.observed'
    '_at)], ["Recurso observado", evidence.resource_id]]),\n      node("pre", JSON.stringify(evidence.valu'
    'e, null, 2), "json"));\n    item.dataset.evidenceKey = key;\n    return item;\n  };\n  const findingDisc'
    'losure = finding => {\n    const wrapper = node("details", undefined, "finding"); wrapper.dataset.det'
    'ailKey = `finding:${finding.check_id}`;\n    const summary = node("summary");\n    add(summary, severi'
    'ty(finding.severity), node("span", finding.title, "finding-title"), node("span", `${finding.check_id'
    '} / ${LEVEL.get(finding.level)} / ${finding.resource_id}`, "finding-reference"));\n    const body = n'
    'ode("div", undefined, "finding-body");\n    add(body, facts([["ID / regla", `${finding.check_id} / ${'
    'text(finding.rule_id)}`], ["Nivel", LEVEL.get(finding.level)], ["Área", AREA.get(finding.area)],\n   '
    '   ["Pilar Well-Architected", PILLAR.get(finding.pillar)], ["Severidad", SEVERITY.get(finding.severi'
    'ty)], ["Esfuerzo orientativo", EFFORT.get(finding.effort)],\n      ["Namespace / carga", `${text(find'
    'ing.namespace)} / ${text(finding.workload_name)}`], ["Nombre del recurso", finding.resource_name],\n '
    '     ["ID del recurso", finding.resource_id], ["ID del checklist", finding.checklist_id], ["GUID del'
    ' checklist", finding.checklist_guid],\n      ["Microsoft Learn", permittedLink(finding.learn_url, "Ab'
    'rir recomendación en Microsoft Learn")],\n      ["Referencia del checklist", finding.checklist_url], '
    '["Referencia Kubernetes", finding.kubernetes_url]]));\n    add(body, node("h4", "Impacto en el negoci'
    'o"), node("p", finding.business_impact, "prose"), node("h4", "Recomendación concreta"), node("p", fi'
    'nding.recommendation, "prose"));\n    if (finding.reason) add(body, node("h4", "Motivo registrado"), '
    'node("p", finding.reason, "prose"));\n    const list = node("ol", undefined, "evidence-list");\n    fo'
    'r (const [index, evidence] of finding.evidence.entries()) list.append(renderEvidence(evidence, `${fi'
    'nding.check_id}:${index}`));\n    add(body, node("h4", "Evidencia observada y fecha de la fuente"), l'
    'ist, rawReceipt(finding, `finding-raw:${finding.check_id}`));\n    return add(wrapper, summary, body)'
    ';\n  };\n  const renderFindings = scope => {\n    byId("findings-count").textContent = `${scope.finding'
    's.length} hallazgos coincidentes de ${model.findings.length} incorporados. ${scope.checks.filter(che'
    'ck => check.status === "not_evaluated").length} comprobaciones no evaluadas en este alcance.`;\n    c'
    'onst list = byId("findings-list"); list.replaceChildren(); byId("findings-pagination").replaceChildr'
    'en();\n    const visible = scope.findings.slice(0, showLimit("findings"));\n    for (const [level, lab'
    'el] of LEVEL) {\n      const group = visible.filter(finding => finding.level === level);\n      if (!g'
    'roup.length) continue;\n      const section = node("div", undefined, "findings-group"); section.appen'
    'd(node("h3", label === "Carga de trabajo" ? "Cargas de trabajo" : label === "Recurso asociado" ? "Re'
    'cursos asociados" : label));\n      for (const finding of group) section.append(findingDisclosure(fin'
    'ding));\n      list.append(section);\n    }\n    if (!scope.findings.length) list.append(empty("0 halla'
    'zgos coincidentes. Puede deberse a los filtros o a falta de evaluación; no implica una configuración'
    ' correcta. Consulte las comprobaciones y las fuentes."));\n    else pagination(byId("findings-paginat'
    'ion"), "findings", scope.findings.length, visible.length, renderFiltered);\n    renderTable(byId("che'
    'cks-table"), "checks", "Comprobaciones y cobertura", [\n      ["Comprobación / evidencia", check => a'
    'dd(node("div"), node("strong", `${check.check_id} / ${check.title}`), rawReceipt(check, `check:${che'
    'ck.check_id}`))],\n      ["Estado / motivo", check => add(node("div"), node("strong", STATUS.get(chec'
    'k.status)), node("span", check.reason, "cell-note"))],\n      ["Área / pilar", check => `${AREA.get(c'
    'heck.area)} / ${PILLAR.get(check.pillar)}`], ["Recurso", check => check.resource_id]\n    ], scope.ch'
    'ecks, renderFiltered);\n  };\n  const renderFinops = scope => {\n    const costSource = model.finops.so'
    'urces.map(id => sourceMap.get(id)).find(source => source?.cost_type === "ActualCost") || sourceMap.g'
    'et("azure_cost");\n    const observed = add(node("div", undefined, "finops-value"), node("h3", "Coste'
    ' observado"),\n      node("strong", money(model.finops.actual_cost), `financial-amount${model.finops.'
    'actual_cost === null ? " unknown" : ""}`),\n      node("p", "Importe aportado por la fuente de factur'
    'ación; no es una estimación ni una factura por namespace.", "qualification"));\n    const savings = a'
    'dd(node("div", undefined, "finops-value savings"), node("h3", "Ahorro estimado"),\n      node("strong'
    '", model.finops.estimated_savings === null ? "Por estimar" : money(model.finops.estimated_savings),\n'
    '        `financial-amount${model.finops.estimated_savings === null ? " unknown" : ""}`),\n      node('
    '"p", model.finops.estimated_savings === null ?\n        "No calculado. Faltan precios aplicables, una'
    ' línea base representativa y los supuestos de la propuesta. Un candidato no equivale a ahorro." :\n  '
    '      "Estimación aportada por el motor, no ahorro realizado. Revise la ventana, los supuestos y la '
    'elegibilidad antes de decidir.", "qualification"));\n    byId("finops-baseline").replaceChildren(add('
    'node("div", undefined, "finops-values"), observed, savings),\n      facts([["Periodo de facturación",'
    ' windowText(model.finops.window)], ["Divisa observada", model.finops.currency],\n        ["Ámbito exa'
    'cto de lectura de coste", costSource?.scope], ["Asociación de facturación / no propiedad", costSourc'
    'e?.association],\n        ["Fuente de coste / estado", costSource ? `${costSource.id} / ${SOURCE_STAT'
    'US.get(costSource.status)}` : "No evaluado"],\n        ["Lectura completa de facturación", model.fino'
    'ps.cost_source_complete === true ? "Completa según el recibo" : model.finops.cost_source_complete =='
    '= false ? "No completa" : "No evaluado"],\n        ["Fuentes declaradas", model.finops.sources.length'
    ' ? model.finops.sources.map(id => `${id} / ${sourceMap.has(id) ? SOURCE_STATUS.get(sourceMap.get(id)'
    '.status) : "No evaluado"}`).join("; ") : "No evaluado"],\n        ["Supuestos de ahorro", model.finop'
    's.estimated_savings === null ? "No calculado; no se ha aportado un modelo de ahorro." : "Consulte la'
    's notas y la evidencia de la estimación aportada."]]));\n    byId("finops-baseline").append(node("p",'
    ' "Los filtros de namespace o workload no asignan el coste a esa carga. El periodo y el ámbito conser'
    'van el recibo de facturación completo; los registros pueden incluir activos históricos, eliminados o'
    ' hijos de VMSS.", "qualification"));\n    byId("finops-notes").replaceChildren();\n    for (const note'
    ' of model.finops.notes) byId("finops-notes").append(node("li", note));\n    if (!model.finops.notes.l'
    'ength) byId("finops-notes").append(node("li", "No evaluado: no se han incorporado notas de cualifica'
    'ción financiera."));\n    renderTable(byId("finops-candidates"), "finops-candidates", "Candidatos Fin'
    'Ops", [\n      ["Candidato / fuente", row => add(node("div"), node("strong", row.title), node("span",'
    ' `${text(row.id)} / ${text(row.resource_id)} / ${text(row.source_id)}`, "cell-note"), rawReceipt(row'
    ', `candidate:${text(row.id)}`))],\n      ["Base / muestra", row => `${text(row.basis)} / ${text(row.v'
    'alue)} ${text(row.unit)} / ${text(row.sample_count)} muestras / ${windowText(row.window)}`],\n      ['
    '"Recomendación aportada", row => row.recommendation], ["Ahorro estimado", row => row.estimated_savin'
    'gs === null ? "Por estimar / No calculado" : money(row.estimated_savings)]\n    ], scope.candidates, '
    'renderFiltered);\n    const receipts = byId("finops-receipts"); receipts.replaceChildren();\n    for ('
    'const [key, label] of [["cost_rows", "Costes incorporados"], ["metrics", "Utilización incorporada"]]'
    ') {\n      const content = node("div"); add(receipts, node("h3", label), content);\n      const rows ='
    ' model.finops[key] === null ? null : model.finops[key].filter(row => (!state.filters.area || state.f'
    'ilters.area === "finops") && scope.filterRecord(row, `finops.${key}`));\n      renderTable(content, `'
    'finops:${key}`, label, [["Registro completo", row => rawReceipt(row, `${key}:${text(row.resource_id)'
    '}:${JSON.stringify(row)}`)]], rows, renderFiltered);\n    }\n  };\n  const graphIconKey = kind => {\n   '
    ' const value = kind.toLowerCase();\n    if (["aks", "cluster", "managed_cluster", "agent_pool", "node'
    '_pool", "microsoft.containerservice/managedclusters", "microsoft.containerservice/managedclusters/ag'
    'entpools"].includes(value)) return "aks";\n    if (["load_balancer", "public_load_balancer", "private'
    '_load_balancer", "azure_load_balancer", "load_balancer_frontend_public", "load_balancer_frontend_pri'
    'vate",\n      "load_balancer_backend_pool", "load_balancer_probe", "load_balancer_rule", "microsoft.n'
    'etwork/loadbalancers"].includes(value)) return "load_balancer";\n    if (["vnet", "subnet", "virtual_'
    'network", "microsoft.network/virtualnetworks", "microsoft.network/virtualnetworks/subnets"].includes'
    '(value)) return "virtual_network";\n    if (["nsg", "network_security_group", "microsoft.network/netw'
    'orksecuritygroups"].includes(value)) return "network_security_group";\n    if (["disk", "managed_disk'
    '", "microsoft.compute/disks"].includes(value)) return "disk";\n    if (["acr", "container_registry", '
    '"microsoft.containerregistry/registries"].includes(value)) return "container_registry";\n    if (["ke'
    'y_vault", "microsoft.keyvault/vaults"].includes(value)) return "key_vault";\n    if (["identity", "ma'
    'naged_identity", "microsoft.managedidentity/userassignedidentities"].includes(value)) return "manage'
    'd_identity";\n    if (["storage", "storage_account", "microsoft.storage/storageaccounts"].includes(va'
    'lue)) return "storage_account";\n    return null;\n  };\n  const product = (iconKey, label, description'
    ') => {\n    const wrapper = node("div", undefined, "architecture-product");\n    if (AZURE_ICONS.has(i'
    'conKey)) {\n      const image = node("img"); image.src = AZURE_ICONS.get(iconKey); image.alt = ""; im'
    'age.className = "architecture-product-icon";\n      wrapper.append(image);\n    }\n    const reading = '
    'add(node("div"), node("strong", label));\n    if (description) reading.append(node("span", descriptio'
    'n, "cell-note"));\n    return add(wrapper, reading);\n  };\n  const renderReference = () => {\n    const'
    ' reference = byId("architecture-reference");\n    if (reference.childElementCount) return;\n    add(re'
    'ference, add(node("div", undefined, "architecture-note"), node("h3", "Propuesta de referencia - NO d'
    'esplegada"),\n      node("p", "Patrón condicional inspirado en Azure Architecture Center y los cinco '
    'pilares Well-Architected. No es un mapa del cliente, un proyecto ejecutado ni una cotización."),\n   '
    '   node("p", "Validar SLA, RTO/RPO, aislamiento, regiones, tráfico, dependencias y presupuesto con l'
    'os responsables antes de elegir componentes.", "qualification")));\n    const diagram = node("figure"'
    ', undefined, "reference-diagram"); diagram.setAttribute("aria-label", "Alternativas de entrada y sep'
    'aración de una arquitectura AKS de referencia, no desplegada");\n    const entries = node("div", unde'
    'fined, "reference-entry");\n    add(entries, add(node("div"), product("application_gateway", "Azure A'
    'pplication Gateway + WAF", "Opción HTTP(S): protección de aplicación según el modelo de amenazas."))'
    ',\n      add(node("div"), product("load_balancer", "Azure Load Balancer público / ELB", "Alternativa '
    'L4 cuando se necesite exposición pública; no proporciona WAF.")));\n    const network = node("div", u'
    'ndefined, "reference-network");\n    add(network, node("h3", "Azure Virtual Network / propuesta de su'
    'bnets separadas"));\n    const ingress = add(node("div", undefined, "reference-lane"), node("h4", "Su'
    'bnet de ingreso"),\n      product("load_balancer", "Azure Load Balancer privado / ILB", "Frontend pri'
    'vado para el patrón de entrada interna, cuando corresponda."),\n      node("p", "El controlador o gat'
    'eway de Kubernetes debe elegirse y operarse explícitamente; su presencia no se deduce de un Service.'
    '", "qualification"));\n    const cluster = add(node("div", undefined, "reference-lane"), node("h4", "'
    'Subnet de AKS"),\n      product("aks", "Azure Kubernetes Service (AKS)", "Pools System y User separad'
    'os; zonas y escala según SLA y capacidad regional."),\n      add(node("div", undefined, "reference-wo'
    'rkloads"),\n        node("p", "Pool System: componentes de plataforma y capacidad mínima admitida."),'
    '\n        node("p", "Pool User: aplicaciones, namespaces por equipo, quotas y NetworkPolicy.")));\n   '
    ' add(network, add(node("div", undefined, "reference-lanes"), ingress, cluster));\n    add(diagram, en'
    'tries, node("p", "Alternativas de entrada; no representan dos caminos desplegados. Revisar TLS, ruta'
    's y exposición.", "reference-link"), network);\n    const platform = add(node("div", undefined, "refe'
    'rence-platform"),\n      product("container_registry", "Azure Container Registry", "Solo si la cadena'
    ' de suministro y las dependencias aprobadas lo requieren."),\n      product("key_vault", "Azure Key V'
    'ault", "Acceso por identidad y alcance mínimo; no se leen secretos en esta auditoría."),\n      produ'
    'ct("monitor", "Azure Monitor", "Telemetría y SLO definidos; habilitar una extensión no prueba entreg'
    'a ni salud."));\n    add(diagram, platform, node("figcaption", "Las subnets y enlaces de esta propues'
    'ta son decisiones de diseño pendientes. Ningún icono demuestra que el recurso exista en el clúster o'
    'bservado."));\n    reference.append(diagram);\n    const decisions = node("div", undefined, "reference'
    '-decisions");\n    add(decisions, node("h3", "Decisiones y contrapartidas que debe cerrar el equipo")'
    ');\n    const rows = [\n      ["Fiabilidad", "Separar pools System/User y definir disponibilidad, actu'
    'alización, backups y recuperación.", "Zonas y redundancia aumentan resiliencia solo con región, capa'
    'cidad, SLA y pruebas de recuperación adecuados."],\n      ["Seguridad", "Elegir exposición pública L4'
    ' o entrada HTTP(S) protegida; usar identidades y mínimos permisos.", "ILB no equivale a WAF. Namespa'
    'ces no son una frontera de aislamiento suficiente para todos los modelos multitenant."],\n      ["Cos'
    'te", "Dimensionar con utilización representativa, costes por ámbito y precios/compromisos aplicables'
    '.", "Gateways, redundancia, telemetría y endpoints tienen coste. No se asignan precios ni porcentaje'
    's de ahorro desde este diagrama."],\n      ["Operaciones", "Documentar IaC, propietarios, SLO, actual'
    'izaciones y políticas por namespace.", "La ausencia de IaC en la auditoría no demuestra drift ni cre'
    'ación manual."],\n      ["Rendimiento", "Validar requests/limits, autoscaling, rutas y capacidad con '
    'pruebas representativas.", "Una muestra actual no demuestra capacidad sostenida, rendimiento de apli'
    'cación ni un derecho a reducir SKU."]\n    ];\n    renderTable(decisions.appendChild(node("div")), "re'
    'ference-decisions", "Propuesta condicional para revisión humana",\n      [["Pilar", row => row[0]], ['
    '"Decisión propuesta", row => row[1]], ["Supuesto / contrapartida", row => row[2]]], rows, renderRefe'
    'rence);\n    decisions.querySelector("caption").textContent = "Cinco decisiones condicionales de refe'
    'rencia; no son hallazgos ni evidencia de recursos observados.";\n    decisions.querySelector(".table-'
    'footer").remove();\n    reference.append(decisions);\n    const links = node("p", undefined, "qualific'
    'ation");\n    add(links, permittedLink("https://learn.microsoft.com/en-us/azure/architecture/referenc'
    'e-architectures/containers/aks/baseline-aks", "AKS baseline / Azure Architecture Center"),\n      doc'
    'ument.createTextNode(" · "), permittedLink("https://learn.microsoft.com/en-us/azure/architecture/gui'
    'de/multitenant/service/aks", "AKS multitenant / namespaces"),\n      document.createTextNode(" · "), '
    'permittedLink("https://learn.microsoft.com/en-us/azure/well-architected/service-guides/azure-kuberne'
    'tes-service", "Guía AKS / cinco pilares"));\n    add(reference, links, node("p", "Referencias oficial'
    'es consultadas el 13 oct 2026. Iconos Microsoft Azure V24: uso en diagramas y documentación; se cons'
    'ervan formas y proporciones. No son el logotipo de este informe.", "qualification"));\n  };\n  const g'
    'raphKindLabel = kind => GRAPH_KINDS.get(kind) || kind;\n  const graphNamespace = row => {\n    const e'
    'vidence = isObject(row.evidence) ? row.evidence : {};\n    return evidence.namespace || evidence.kube'
    'rnetes_ref?.namespace || "";\n  };\n  const graphWorkloadMatches = (row, selected) => {\n    const evid'
    'ence = isObject(row.evidence) ? row.evidence : {};\n    const reference = evidence.kubernetes_ref || '
    'evidence;\n    const target = workloadIdentity(selected);\n    if (graphNamespace(row) !== target.name'
    'space) return false;\n    if (reference.uid && target.uid) return reference.uid === target.uid;\n    r'
    'eturn reference.name === target.name && reference.kind === target.kind;\n  };\n  const renderArchitect'
    'ure = scope => {\n    const graph = model.architecture;\n    for (const id of ["architecture-observed-'
    'note", "architecture-map", "architecture-properties", "architecture-tree", "architecture-nodes", "ar'
    'chitecture-edges", "architecture-unknowns"]) byId(id).replaceChildren();\n    const kindSelect = byId'
    '("architecture-kind"), select = byId("architecture-node");\n    const note = byId("architecture-obser'
    'ved-note");\n    if (!graph) {\n      add(note, node("h3", "Arquitectura observada / No evaluado"),\n  '
    '      node("p", "Esta instantánea no incorpora un grafo de relaciones confirmadas. El inventario sig'
    'ue disponible, pero no se inventan conexiones, ingress, ELB/ILB ni dependencias."));\n      byId("arc'
    'hitecture-map").append(empty("No evaluado: no se han aportado nodos y enlaces con sus campos de unió'
    'n."));\n      kindSelect.disabled = true; select.disabled = true;\n      kindSelect.replaceChildren(no'
    'de("option", "Todos los tipos")); kindSelect.firstChild.value = "";\n      select.replaceChildren(nod'
    'e("option", "No evaluado"));\n      byId("architecture-tree").append(node("h3", "Recursos / No evalua'
    'do"));\n      byId("architecture-unknowns").append(empty("Consulte los recibos de fuente de esta inst'
    'antánea; un error no demuestra ausencia de relaciones."));\n      return;\n    }\n    kindSelect.disabl'
    'ed = false; select.disabled = false;\n    const nodeMap = new Map(graph.nodes.map(row => [row.id, row'
    ']));\n    let scoped = graph.nodes;\n    if (state.filters.namespace || state.filters.workload) {\n    '
    '  const direct = new Set(graph.nodes.filter(row => state.filters.workload ? graphWorkloadMatches(row'
    ', workloadMap.get(state.filters.workload)) :\n        graphNamespace(row) === state.filters.namespace'
    ').map(row => row.id));\n      const context = new Set(direct);\n      for (const edge of graph.edges) '
    'if (direct.has(edge.from) || direct.has(edge.to)) { context.add(edge.from); context.add(edge.to); }\n'
    '      scoped = graph.nodes.filter(row => context.has(row.id));\n    }\n    const kinds = [...new Set(s'
    'coped.map(row => row.kind))].sort((a, b) => graphKindLabel(a).localeCompare(graphKindLabel(b), "es")'
    ');\n    kindSelect.replaceChildren(node("option", "Todos los tipos")); kindSelect.firstChild.value = '
    '"";\n    fillOptions(kindSelect, kinds.map(kind => [kind, graphKindLabel(kind)]));\n    if (!kinds.inc'
    'ludes(state.graphKind)) state.graphKind = "";\n    kindSelect.value = state.graphKind;\n    const opti'
    'ons = scoped.filter(row => !state.graphKind || row.kind === state.graphKind);\n    if (!options.some('
    'row => row.id === state.graphNode)) state.graphNode = options.find(row => row.resource_id === model.'
    'cluster.id && graphIconKey(row.kind) === "aks")?.id || options[0]?.id || "";\n    select.replaceChild'
    'ren();\n    for (const row of options) { const option = node("option", `${graphKindLabel(row.kind)} /'
    ' ${row.label}`); option.value = row.id; select.append(option); }\n    if (!options.length) { const op'
    'tion = node("option", "0 recursos coincidentes / no implica ausencia"); option.value = ""; select.ap'
    'pend(option); }\n    select.value = state.graphNode;\n    add(note, node("h3", "Arquitectura observada'
    '"),\n      node("p", `${graph.nodes.length} nodos y ${graph.edges.length} relaciones aportadas; ${gra'
    'ph.unknowns.length} límites no evaluados. Referencia: ${utc(model.reference_utc)}.`),\n      node("p"'
    ', "Las flechas indican referencias o uniones de configuración confirmadas, no tráfico verificado, mo'
    'ntajes, autorización efectiva ni salud. ELB es Azure Load Balancer público; ILB es su frontend priva'
    'do, nunca un producto AWS.", "qualification"),\n      node("p", state.filters.namespace || state.filt'
    'ers.workload ? "Se muestran los objetos de la selección Kubernetes y sus vecinos confirmados; los fi'
    'ltros de área/estado/severidad no reinterpretan la topología." :\n        "Topología del clúster comp'
    'leto. Los filtros de área/estado/severidad no reinterpretan estas relaciones.", "qualification"));\n '
    '   const chosen = nodeMap.get(state.graphNode);\n    const tree = byId("architecture-tree"); tree.app'
    'end(node("h3", `Recursos / ${options.length}`));\n    const choose = id => { state.graphNode = id; re'
    'nderArchitecture(currentScope()); };\n    for (const kind of kinds) {\n      const rows = options.filt'
    'er(row => row.kind === kind);\n      if (!rows.length) continue;\n      const list = node("ul");\n     '
    ' for (const row of rows) {\n        const button = node("button", row.label); button.type = "button";'
    ' button.setAttribute("aria-pressed", String(row.id === state.graphNode));\n        button.dataset.gra'
    'phNode = row.id; button.addEventListener("click", () => choose(row.id)); list.append(add(node("li"),'
    ' button));\n      }\n      const group = node("details"); group.dataset.detailKey = `graph-kind:${kind'
    '}`; group.open = rows.some(row => row.id === state.graphNode);\n      add(group, node("summary", `${g'
    'raphKindLabel(kind)} / ${rows.length}`), list); tree.append(group);\n    }\n    const map = byId("arch'
    'itecture-map");\n    map.tabIndex = 0; map.setAttribute("role", "region");\n    if (!chosen) map.appen'
    'd(empty("0 objetos con identidad coincidente. No se utiliza el ARM ID compartido para mezclar namesp'
    'aces o workloads."));\n    else {\n      const allowedIds = new Set(scoped.map(row => row.id));\n      '
    'const connections = graph.edges.filter(edge => allowedIds.has(edge.from) && allowedIds.has(edge.to) '
    '&& (edge.from === chosen.id || edge.to === chosen.id));\n      const neighborIds = [...new Set(connec'
    'tions.map(edge => edge.from === chosen.id ? edge.to : edge.from))].filter(id => id !== chosen.id);\n '
    '     const visibleIds = new Set([chosen.id, ...neighborIds.slice(0, 12)]);\n      const visible = gra'
    'ph.nodes.filter(row => visibleIds.has(row.id));\n      const left = visible.filter(row => row.id !== '
    'chosen.id && connections.some(edge => edge.to === chosen.id && edge.from === row.id));\n      const r'
    'ight = visible.filter(row => row.id !== chosen.id && !left.includes(row));\n      const rows = Math.m'
    'ax(1, left.length, right.length), height = Math.max(240, rows * 96 + 64);\n      const positions = ne'
    'w Map([[chosen.id, {x: left.length ? 270 : 12, y: height / 2 - 36}]]);\n      for (const [index, row]'
    ' of left.entries()) positions.set(row.id, {x: 12, y: 44 + index * 96});\n      for (const [index, row'
    '] of right.entries()) positions.set(row.id, {x: 528, y: 44 + index * 96});\n      const drawing = svg'
    'Node("svg", {viewBox: `0 0 744 ${height}`, role: "group", "aria-labelledby": "architecture-svg-title'
    ' architecture-svg-description"});\n      add(drawing, svgNode("title", {id: "architecture-svg-title"}'
    ', `Relaciones confirmadas de ${chosen.label}`),\n        svgNode("desc", {id: "architecture-svg-descr'
    'iption"}, `${connections.length} relaciones directas aportadas. Los campos y registros completos est'
    'án en la tabla de relaciones; una flecha no prueba tráfico.`));\n      const marker = svgNode("marker'
    '", {id: "architecture-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: '
    '6, orient: "auto-start-reverse"});\n      marker.append(svgNode("path", {d: "M 0 0 L 10 5 L 0 10 z", '
    'fill: "var(--accent)"})); drawing.append(add(svgNode("defs"), marker));\n      for (const edge of con'
    'nections) {\n        const from = positions.get(edge.from), to = positions.get(edge.to);\n        if ('
    '!from || !to || edge.from === edge.to) continue;\n        const forward = from.x < to.x, start = {x: '
    'from.x + (forward ? 204 : 0), y: from.y + 36}, end = {x: to.x + (forward ? 0 : 204), y: to.y + 36};\n'
    '        const line = svgNode("path", {d: `M${start.x},${start.y} C${(start.x + end.x) / 2},${start.y'
    '} ${(start.x + end.x) / 2},${end.y} ${end.x},${end.y}`,\n          class: "graph-line", "marker-end":'
    ' "url(#architecture-arrow)"});\n        line.append(svgNode("title", {}, edge.relation)); drawing.app'
    'end(line);\n      }\n      for (const row of visible) {\n        const position = positions.get(row.id)'
    ';\n        const group = svgNode("g", {class: "graph-node", role: "button", tabindex: 0, "aria-label"'
    ': `Ver identidad y evidencia de ${row.label}`, "data-graph-node": row.id});\n        add(group, svgNo'
    'de("title", {}, `${graphKindLabel(row.kind)} / ${row.label} / ${row.resource_id}`),\n          svgNod'
    'e("rect", {x: position.x, y: position.y, width: 204, height: 72, rx: 4, class: `graph-box${row.id =='
    '= chosen.id ? " selected" : ""}`}));\n        const icon = graphIconKey(row.kind);\n        if (icon &'
    '& AZURE_ICONS.has(icon)) group.append(svgNode("image", {x: position.x + 12, y: position.y + 18, widt'
    'h: 36, height: 36, href: AZURE_ICONS.get(icon), preserveAspectRatio: "xMidYMid meet", "aria-hidden":'
    ' "true"}));\n        const inset = icon && AZURE_ICONS.has(icon) ? 58 : 12, limit = inset === 58 ? 19'
    ' : 27;\n        const shortened = value => value.length <= limit ? value : `${value.slice(0, limit - '
    '3)}...`;\n        const kindLabel = new Map([["load_balancer_frontend_public", "ELB / público"], ["lo'
    'ad_balancer_frontend_private", "ILB / privado"],\n          ["public_load_balancer", "ELB / Azure púb'
    'lico"], ["private_load_balancer", "ILB / Azure privado"]]).get(row.kind) || graphKindLabel(row.kind)'
    ';\n        add(group, svgNode("text", {x: position.x + inset, y: position.y + 31, class: "graph-label'
    '"}, shortened(row.label)),\n          svgNode("text", {x: position.x + inset, y: position.y + 49, cla'
    'ss: "graph-kind"}, shortened(kindLabel)));\n        group.addEventListener("click", () => choose(row.'
    'id));\n        group.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) {'
    ' event.preventDefault(); choose(row.id); byId("architecture-node").focus({preventScroll: true}); } }'
    ');\n        drawing.append(group);\n      }\n      add(map, drawing, node("figcaption", `Vecindad confi'
    'rmada: ${visible.length} nodos visibles de ${neighborIds.length + 1}; ${connections.length} relacion'
    'es directas. En pantallas pequeñas, desplace horizontalmente el diagrama. Las etiquetas abreviadas c'
    'onservan nombre, ID y evidencia completos en el selector y los recibos. No hay enlaces inferidos des'
    'de un nombre o icono.`));\n      const properties = byId("architecture-properties");\n      add(proper'
    'ties, node("h3", chosen.label), facts([["Tipo", graphKindLabel(chosen.kind)], ["ID observado", chose'
    'n.resource_id], ["Clave del grafo", chosen.id],\n        ["Clúster", chosen.cluster_name], ["Fuente",'
    ' `${chosen.source_id} / ${SOURCE_STATUS.get(sourceMap.get(chosen.source_id).status)}`],\n        ["Ob'
    'servado en UTC", utc(chosen.observed_at)], ["Procedencia", chosen.provenance]]), rawReceipt(chosen, '
    '`graph-node:${chosen.id}`));\n    }\n    const scopedIds = new Set(scoped.map(row => row.id));\n    ren'
    'derTable(byId("architecture-nodes"), "architecture-nodes", "Nodos observados",\n      [["Recurso / ob'
    'jeto", row => row.label], ["Tipo", row => graphKindLabel(row.kind)], ["Identidad", row => row.resour'
    'ce_id],\n        ["Fuente / fecha", row => `${row.source_id} / ${utc(row.observed_at)}`], ["Evidencia'
    ' completa", row => rawReceipt(row, `graph-node-record:${row.id}`)]],\n      scoped, renderFiltered);\n'
    '    renderTable(byId("architecture-edges"), "architecture-edges", "Relaciones confirmadas",\n      [['
    '"Origen", row => nodeMap.get(row.from).label], ["Relación aportada", row => row.relation], ["Destino'
    '", row => nodeMap.get(row.to).label],\n        ["Fuentes / fecha", row => `${row.source_ids.join(", "'
    ')} / ${utc(row.observed_at)}`], ["Campos de unión", row => rawReceipt(row, `graph-edge:${row.id}`)]]'
    ',\n      graph.edges.filter(row => scopedIds.has(row.from) && scopedIds.has(row.to)), renderFiltered)'
    ';\n    renderTable(byId("architecture-unknowns"), "architecture-unknowns", "Topología no evaluada",\n '
    '     [["Tipo / código", row => `${graphKindLabel(row.kind)} / ${row.code}`], ["No evaluado / motivo"'
    ', row => row.message],\n        ["Fuentes / evidencia", row => add(node("div"), node("span", row.sour'
    'ce_ids.join(", ")), rawReceipt(row, `graph-unknown:${row.code}:${text(row.resource_id)}`))]],\n      '
    'graph.unknowns, renderFiltered);\n  };\n  const renderActions = scope => {\n    const plan = byId("acti'
    'on-plan"); plan.replaceChildren();\n    for (const [key, [label, note]] of BUCKET) {\n      const sect'
    'ion = node("section", undefined, "action-bucket"); const content = node("div");\n      add(section, n'
    'ode("h3", label), node("p", note, "qualification"), content); plan.append(section);\n      const rows'
    ' = scope.actions.filter(action => action.planning_bucket === key);\n      renderTable(content, `actio'
    'ns:${key}`, label, [\n        ["Acción / comprobación", action => add(node("div"), node("strong", act'
    'ion.title), node("span", `${action.action_id} / ${action.check_id} / ${text(action.resource_id)}`, "'
    'cell-note"), rawReceipt(action, `action:${action.action_id}`))],\n        ["Prioridad / esfuerzo", ac'
    'tion => add(node("div"), severity(action.severity), node("span", `Esfuerzo ${EFFORT.get(action.effor'
    't).toLocaleLowerCase("es-ES")}`, "cell-note"))],\n        ["Impacto / recomendación", action => add(n'
    'ode("div"), node("p", checkMap.get(action.check_id).business_impact), node("p", action.recommendatio'
    'n), permittedLink(action.learn_url, "Microsoft Learn"))],\n        ["Responsable", action => action.o'
    'wner === null || action.owner === undefined || action.owner === "" ? "Sin asignar" : text(action.own'
    'er)]\n      ], rows, renderFiltered);\n    }\n  };\n  const scopeDescription = () => {\n    const f = sta'
    'te.filters;\n    return `Alcance seleccionado: ${model.cluster.name}; área ${f.area ? AREA.get(f.area'
    ') : "todas"}; namespace ${f.namespace || "todos"}; carga ${f.workload ? text(workloadMap.get(f.workl'
    'oad)?.name) : "todas"}; nivel ${f.level ? LEVEL.get(f.level) : "todos"}; estado ${f.status ? STATUS.'
    'get(f.status) : "todos"}; severidad ${f.severity ? SEVERITY.get(f.severity) : "todas"}; búsqueda ${f'
    '.search || "sin filtro"}.`;\n  };\n  const renderScope = scope => {\n    const evaluated = scope.checks'
    '.filter(check => ["passed", "failed"].includes(check.status)).length;\n    const unknown = scope.chec'
    'ks.filter(check => check.status === "not_evaluated").length;\n    const na = scope.checks.filter(chec'
    'k => check.status === "not_applicable").length;\n    byId("filter-count").textContent = `${scope.chec'
    'ks.length} comprobaciones coincidentes de ${model.checks.length}; ${evaluated} evaluadas, ${unknown}'
    ' no evaluadas y ${na} no aplicables. Los índices mantienen el alcance completo.`;\n    byId("print-sc'
    'ope").textContent = `${scopeDescription()} Se imprimen todos los registros coincidentes, sin límite '
    'de página de interfaz. Los índices del resumen conservan el alcance completo.`;\n    const path = byI'
    'd("scope-path"); path.replaceChildren();\n    const resetPart = (label, clear) => {\n      const butto'
    'n = node("button", label); button.type = "button";\n      button.addEventListener("click", () => { cl'
    'ear(); synchronizeFilters(); filtersChanged(false); }); return button;\n    };\n    path.append(resetP'
    'art(model.cluster.name, () => { state.filters.area = ""; state.filters.namespace = ""; state.filters'
    '.workload = ""; }));\n    if (state.filters.area) add(path, node("span", "/"), resetPart(AREA.get(sta'
    'te.filters.area), () => { state.filters.namespace = ""; state.filters.workload = ""; }));\n    if (st'
    'ate.filters.namespace) add(path, node("span", "/"), resetPart(state.filters.namespace, () => { state'
    '.filters.workload = ""; }));\n    if (state.filters.workload) add(path, node("span", "/"), node("span'
    '", workloadMap.get(state.filters.workload)?.name, "scope-part"));\n  };\n  const renderFiltered = () ='
    '> {\n    const opened = new Map([...document.querySelectorAll("details")].map(item => [item.dataset.d'
    'etailKey, item.open]));\n    const scope = currentScope(); renderScope(scope); renderInventory(scope)'
    '; renderArchitecture(scope); renderFindings(scope); renderFinops(scope); renderActions(scope);\n    f'
    'or (const item of document.querySelectorAll("details")) item.open = state.print || (opened.has(item.'
    'dataset.detailKey) ? opened.get(item.dataset.detailKey) : item.open);\n  };\n  const synchronizeFilter'
    's = () => {\n    for (const key of ["search", "area", "namespace", "level", "status", "severity"]) by'
    'Id(`filter-${key}`).value = state.filters[key];\n    populateWorkloads();\n  };\n  const filtersChanged'
    ' = (read = true) => {\n    if (read) for (const key of ["search", "area", "namespace", "workload", "l'
    'evel", "status", "severity"]) state.filters[key] = byId(`filter-${key}`).value;\n    state.pages.clea'
    'r(); renderFiltered();\n  };\n  byId("filters-form").addEventListener("submit", event => event.prevent'
    'Default());\n  byId("filter-search").addEventListener("input", () => filtersChanged());\n  for (const '
    'key of ["area", "namespace", "workload", "level", "status", "severity"]) byId(`filter-${key}`).addEv'
    'entListener("change", () => {\n    if (key === "namespace") { state.filters.namespace = byId("filter-'
    'namespace").value; state.filters.workload = ""; populateWorkloads(); }\n    filtersChanged();\n  });\n '
    ' byId("reset-filters").addEventListener("click", () => {\n    for (const key of Object.keys(state.fil'
    'ters)) state.filters[key] = "";\n    synchronizeFilters(); filtersChanged(false);\n  });\n  const rende'
    'rMethod = () => {\n    const opened = new Map([...document.querySelectorAll("#methodology details")].'
    'map(item => [item.dataset.detailKey, item.open]));\n    byId("method-scope").replaceChildren();\n    a'
    'dd(byId("method-scope"), facts([["Solo lectura", "Declarado por el motor"], ["Tipo de índice", model'
    '.methodology.score_kind]]),\n      detail("Alcance declarado de la evaluación", "method-scope", node('
    '"pre", JSON.stringify(model.methodology.scope ?? null, null, 2), "json")));\n    renderTable(byId("so'
    'urces-table"), "sources", "Fuentes de la instantánea completa", [\n      ["Fuente / recibo", source ='
    '> add(node("div"), node("strong", source.id), rawReceipt(source, `source:${source.id}`))],\n      ["E'
    'stado", source => SOURCE_STATUS.get(source.status)], ["Referencia UTC", source => utc(source.time)],'
    '\n      ["Registros / páginas", source => `${text(source.records)} / ${text(source.pages)}`],\n      ['
    '"Límites / errores", source => {\n        const wrapper = node("div", source.note);\n        if (sourc'
    'e.window) wrapper.append(node("span", windowText(source.window), "cell-note"));\n        const list ='
    ' node("ul", undefined, "source-errors");\n        for (const error of source.errors) list.append(node'
    '("li", `${text(error.code)} / HTTP ${text(error.http_status)} / ${text(error.message)}`));\n        w'
    'rapper.append(list); return wrapper;\n      }]\n    ], model.sources, () => { renderMethod(); if (stat'
    'e.print) for (const item of document.querySelectorAll("details")) item.open = true; });\n    const so'
    'urceData = byId("source-data-receipts"); sourceData.replaceChildren();\n    if (model.source_data ==='
    ' undefined || model.source_data === null) sourceData.append(empty("No evaluado: el modelo no incorpo'
    'ra observaciones adicionales."));\n    else sourceData.append(node("pre", JSON.stringify(model.source'
    '_data, null, 2), "json"));\n    const pins = byId("catalog-pins"); pins.replaceChildren();\n    const '
    'coverage = model.methodology.catalog_coverage;\n    pins.append(facts([["Elementos del catálogo", cov'
    'erage?.total_items], ["Elementos mapeados", coverage?.mapped_items], ["Elementos sin automatizar", c'
    'overage?.unmapped_items]]));\n    add(pins, node("p", "La cobertura de catálogo no es la cobertura de'
    ' evaluación. Las preguntas de arquitectura o intención no automatizadas requieren revisión humana.",'
    ' "qualification"),\n      detail("Snapshot y versiones del catálogo", "catalog-snapshot", node("pre",'
    ' JSON.stringify(model.methodology.catalogSnapshot ?? null, null, 2), "json")));\n    const references'
    ' = model.methodology.source_pins;\n    if (!references || !references.length) pins.append(empty("No e'
    'valuado: no se han incorporado referencias fijadas."));\n    else for (const [index, pin] of referenc'
    'es.entries()) pins.append(rawReceipt(pin, `pin:${index}`));\n    const queries = byId("queries"); que'
    'ries.replaceChildren();\n    if (!model.methodology.queries || !model.methodology.queries.length) que'
    'ries.append(empty("No evaluado: el modelo no incorpora firmas de consulta ni límites de lectura."));'
    '\n    else for (const [index, query] of model.methodology.queries.entries()) {\n      const section = '
    'node("div", undefined, "query");\n      add(section, node("h4", query.description || query.source_id)'
    ', facts([["Fuente", query.source_id], ["Límite de lectura", query.read_limit]]),\n        node("pre",'
    ' query.command, "command"), rawReceipt(query, `query:${index}`)); queries.append(section);\n    }\n   '
    ' const limitations = byId("limitations"); limitations.replaceChildren();\n    if (!model.methodology.'
    'limitations.length) limitations.append(empty("No evaluado: no se han incorporado limitaciones declar'
    'adas. No implica ausencia de limitaciones."));\n    else { const list = node("ul", undefined, "finops'
    '-notes"); for (const item of model.methodology.limitations) list.append(node("li", item)); limitatio'
    'ns.append(list); }\n    for (const item of document.querySelectorAll("#methodology details")) item.op'
    'en = state.print || (opened.get(item.dataset.detailKey) || false);\n  };\n  const csvValue = value => '
    '{\n    let content = value === null || value === undefined ? "" : (typeof value === "object" ? JSON.s'
    'tringify(value) : String(value));\n    if (/^[\\s\\u0000-\\u001f]*[=+\\-@]/u.test(content) || /^[\\t\\r\\n]/'
    'u.test(content)) content = `\'${content}`;\n    return `"${content.replace(/"/g, \'""\')}"`;\n  };\n  cons'
    't download = (filename, content, type) => {\n    const blob = new Blob([content], {type}); const url '
    '= URL.createObjectURL(blob);\n    const link = node("a"); link.href = url; link.download = filename; '
    'link.hidden = true; document.body.append(link); link.click(); link.remove();\n    setTimeout(() => UR'
    'L.revokeObjectURL(url), 1000);\n  };\n  const exportCsv = (filename, columns, rows) => {\n    const lin'
    'es = [columns.map(([label]) => csvValue(label)).join(",")];\n    for (const row of rows) lines.push(c'
    'olumns.map(([, read]) => csvValue(read(row))).join(","));\n    download(filename, `\\ufeff${lines.join'
    '("\\r\\n")}\\r\\n`, "text/csv;charset=utf-8");\n  };\n  const checkColumns = [\n    ["check_id", row => row'
    '.check_id], ["checklist_id", row => row.checklist_id], ["title", row => row.title],\n    ["status", r'
    'ow => row.status], ["level", row => row.level], ["area", row => row.area], ["pillar", row => row.pil'
    'lar],\n    ["severity", row => row.severity], ["resource_id", row => row.resource_id], ["namespace", '
    'row => row.namespace],\n    ["workload_name", row => row.workload_name], ["evidence", row => row.evid'
    'ence], ["recommendation", row => row.recommendation],\n    ["learn_url", row => row.learn_url], ["eff'
    'ort", row => row.effort], ["reason", row => row.reason], ["record_json", row => row]\n  ];\n  byId("ex'
    'port-json").addEventListener("click", () => download("aks-review-completo.json", embeddedJson, "appl'
    'ication/json;charset=utf-8"));\n  byId("export-filtered-json").addEventListener("click", () => {\n    '
    'const scope = currentScope();\n    const finops = Object.create(null);\n    for (const [key, value] of'
    ' Object.entries(model.finops)) finops[key] = key === "candidates" ? scope.candidates : (["cost_rows"'
    ', "metrics"].includes(key) && value !== null ? value.filter(row => (!state.filters.area || state.fil'
    'ters.area === "finops") && scope.filterRecord(row, `finops.${key}`)) : value);\n    const exported = '
    'Object.create(null);\n    for (const [key, value] of Object.entries(model)) exported[key] = value;\n  '
    '  exported.inventory = scope.inventory; exported.checks = scope.checks; exported.findings = scope.fi'
    'ndings; exported.finops = finops; exported.actions = scope.actions;\n    exported.filter_context = st'
    'ate.filters;\n    exported.scope_note = "Los índices, fuentes, source_data, metodología y topología c'
    'onservan la instantánea completa del clúster; las colecciones filtradas contienen todos los registro'
    's coincidentes, no solo la página visible.";\n    if (documentModel.schema_version === "1.0") downloa'
    'd("aks-review-alcance.json", JSON.stringify(exported), "application/json;charset=utf-8");\n    else {'
    '\n      const wrapper = Object.create(null);\n      for (const [key, value] of Object.entries(document'
    'Model)) wrapper[key] = value;\n      wrapper.cluster_runs = documentModel.cluster_runs.map(run => run'
    '.cluster_name === state.activeCluster ? {...run, snapshot: exported} : run);\n      wrapper.filter_co'
    'ntext = {cluster_name: state.activeCluster, ...state.filters};\n      wrapper.scope_note = "Solo la i'
    'nstantánea del clúster seleccionado contiene colecciones filtradas. Los demás resultados, errores, s'
    'napshots y el agregado conservan su alcance original completo. La topología y los recibos no se reco'
    'rtan.";\n      download("aks-review-alcance.json", JSON.stringify(wrapper), "application/json;charset'
    '=utf-8");\n    }\n  });\n  byId("export-findings").addEventListener("click", () => exportCsv("aks-revie'
    'w-hallazgos.csv", checkColumns, currentScope().findings));\n  byId("export-checks").addEventListener('
    '"click", () => exportCsv("aks-review-comprobaciones.csv", checkColumns, currentScope().checks));\n  b'
    'yId("export-inventory").addEventListener("click", () => {\n    const rows = [];\n    for (const [colle'
    'ction, records] of Object.entries(currentScope().inventory)) if (records !== null) for (const record'
    ' of records) rows.push({collection, record});\n    exportCsv("aks-review-inventario.csv", [["collecti'
    'on", row => row.collection], ["resource_id", row => row.record.resource_id], ["namespace", row => ro'
    'w.record.namespace],\n      ["name", row => row.record.name], ["source_id", row => row.record.source_'
    'id], ["observed_at", row => row.record.observed_at], ["record_json", row => row.record]], rows);\n  }'
    ');\n  byId("export-finops").addEventListener("click", () => exportCsv("aks-review-finops.csv", [["id"'
    ', row => row.id], ["title", row => row.title],\n    ["resource_id", row => row.resource_id], ["source'
    '_id", row => row.source_id], ["window", row => row.window], ["sample_count", row => row.sample_count'
    '],\n    ["estimated_savings", row => row.estimated_savings], ["recommendation", row => row.recommenda'
    'tion], ["record_json", row => row]], currentScope().candidates || []));\n  byId("export-actions").add'
    'EventListener("click", () => exportCsv("aks-review-plan.csv", [["action_id", row => row.action_id], '
    '["check_id", row => row.check_id],\n    ["resource_id", row => row.resource_id], ["planning_bucket", '
    'row => row.planning_bucket], ["severity", row => row.severity], ["effort", row => row.effort],\n    ['
    '"owner", row => row.owner], ["recommendation", row => row.recommendation], ["record_json", row => ro'
    'w]], currentScope().actions));\n  const preparePrint = () => {\n    if (state.print || !model) return;'
    '\n    state.openDetails = new Map([...document.querySelectorAll("details")].map(item => [item.dataset'
    '.detailKey, item.open]));\n    renderReference(); state.print = true; renderFiltered(); renderMethod('
    ');\n    for (const item of document.querySelectorAll("details")) item.open = true;\n  };\n  const resto'
    'rePrint = () => {\n    if (!state.print) return;\n    state.print = false; renderFiltered(); renderMet'
    'hod();\n    for (const item of document.querySelectorAll("details")) item.open = state.openDetails.ge'
    't(item.dataset.detailKey) || false;\n    state.openDetails.clear();\n  };\n  window.addEventListener("b'
    'eforeprint", preparePrint); window.addEventListener("afterprint", restorePrint);\n  byId("print-repor'
    't").addEventListener("click", () => window.print());\n  byId("jump-section").addEventListener("change'
    '", () => {\n    const target = byId(byId("jump-section").value);\n    target.scrollIntoView(); const h'
    'eading = target.querySelector("h1, h2"); heading.tabIndex = -1; heading.focus({preventScroll: true})'
    ';\n  });\n  const setArchitectureTab = (selected, focus = false) => {\n    if (selected === "reference"'
    ') renderReference();\n    for (const key of ["observed", "reference"]) {\n      const active = key ==='
    ' selected, tab = byId(`architecture-${key}-tab`);\n      tab.setAttribute("aria-selected", String(act'
    'ive)); tab.tabIndex = active ? 0 : -1;\n      byId(`architecture-${key}`).hidden = !active;\n      if '
    '(active && focus) tab.focus({preventScroll: true});\n    }\n  };\n  for (const key of ["observed", "ref'
    'erence"]) {\n    const tab = byId(`architecture-${key}-tab`);\n    tab.addEventListener("click", () =>'
    ' setArchitectureTab(key));\n    tab.addEventListener("keydown", event => {\n      if (["ArrowLeft", "A'
    'rrowRight", "Home", "End"].includes(event.key)) {\n        event.preventDefault(); setArchitectureTab'
    '(event.key === "Home" ? "observed" : event.key === "End" ? "reference" : key === "observed" ? "refer'
    'ence" : "observed", true);\n      }\n    });\n  }\n  byId("architecture-kind").addEventListener("change"'
    ', () => { state.graphKind = byId("architecture-kind").value; state.graphNode = ""; renderArchitectur'
    'e(currentScope()); });\n  byId("architecture-node").addEventListener("change", () => { state.graphNod'
    'e = byId("architecture-node").value; renderArchitecture(currentScope()); });\n  const renderClusterOv'
    'erview = () => {\n    const multi = documentModel.schema_version !== "1.0", roster = documentModel.av'
    'ailable_clusters || [];\n    byId("cluster-overview").hidden = !multi && roster.length <= 1;\n    cons'
    't selector = byId("cluster-selector"); selector.replaceChildren();\n    for (const run of clusterRuns'
    ') { const option = node("option", `${run.cluster_name} / ${RUN_STATUS.get(run.status)}`); option.val'
    'ue = run.cluster_name; selector.append(option); }\n    for (const row of roster) if (!clusterRuns.som'
    'e(run => run.cluster_name === row.cluster_name)) {\n      const option = node("option", `${row.cluste'
    'r_name} / disponible para nueva ejecución, no incluido`);\n      option.value = row.cluster_name; opt'
    'ion.disabled = true; selector.append(option);\n    }\n    selector.value = state.activeCluster;\n    co'
    'nst aggregate = byId("cluster-aggregate"); aggregate.replaceChildren();\n    if (multi) {\n      const'
    ' supplied = documentModel.aggregate, score = supplied.scores.global;\n      add(aggregate, facts([["Í'
    'ndice local conjunto aportado", percentage(score.score)], ["Cobertura conjunta aportada", supplied.s'
    'cores.coverage.coverage_percent === null ? "No evaluado" : `${supplied.scores.coverage.coverage_perc'
    'ent.toLocaleString("es-ES", {maximumFractionDigits: 1})} %`],\n        ["Comprobaciones aportadas", s'
    'coreCaption(score)], ["Hallazgos / críticos", `${supplied.findings_total} / ${supplied.critical_tota'
    'l}`],\n        ["Ejecuciones recopiladas / parciales / fallidas", `${supplied.cluster_status_counts.o'
    'k ?? 0} / ${supplied.cluster_status_counts.partial ?? 0} / ${supplied.cluster_status_counts.error ??'
    ' 0}`],\n        ["Referencia conjunta UTC", utc(documentModel.reference_utc)]]),\n        node("p", "E'
    'l agregado es el recibido del motor; un clúster con error primario no tiene configuración evaluada. '
    'No se suman monedas, periodos de facturación ni ámbitos de node resource groups.", "qualification"),'
    '\n        detail("Agregado e inventarios por clúster completos", "multi-aggregate", node("pre", JSON.'
    'stringify(supplied, null, 2), "json")));\n    } else aggregate.append(node("p", "El catálogo adiciona'
    'l es configuración fija autorizada, no evidencia de presencia ni salud. Este archivo contiene una so'
    'la instantánea.", "qualification"));\n    renderTable(byId("cluster-runs-table"), "cluster-runs", "Re'
    'sultados incluidos por clúster",\n      [["Clúster", run => run.cluster_name], ["Estado de recopilaci'
    'ón", run => RUN_STATUS.get(run.status)], ["Referencia UTC", run => utc(run.reference_utc)],\n        '
    '["Error / identidad", run => rawReceipt({cluster_id: run.cluster_id, status: run.status, error: run.'
    'error}, `cluster-run:${run.cluster_name}`)]], clusterRuns, renderClusterOverview);\n    byId("export-'
    'json").textContent = multi ? "JSON completo (todos los clústeres)" : "JSON completo";\n  };\n  const i'
    'nitializeCluster = name => {\n    const run = clusterRuns.find(item => item.cluster_name === name);\n '
    '   assert(run, "Clúster no incluido en el documento.");\n    state.activeCluster = name; model = run.'
    'snapshot; state.pages.clear(); state.graphNode = ""; state.graphKind = "";\n    for (const key of Obj'
    'ect.keys(state.filters)) state.filters[key] = "";\n    for (const item of document.querySelectorAll("'
    'details")) item.open = false;\n    for (const id of ["cover", "executive", "scope-filters", "inventor'
    'y", "architecture", "findings", "finops", "actions", "methodology"]) byId(id).hidden = !model;\n    b'
    'yId("jump-section").disabled = !model; document.querySelector(".contents nav").hidden = !model;\n    '
    'document.querySelector(".report-footer").hidden = !model;\n    byId("cluster-error").hidden = Boolean'
    '(model);\n    for (const id of ["export-filtered-json", "export-inventory", "export-findings", "expor'
    't-checks", "export-finops", "export-actions", "print-report"]) byId(id).disabled = !model;\n    if (!'
    'model) {\n      byId("cluster-error").replaceChildren(node("h2", `${run.cluster_name} / No evaluado`)'
    ',\n        node("p", "La lectura primaria falló. No hay una instantánea de este clúster: no se muestr'
    'an índices, costes, workloads ni una arquitectura prestados de otra ejecución."),\n        node("pre"'
    ', JSON.stringify(run.error, null, 2), "json"));\n      return;\n    }\n    sourceMap.clear(); checkMap.'
    'clear(); workloadMap.clear(); workloadResourceCounts.clear(); namespaces.clear();\n    for (const sou'
    'rce of model.sources) sourceMap.set(source.id, source);\n    for (const check of model.checks) checkM'
    'ap.set(check.check_id, check);\n    for (const row of model.inventory.workloads || []) workloadResour'
    'ceCounts.set(row.resource_id, (workloadResourceCounts.get(row.resource_id) || 0) + 1);\n    for (cons'
    't row of model.inventory.workloads || []) if (row.resource_id && row.name) workloadMap.set(workloadK'
    'ey(row), workloadIdentity(row));\n    for (const check of model.checks) if (check.workload_name && !['
    '...workloadMap.values()].some(row => matchesWorkload(check, row))) workloadMap.set(workloadKey(check'
    '), workloadIdentity(check));\n    for (const row of model.inventory.namespaces || []) if (row.name) n'
    'amespaces.add(row.name);\n    for (const row of workloadMap.values()) if (row.namespace) namespaces.a'
    'dd(row.namespace);\n    for (const check of model.checks) if (check.namespace) namespaces.add(check.n'
    'amespace);\n    byId("filter-namespace").replaceChildren(node("option", "Todos los namespaces")); byI'
    'd("filter-namespace").firstChild.value = "";\n    fillOptions(byId("filter-namespace"), [...namespace'
    's].sort((left, right) => left.localeCompare(right, "es")).map(value => [value, value]));\n    synchro'
    'nizeFilters(); setArchitectureTab("observed");\n    renderCover(); renderExecutive(); renderFiltered('
    '); renderMethod();\n  };\n  byId("cluster-selector").addEventListener("change", () => initializeCluste'
    'r(byId("cluster-selector").value));\n  renderClusterOverview(); initializeCluster(state.activeCluster'
    ');\n  byId("opening-state").hidden = true; byId("report-shell").hidden = false;\n  byId("export-json")'
    '.disabled = false; byId("print-report").disabled = !model; document.body.dataset.status = "ready";\n}'
    ')();\n</script>\n</body>\n</html>\n'
)
# END BUILD-TIME ASSETS


class EvidenceError(RuntimeError):
    def __init__(self, code, message, source="assessment", http_status=None):
        super().__init__(message)
        self.code, self.source, self.http_status = code, source, http_status

    def public(self):
        return {"code": self.code, "message": str(self), "source_id": self.source, "http_status": self.http_status}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def obj(value, source="normalization"):
    if not isinstance(value, dict):
        raise EvidenceError("invalid_shape", "Expected a source object; no empty-success substitution.", source)
    return value


def array(value, source="normalization"):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise EvidenceError("invalid_shape", "Expected an array of source objects.", source)
    return value


def pick(value, *keys):
    result = value
    for key in keys:
        if not isinstance(result, dict) or key not in result:
            return None
        result = result[key]
    return result


def text(value, source="normalization", maximum=8192):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise EvidenceError("invalid_field", "Source text has an invalid type, control character or safety-bound length.", source)
    return value


def number(value, source="normalization"):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EvidenceError("invalid_field", "Source numeric value must be finite.", source)
    return value


def boolean(value, source="normalization"):
    if value is not None and not isinstance(value, bool):
        raise EvidenceError("invalid_field", "Source boolean field has an invalid type.", source)
    return value


def strings(value, source="normalization"):
    if value is None:
        return None
    if not isinstance(value, list):
        raise EvidenceError("invalid_field", "Source string-list field has an invalid type.", source)
    return [text(item, source) for item in value]


def labels(value, source):
    if value is None:
        return None
    return {text(key, source, 253): text(item, source, 256) for key, item in obj(value, source).items()}


def arm_id(value, subscription=None):
    value = text(value, "scope", 2048)
    if value is None:
        raise EvidenceError("invalid_resource_id", "A scoped ARM resource ID is required.", "scope")
    parts = value.split("/")
    if (
        len(parts) < 9 or parts[0] != "" or parts[1].lower() != "subscriptions"
        or parts[3].lower() != "resourcegroups" or parts[5].lower() != "providers"
        or any(not re.fullmatch(r"[A-Za-z0-9_.()-]+", part) or part in (".", "..") for part in parts[2:])
        or (subscription and parts[2].lower() != subscription.lower())
    ):
        raise EvidenceError("invalid_resource_id", "Resource ID changed the fixed subscription or has unsafe syntax.", "scope")
    try:
        UUID(parts[2])
    except ValueError as exc:
        raise EvidenceError("invalid_resource_id", "Resource ID subscription is not a GUID.", "scope") from exc
    return value


def scope_subscription(config=None):
    """Subscription of the cluster being collected: per-cluster config, the active collection, then the default."""
    return (config or {}).get("subscription_id") or ACTIVE_SUBSCRIPTION_ID or SUBSCRIPTION_ID


def same_subscription_id(value):
    """Validated ARM ID inside the collected cluster's subscription, or None for a cross-subscription reference (never read)."""
    value = arm_id(value)
    return value if value.split("/")[2].lower() == scope_subscription().lower() else None


def resource_type(resource_id):
    parts = arm_id(resource_id).split("/")
    return (parts[6] + "/" + "/".join(parts[7::2])).lower()


def guid_setting(key, value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value.lower():
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise EvidenceError("configuration_required", "Configure the explicit " + key + " before authentication.", "configuration") from exc
    return value.lower()


def validate_config(cluster_name):
    for key, value in (
        ("SUBSCRIPTION_ID", SUBSCRIPTION_ID), ("TENANT_ID", TENANT_ID),
        ("MANAGED_IDENTITY_CLIENT_ID", MANAGED_IDENTITY_CLIENT_ID),
    ):
        guid_setting(key, value)
    if not isinstance(APPROVED_CLUSTERS, dict) or not 1 <= len(APPROVED_CLUSTERS) <= MAX_APPROVED_CLUSTERS:
        raise EvidenceError("configuration_required", "Configure one to " + str(MAX_APPROVED_CLUSTERS) + " fixed approved clusters.", "configuration")
    name = cluster_name or next(iter(APPROVED_CLUSTERS))
    if name not in APPROVED_CLUSTERS:
        raise EvidenceError("invalid_parameter", "cluster_name must exactly match the fixed operator allowlist.", "parameters")
    defaults = globals().get("CLUSTER_DEFAULTS") or {}
    config = {**json.loads(json.dumps(obj(defaults, "configuration"))), **obj(APPROVED_CLUSTERS[name], "configuration")}
    subscription = guid_setting("subscription_id of " + name, config.get("subscription_id") or SUBSCRIPTION_ID)
    aks_name = config.get("aks_name") or name
    for field, value, maximum in (("name", name, 63), ("aks_name", aks_name, 63), ("resource_group", config.get("resource_group"), 90)):
        if not isinstance(value, str) or not 1 <= len(value) <= maximum or not re.fullmatch(r"[A-Za-z0-9_.()-]+", value):
            raise EvidenceError("configuration_required", "Configure a bounded approved " + field + ".", "configuration")
    if config.get("environment") not in ("LAB", "PRODUCTION", "UNSPECIFIED"):
        raise EvidenceError("configuration_required", "Set explicit LAB/PRODUCTION/UNSPECIFIED environment.", "configuration")
    for field in ("kubernetes_enabled", "include_node_resource_group", "advisor_enabled"):
        if not isinstance(config.get(field), bool):
            raise EvidenceError("configuration_required", "Configure the boolean " + field + ".", "configuration")
    if config.get("cost_scope") not in ("disabled", "resource_ids", "node_resource_group"):
        raise EvidenceError("configuration_required", "Configure disabled/resource_ids/node_resource_group cost scope.", "configuration")
    if config.get("cost_timeframe") not in ("month_to_date", "last_30_days"):
        raise EvidenceError("configuration_required", "Configure month_to_date/last_30_days billing timeframe.", "configuration")
    for field in ("approved_related_resource_ids", "approved_cost_resource_ids"):
        ids = config.get(field)
        if not isinstance(ids, list) or len(ids) > MAX_RELATED_RESOURCES or len({str(i).lower() for i in ids}) != len(ids):
            raise EvidenceError("configuration_required", "Configure distinct bounded fixed resource IDs.", "configuration")
        for value in ids:
            arm_id(value, subscription)
    requirements = obj(config.get("requirements"), "configuration")
    allowed = {
        "sla_required", "availability_zones_required", "network_policy_required", "workload_identity_required",
        "overlay_required", "policy_required", "defender_required", "autoscaling_required", "automatic_upgrades_required",
        "node_image_upgrades_required", "diagnostics_required", "maintenance_required", "quota_required",
        "pod_security_required", "cpu_limits_required", "workloads",
    }
    if set(requirements) - allowed:
        raise EvidenceError("configuration_required", "Unknown requirement key; requirements are operator-authored, not query inputs.", "configuration")
    for key, value in requirements.items():
        if key != "workloads" and value is not None and not isinstance(value, bool):
            raise EvidenceError("configuration_required", "Requirement values must be boolean or unknown.", "configuration")
    for key, values in obj(requirements.get("workloads", {}), "configuration").items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9.-]+/[a-z0-9.-]+", key):
            raise EvidenceError("configuration_required", "Workload requirements use namespace/name keys.", "configuration")
        if set(obj(values, "configuration")) - {"availability_required", "autoscaling_required", "probes_required", "network_policy_required"}:
            raise EvidenceError("configuration_required", "Unknown per-workload requirement.", "configuration")
        if any(value is not None and not isinstance(value, bool) for value in values.values()):
            raise EvidenceError("configuration_required", "Per-workload requirements must be boolean or unknown.", "configuration")
    validate_logo(CLIENT_LOGO_DATA_URI, CLIENT_LOGO_ALT)
    if not isinstance(CLIENT_NAME, str) or not 1 <= len(CLIENT_NAME) <= 120 or any(ord(c) < 32 for c in CLIENT_NAME):
        raise EvidenceError("configuration_required", "Client label must be bounded plain text.", "configuration")
    return {**config, "name": name, "aks_name": aks_name, "subscription_id": subscription,
            "id": "/subscriptions/" + subscription + "/resourceGroups/"
            + config["resource_group"] + "/providers/Microsoft.ContainerService/managedClusters/" + aks_name}


def roster_row(config):
    return {"cluster_id": config["id"], "cluster_name": config["name"],
            "resource_group": config["resource_group"], "environment": config["environment"]}


def selection_configs(cluster_name, cluster_names):
    first = validate_config(cluster_name)
    configured = [validate_config(name) for name in APPROVED_CLUSTERS]
    if len({config["id"].lower() for config in configured}) != len(configured):
        raise EvidenceError("configuration_required", "Two allowlist entries point to the same AKS resource ID.", "configuration")
    names = cluster_names.split(",") if cluster_names else [first["name"]]
    if len(set(names)) != len(names) or any(name not in APPROVED_CLUSTERS for name in names):
        raise EvidenceError("invalid_parameter", "Select distinct exact fixed allowlisted cluster names only.", "parameters")
    by_name = {config["name"]: config for config in configured}
    return [by_name[name] for name in names], configured


def list_clusters_page(configured, limit, offset):
    """Paged fixed roster (configuration, not observed health) that always fits the 3500-byte transport budget."""
    rows = [{"cluster_name": config["name"], "resource_group": config["resource_group"],
             "subscription_id": config["subscription_id"], "environment": config["environment"]}
            for config in configured]
    if len(configured) <= MAX_REPORT_CLUSTERS:
        for row, config in zip(rows, configured):
            row["cluster_id"] = config["id"]
    page = rows[offset:offset + limit]
    response = {"status": "ok", "operation": "list_clusters", "fresh_collection": False,
                "roster_basis": "fixed_operator_configuration_not_observed_inventory",
                "total": len(rows), "subscription_count": len({row["subscription_id"] for row in rows}),
                "offset": offset, "returned": 0, "next_offset": None,
                "report_batch_limit": MAX_REPORT_CLUSTERS, "available_clusters": page}
    while True:
        response["returned"] = len(response["available_clusters"])
        end = offset + response["returned"]
        response["next_offset"] = end if end < len(rows) else None
        if response_bytes(response) <= TRANSPORT_BYTES or len(response["available_clusters"]) <= 1:
            return ensure_response(response)
        response["available_clusters"] = response["available_clusters"][:-1]


def validate_parameters(operation, view, cluster_name, namespace, workload_name, area, level, severity, status, limit, offset, cluster_names=""):
    for key, value, choices in (
        ("operation", operation, ("report", "query", "list_clusters")), ("view", view, VIEWS),
        ("area", area, ("all",) + AREAS), ("level", level, ("all",) + LEVELS),
        ("severity", severity, ("all",) + SEVERITIES), ("status", status, ("all",) + CHECK_STATUSES + SOURCE_STATUSES),
    ):
        if value not in choices:
            raise EvidenceError("invalid_parameter", key + " must use its declared enum.", "parameters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise EvidenceError("invalid_parameter", "limit must be an integer from 1 to 100.", "parameters")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 500000:
        raise EvidenceError("invalid_parameter", "offset must be an integer from 0 to 500000.", "parameters")
    for key, value, maximum in (("cluster_name", cluster_name, 63), ("namespace", namespace, 63), ("workload_name", workload_name, 253)):
        if not isinstance(value, str) or len(value) > maximum or (value and not re.fullmatch(r"[A-Za-z0-9_.()-]+", value)):
            raise EvidenceError("invalid_parameter", key + " is a bounded exact local name, not a path, URL or command.", "parameters")
    if not isinstance(cluster_names, str) or len(cluster_names) > 319 or (
        cluster_names and not re.fullmatch(r"[A-Za-z0-9_.()-]{1,63}(?:,[A-Za-z0-9_.()-]{1,63}){0,4}", cluster_names)
    ):
        raise EvidenceError("invalid_parameter", "cluster_names is an exact comma-separated allowlist selection of one to five names, without spaces.", "parameters")
    if cluster_name and cluster_names:
        raise EvidenceError("invalid_combination", "Use cluster_name or cluster_names, not both.", "parameters")
    filters = any((namespace, workload_name, area != "all", level != "all", severity != "all", status != "all"))
    if operation == "report" and (view != "summary" or filters or limit != 25 or offset != 0):
        raise EvidenceError("invalid_combination", "report cannot be narrowed by query filters or paging.", "parameters")
    if operation == "list_clusters" and (view != "summary" or filters):
        raise EvidenceError("invalid_combination", "list_clusters accepts only limit/offset roster paging, not query filters.", "parameters")
    if operation == "list_clusters" and (cluster_name or cluster_names):
        raise EvidenceError("invalid_combination", "list_clusters lists the complete fixed roster; do not select a cluster.", "parameters")
    if operation == "query":
        if view in ("summary", "cluster", "node_pools", "coverage", "architecture") and (namespace or workload_name):
            raise EvidenceError("invalid_combination", "Namespace/workload filters do not apply to this view.", "parameters")
        if view not in ("findings", "actions") and any((area != "all", level != "all", severity != "all")):
            raise EvidenceError("invalid_combination", "Area/level/severity filters apply only to rule assessments or actions.", "parameters")
        if status != "all" and (
            (view == "coverage" and status not in SOURCE_STATUSES)
            or (view in ("findings", "actions") and status not in CHECK_STATUSES)
            or view not in ("coverage", "findings", "actions")
        ):
            raise EvidenceError("invalid_combination", "Status does not belong to the selected view.", "parameters")
    return dict(operation=operation, view=view, cluster_name=cluster_name, namespace=namespace, workload_name=workload_name,
                area=area, level=level, severity=severity, status=status, limit=limit, offset=offset, cluster_names=cluster_names)


def quantity(value, unit):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise EvidenceError("invalid_quantity", "Kubernetes quantity must be a bounded string.", "normalization")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[numkKMGTPE]|[KMGTPE]i)?", value)
    if not match:
        raise EvidenceError("invalid_quantity", "Invalid Kubernetes quantity; no zero substitution.", "normalization")
    suffix = match[2] or ""
    factors = {"": Decimal(1), "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"),
               "k": Decimal("1e3"), "K": Decimal("1e3"), "M": Decimal("1e6"), "G": Decimal("1e9"),
               "T": Decimal("1e12"), "P": Decimal("1e15"), "E": Decimal("1e18")}
    try:
        if suffix.startswith(("e", "E")) and len(suffix) > 1:
            exponent = int(suffix[1:])
            if not -30 <= exponent <= 30:
                raise InvalidOperation
            factor = Decimal(10) ** exponent
        else:
            factor = Decimal(2) ** (10 * ("KMGTPE".index(suffix[0]) + 1)) if suffix.endswith("i") else factors[suffix]
        result = Decimal(match[1]) * factor
        if not result.is_finite() or result < 0 or result > Decimal("1e30"):
            raise InvalidOperation
    except (InvalidOperation, KeyError, ValueError, OverflowError) as exc:
        raise EvidenceError("invalid_quantity", "Kubernetes quantity exceeds the finite nonnegative safety bound.", "normalization") from exc
    if unit == "bytes":
        return int(result.to_integral_value(rounding="ROUND_CEILING"))
    if unit == "cores":
        return float(result)
    raise EvidenceError("invalid_quantity_unit", "Quantity unit must be bytes or cores.", "normalization")


def source_receipt(source_id, reference, status="complete", note="", **extra):
    return {"id": source_id, "status": status, "time": reference, "records": 0, "pages": 0, "errors": [],
            "note": note, "window": None, **extra}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Identity:
    def __init__(self):
        try:
            from azure.identity import ManagedIdentityCredential
        except ImportError as exc:
            raise EvidenceError("dependency_missing", "Live reads require azure-identity; no cached-login fallback.", "identity") from exc
        self.credential = ManagedIdentityCredential(client_id=MANAGED_IDENTITY_CLIENT_ID)
        self.deadline = time.monotonic() + MAX_COLLECTION_SECONDS
        self.request_count = 0

    def token(self, audience):
        from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
        from azure.identity import CredentialUnavailableError
        if time.monotonic() >= self.deadline:
            raise EvidenceError("collection_deadline", "Collection elapsed-time bound reached; remaining reads are unavailable, not healthy.", "collection")
        if self.request_count >= MAX_HTTP_ATTEMPTS:
            raise EvidenceError("request_cap", "Collection HTTP-attempt bound reached; no narrower success is substituted.", "collection")
        self.request_count += 1
        try:
            token = self.credential.get_token(audience.rstrip("/") + "/.default").token
        except (ClientAuthenticationError, CredentialUnavailableError, ServiceRequestError) as exc:
            raise EvidenceError("authentication_failed", "The explicitly selected managed identity could not acquire a token.", "identity") from exc
        try:
            encoded = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        except (AttributeError, IndexError, ValueError, binascii.Error) as exc:
            raise EvidenceError("authentication_failed", "Managed identity returned an unreadable token; token contents are excluded.", "identity") from exc
        tenant, client, token_audience = pick(claims, "tid"), pick(claims, "appid") or pick(claims, "azp"), pick(claims, "aud")
        if not (
            isinstance(tenant, str) and tenant.lower() == TENANT_ID.lower()
            and isinstance(client, str) and client.lower() == MANAGED_IDENTITY_CLIENT_ID.lower()
            and isinstance(token_audience, str) and token_audience.rstrip("/") == audience.rstrip("/")
        ):
            raise EvidenceError("identity_mismatch", "Token tenant, client identity or audience differs from the fixed configuration.", "identity")
        return token

    def close(self):
        self.credential.close()


class HttpReader:
    def __init__(self, identity, origin=ARM_ORIGIN, audience=ARM_ORIGIN, context=None):
        self.identity, self.origin, self.audience = identity, origin, audience
        self.context = context or ssl.create_default_context()
        if self.context.verify_mode != ssl.CERT_REQUIRED or not self.context.check_hostname:
            raise EvidenceError("unsafe_tls", "TLS peer and hostname validation are mandatory.", "tls")
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=self.context))
        self.allowed = set()

    def allow(self, url, method="GET"):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or (parsed.scheme + "://" + parsed.netloc) != self.origin or parsed.fragment or parsed.username:
            raise EvidenceError("unsafe_endpoint", "Read URL changed the fixed HTTPS origin.", "scope")
        if method not in ("GET", "POST"):
            raise EvidenceError("unsafe_method", "Only enumerated reads and two fixed read-only POST operations are permitted.", "scope")
        self.allowed.add((method, parsed.path))
        return url

    def request(self, method, url, source, body=None, allow_empty=False):
        parsed = urlsplit(url)
        if (
            parsed.scheme + "://" + parsed.netloc != self.origin or parsed.fragment or parsed.username or parsed.password
            or len(url) > 16384 or (method, parsed.path) not in self.allowed
        ):
            raise EvidenceError("unsafe_endpoint", "Request was not one of the scoped allowlisted reads.", source)
        if method == "POST" and not (
            parsed.path.lower().endswith("/listclusterusercredential")
            or parsed.path.lower().endswith("/providers/microsoft.costmanagement/query")
        ):
            raise EvidenceError("unsafe_method", "This POST is not an enumerated read-only operation.", source)
        payload = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        for attempt in range(3):
            request = Request(url, data=payload, method=method, headers={
                "Authorization": "Bearer " + self.identity.token(self.audience),
                "Accept": "application/json", "Content-Type": "application/json",
            })
            try:
                with self.opener.open(request, timeout=30) as response:
                    if response.status == 204 and allow_empty:
                        return {}
                    if response.status != 200:
                        raise EvidenceError("http_error", "Read returned HTTP " + str(response.status) + ".", source, response.status)
                    raw = response.read(HTTP_BYTES + 1)
                    if len(raw) > HTTP_BYTES:
                        raise EvidenceError("response_cap", "Source response exceeded the disclosed byte safety bound.", source)
            except HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    retry = exc.headers.get("Retry-After", "") if exc.headers else ""
                    time.sleep(min(int(retry), 5) if re.fullmatch(r"[0-9]{1,4}", retry) else attempt + 1)
                    continue
                raise EvidenceError("access_denied" if exc.code in (401, 403) else "http_error",
                                    "Read returned HTTP " + str(exc.code) + "; response bodies are excluded.", source, exc.code) from exc
            except (URLError, TimeoutError, ssl.SSLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                code = "tls_failed" if isinstance(reason, ssl.SSLError) else "transport_failed"
                raise EvidenceError(code, "Read transport/TLS failed; no insecure retry or alternate identity.", source) from exc
            try:
                return obj(json.loads(raw), source)
            except (ValueError, UnicodeDecodeError) as exc:
                raise EvidenceError("invalid_json", "Source returned invalid JSON; no raw body is exported.", source) from exc
        raise EvidenceError("http_error", "Bounded retries exhausted.", source)


def next_arm_link(link, initial):
    if not isinstance(link, str) or not link or len(link) > 16384:
        raise EvidenceError("invalid_pagination", "Invalid ARM continuation.", "pagination")
    current, first = urlsplit(link), urlsplit(initial)
    if (
        current.scheme != "https" or current.netloc != first.netloc or current.path != first.path
        or current.fragment or current.username or current.password
        or unquote(current.path) != current.path or "\\" in current.path
    ):
        raise EvidenceError("invalid_pagination", "ARM continuation changed origin or exact resource scope.", "pagination")
    try:
        pairs = parse_qsl(current.query, keep_blank_values=True, strict_parsing=True)
        original = dict(parse_qsl(first.query, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise EvidenceError("invalid_pagination", "Malformed continuation query.", "pagination") from exc
    values = dict(pairs)
    paging = {"$skiptoken", "skiptoken", "$skipToken", "skipToken", "$skip", "$top"}
    if len(values) != len(pairs) or any(key not in original and key not in paging for key in values):
        raise EvidenceError("invalid_pagination", "ARM continuation added an unapproved query.", "pagination")
    if any(values.get(key) != value for key, value in original.items() if key not in paging):
        raise EvidenceError("invalid_pagination", "ARM continuation changed version or filter.", "pagination")
    aliases = [value for key, value in pairs if key.lower() in ("$skiptoken", "skiptoken")]
    if aliases and (not all(aliases) or len(set(aliases)) != 1):
        raise EvidenceError("invalid_pagination", "ARM continuation token aliases are empty or conflicting.", "pagination")
    return link


def read_pages(reader, url, source_id, reference, normalize, kubernetes=False, value_key="value", body=None,
               metrics_snapshot=False):
    receipt = source_receipt(source_id, reference, requested_page_size=KUBE_PAGE_SIZE if kubernetes else None,
                             page_cap=MAX_PAGES, row_cap=MAX_ROWS, pagination_complete=False,
                             read_signature=("POST" if body is not None else "GET") + " " + url)
    rows, seen_ids, seen_pages = [], set(), set()
    current, resource_version = url, None
    method = "POST" if body is not None else "GET"
    reader.allow(url, method)
    try:
        for page_number in range(MAX_PAGES):
            if current in seen_pages:
                raise EvidenceError("pagination_loop", "Source repeated a continuation.", source_id)
            seen_pages.add(current)
            payload = reader.request(method, current, source_id, body=body)
            receipt["pages"] = page_number + 1
            if body is not None:
                payload = obj(payload.get("properties"), source_id)
            page = array(payload.get("items" if kubernetes else value_key), source_id)
            if len(rows) + len(page) > MAX_ROWS:
                raise EvidenceError("row_cap", "Source row safety cap reached; scope was not silently narrowed.", source_id)
            for raw in page:
                row = normalize(raw)
                identity = row.get("uid") or row.get("id") or row.get("resource_id")
                if not kubernetes and isinstance(identity, str):
                    identity = identity.lower()
                if not identity or identity in seen_ids:
                    raise EvidenceError("invalid_pagination", "Source row identity is absent or repeated.", source_id)
                seen_ids.add(identity)
                rows.append(row)
            receipt["records"] = len(rows)
            if kubernetes:
                metadata = obj(payload.get("metadata"), source_id)
                version = text(metadata.get("resourceVersion"), source_id, 256)
                if (not version and not metrics_snapshot) or (resource_version is not None and version != resource_version):
                    raise EvidenceError("inconsistent_resource_version", "Kubernetes list resourceVersion changed or is absent.", source_id)
                resource_version = version
                receipt["resource_version"] = version
                if metrics_snapshot:
                    receipt["note"] = "Aggregated metrics API snapshot; resourceVersion may be unavailable, not a transactional inventory cursor."
                continuation = metadata.get("continue")
                remaining = metadata.get("remainingItemCount")
                if remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0):
                    raise EvidenceError("invalid_pagination", "Invalid remainingItemCount.", source_id)
                receipt["remaining_item_count"] = remaining
                if not continuation:
                    if remaining not in (None, 0):
                        raise EvidenceError("missing_continuation", "Remaining Kubernetes records have no continuation.", source_id)
                    receipt["pagination_complete"] = True
                    return rows, receipt
                text(continuation, source_id, 8192)
                current = url.split("?")[0] + "?" + urlencode({"limit": KUBE_PAGE_SIZE, "continue": continuation})
            else:
                continuation = payload.get("nextLink")
                if not continuation:
                    receipt["pagination_complete"] = True
                    return rows, receipt
                current = next_arm_link(continuation, url)
        raise EvidenceError("page_cap", "Source page safety cap reached; collection remains explicitly partial.", source_id)
    except EvidenceError as exc:
        receipt["status"] = "partial" if rows else "denied" if exc.http_status in (401, 403) else "error"
        receipt["records"] = len(rows)
        receipt["errors"].append(exc.public())
        return rows, receipt


def read_one(reader, url, source_id, reference, normalize):
    receipt = source_receipt(source_id, reference, pages=1, read_signature="GET " + url)
    try:
        reader.allow(url)
        result = normalize(reader.request("GET", url, source_id))
        receipt["records"] = 1
        return result, receipt
    except EvidenceError as exc:
        receipt.update(status="denied" if exc.http_status in (401, 403) else "error", records=0, errors=[exc.public()])
        return None, receipt


def common_row(raw, source_id, reference, config, kubernetes=False, metrics_snapshot=False):
    raw = obj(raw, source_id)
    if kubernetes:
        metadata = obj(raw.get("metadata"), source_id)
        uid = text(metadata.get("uid"), source_id, 256)
        name = text(metadata.get("name"), source_id, 253)
        if not name or (not uid and not metrics_snapshot):
            raise EvidenceError("invalid_record", "Kubernetes object requires name and UID.", source_id)
        owners = metadata.get("ownerReferences", [])
        owners = [{"kind": text(r.get("kind"), source_id), "name": text(r.get("name"), source_id),
                   "uid": text(r.get("uid"), source_id), "controller": boolean(r.get("controller"), source_id)}
                  for r in array(owners, source_id)]
        return {"name": name, "uid": uid, "id": ("metrics:" + str(metadata.get("namespace") or "") + "/" + name) if metrics_snapshot else None,
                "kind": text(raw.get("kind"), source_id),
                "namespace": text(metadata.get("namespace"), source_id, 63), "resource_id": config["id"],
                "labels": labels(metadata.get("labels"), source_id), "owner_references": owners,
                "resource_version": text(metadata.get("resourceVersion"), source_id),
                "source_id": source_id, "observed_at": reference, "cluster_name": config["name"], "cluster_id": config["id"]}
    resource_id = arm_id(raw.get("id"), scope_subscription(config))
    return {"id": resource_id, "resource_id": resource_id, "name": text(raw.get("name"), source_id),
            "resource_type": text(raw.get("type"), source_id), "region": text(raw.get("location"), source_id),
            "source_id": source_id, "observed_at": reference, "cluster_name": config["name"], "cluster_id": config["id"]}


def normalize_pool(raw, source_id, reference, config):
    row = common_row(raw, source_id, reference, config)
    properties = obj(raw.get("properties"), source_id)
    for output, field in {
        "mode": "mode", "vm_size": "vmSize", "version": "currentOrchestratorVersion",
        "os_type": "osType", "os_sku": "osSKU", "os_disk_type": "osDiskType", "provisioning_state": "provisioningState",
        "vnet_subnet_id": "vnetSubnetID", "pod_subnet_id": "podSubnetID", "scale_down_mode": "scaleDownMode",
        "priority": "scaleSetPriority",
    }.items():
        row[output] = text(properties.get(field), source_id)
    if row["version"] is None:
        row["version"] = text(properties.get("orchestratorVersion"), source_id)
    for output, field in {"count": "count", "min_count": "minCount", "max_count": "maxCount", "max_pods": "maxPods"}.items():
        row[output] = number(properties.get(field), source_id)
    row["enable_auto_scaling"] = boolean(properties.get("enableAutoScaling"), source_id)
    row["power_state"] = text(pick(properties, "powerState", "code"), source_id)
    row["os_disk_size_gb"] = number(properties.get("osDiskSizeGB"), source_id)
    row["vm_set_type"] = text(properties.get("type"), source_id)
    row["availability_zones"] = strings(properties.get("availabilityZones"), source_id)
    row["node_taints"] = strings(properties.get("nodeTaints"), source_id)
    row["upgrade_settings"] = {
        "max_surge": text(pick(properties, "upgradeSettings", "maxSurge"), source_id),
        "drain_timeout_minutes": number(pick(properties, "upgradeSettings", "drainTimeoutInMinutes"), source_id),
    }
    return row


def normalize_cluster(raw, source_id, reference, config):
    row = common_row(raw, source_id, reference, config)
    if row["id"].lower() != config["id"].lower():
        raise EvidenceError("scope_mismatch", "ARM returned a different cluster ID.", source_id)
    properties = obj(raw.get("properties"), source_id)
    row.update(resource_group=config["resource_group"], subscription_id=scope_subscription(config),
               version=text(properties.get("currentKubernetesVersion") or properties.get("kubernetesVersion"), source_id),
               power_state=text(pick(properties, "powerState", "code"), source_id),
               provisioning_state=text(properties.get("provisioningState"), source_id),
               sku_tier=text(pick(raw, "sku", "tier"), source_id),
               mode=text(pick(raw, "sku", "name"), source_id),
               node_resource_group=text(properties.get("nodeResourceGroup"), source_id, 90),
               identity_type=text(pick(raw, "identity", "type"), source_id),
               enable_rbac=boolean(properties.get("enableRBAC"), source_id),
               managed_entra=boolean(pick(properties, "aadProfile", "managed"), source_id),
               azure_rbac=boolean(pick(properties, "aadProfile", "enableAzureRBAC"), source_id),
               disable_local_accounts=boolean(properties.get("disableLocalAccounts"), source_id),
               workload_identity_enabled=boolean(pick(properties, "securityProfile", "workloadIdentity", "enabled"), source_id),
               oidc_enabled=boolean(pick(properties, "oidcIssuerProfile", "enabled"), source_id),
               defender_enabled=boolean(pick(properties, "securityProfile", "defender", "securityMonitoring", "enabled"), source_id),
               private_cluster=boolean(pick(properties, "apiServerAccessProfile", "enablePrivateCluster"), source_id),
               authorized_ip_ranges=strings(pick(properties, "apiServerAccessProfile", "authorizedIPRanges"), source_id),
               private_dns_zone=text(pick(properties, "apiServerAccessProfile", "privateDNSZone"), source_id),
               fqdn=text(properties.get("fqdn"), source_id), private_fqdn=text(properties.get("privateFQDN"), source_id),
               support_plan=text(properties.get("supportPlan"), source_id),
               node_provisioning_mode=text(pick(properties, "nodeProvisioningProfile", "mode"), source_id))
    row["network"] = {key: text(pick(properties, "networkProfile", native), source_id) for key, native in {
        "plugin": "networkPlugin", "plugin_mode": "networkPluginMode", "dataplane": "networkDataplane",
        "policy": "networkPolicy", "outbound_type": "outboundType", "load_balancer_sku": "loadBalancerSku",
        "service_cidr": "serviceCidr", "pod_cidr": "podCidr", "dns_service_ip": "dnsServiceIP",
    }.items()}
    row["auto_upgrade"] = {key: text(pick(properties, "autoUpgradeProfile", native), source_id)
                           for key, native in {"channel": "upgradeChannel", "node_os_channel": "nodeOSUpgradeChannel"}.items()}
    addons = properties.get("addonProfiles")
    row["addons"] = None if addons is None else {
        key: {"enabled": boolean(obj(value, source_id).get("enabled"), source_id)}
        for key, value in obj(addons, source_id).items() if key in (
            "azurepolicy", "omsagent", "azureKeyvaultSecretsProvider", "httpApplicationRouting", "ingressApplicationGateway")
    }
    kv_config = pick(properties, "addonProfiles", "azureKeyvaultSecretsProvider", "config")
    if kv_config is not None:
        value = obj(kv_config, source_id).get("enableSecretRotation")
        row["keyvault_secret_rotation"] = value if isinstance(value, bool) else (
            value.lower() == "true" if value in ("true", "false", "True", "False") else None)
    else:
        row["keyvault_secret_rotation"] = None
    row["storage_profile"] = {key: boolean(pick(properties, "storageProfile", native, "enabled"), source_id)
                              for key, native in {"disk_csi": "diskCSIDriver", "file_csi": "fileCSIDriver",
                                                  "snapshot_controller": "snapshotController"}.items()}
    row["effective_outbound_ip_ids"] = [value for value in (
        same_subscription_id(item.get("id"))
        for item in array(pick(properties, "networkProfile", "loadBalancerProfile", "effectiveOutboundIPs") or [], source_id)
    ) if value]
    row["kubelet_identity_resource_id"] = text(pick(properties, "identityProfile", "kubeletidentity", "resourceId"), source_id)
    row["identity"] = {
        "type": row["identity_type"], "principal_id": text(pick(raw, "identity", "principalId"), source_id),
        "user_assigned_resource_ids": [value for value in (
            same_subscription_id(item) for item in obj(pick(raw, "identity", "userAssignedIdentities") or {}, source_id)
        ) if value],
        "kubelet_principal_id": text(pick(properties, "identityProfile", "kubeletidentity", "objectId"), source_id),
    }
    embedded = properties.get("agentPoolProfiles")
    row["embedded_pool_profiles"] = None if embedded is None else [
        normalize_pool({"id": config["id"] + "/agentPools/" + text(item.get("name"), source_id),
                        "name": item.get("name"), "type": "Microsoft.ContainerService/managedClusters/agentPools", "properties": item},
                       source_id, reference, config) for item in array(embedded, source_id)
    ]
    return row


def normalize_containers(containers, source_id):
    result = []
    for container in array(containers, source_id):
        resources = obj(container.get("resources", {}), source_id)
        requests, limits = obj(resources.get("requests", {}), source_id), obj(resources.get("limits", {}), source_id)
        context = obj(container.get("securityContext", {}), source_id)
        image = text(container.get("image"), source_id, 2048)
        if image is not None and (
            not re.fullmatch(r"[A-Za-z0-9._:/@-]+", image) or "://" in image
            or ("@" in image and not re.fullmatch(r"[^@]+@(?:sha256:[0-9a-fA-F]{64}|sha512:[0-9a-fA-F]{128})", image))
        ):
            raise EvidenceError("invalid_image", "Image reference contains unsupported or credential-like syntax.", source_id)
        def probe_configured(key):
            value = container.get(key)
            return False if value is None else bool(obj(value, source_id))
        result.append({
            "name": text(container.get("name"), source_id, 253), "image": image,
            "requests": {"cpu": text(requests.get("cpu"), source_id), "memory": text(requests.get("memory"), source_id)},
            "limits": {"cpu": text(limits.get("cpu"), source_id), "memory": text(limits.get("memory"), source_id)},
            "request_cpu_cores": quantity(requests.get("cpu"), "cores"),
            "request_memory_bytes": quantity(requests.get("memory"), "bytes"),
            "limit_cpu_cores": quantity(limits.get("cpu"), "cores"),
            "limit_memory_bytes": quantity(limits.get("memory"), "bytes"),
            "readiness_probe": probe_configured("readinessProbe"), "liveness_probe": probe_configured("livenessProbe"),
            "startup_probe": probe_configured("startupProbe"),
            "ports": [number(port.get("containerPort"), source_id) for port in array(container.get("ports", []), source_id)],
            "security": {key: boolean(context.get(key), source_id)
                         for key in ("privileged", "allowPrivilegeEscalation", "runAsNonRoot", "readOnlyRootFilesystem")},
            "run_as_user": number(context.get("runAsUser"), source_id),
        })
    return result


def normalize_selector(value, source_id, map_selector=False):
    if value is None:
        return None
    selector = obj(value, source_id)
    if map_selector:
        return {"match_labels": labels(selector, source_id), "match_expressions": []}
    expressions = [
        {"key": text(item.get("key"), source_id), "operator": text(item.get("operator"), source_id),
         "values": strings(item.get("values", []), source_id)}
        for item in array(selector.get("matchExpressions", []), source_id)
    ]
    if any(item["operator"] not in ("In", "NotIn", "Exists", "DoesNotExist") or not item["key"] for item in expressions):
        raise EvidenceError("invalid_selector", "Unsupported selector expression; no guessed join.", source_id)
    if any((item["operator"] in ("In", "NotIn") and not item["values"])
           or (item["operator"] in ("Exists", "DoesNotExist") and item["values"]) for item in expressions):
        raise EvidenceError("invalid_selector", "Selector operator/value cardinality is invalid.", source_id)
    return {"match_labels": labels(selector.get("matchLabels", {}), source_id), "match_expressions": expressions}


def int_or_percent(value, source_id):
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}%", value):
        return value
    raise EvidenceError("invalid_field", "PDB value must be a bounded nonnegative integer or percentage, not arbitrary data.", source_id)


def metric_window_seconds(value, source_id):
    if value is None:
        return None
    value = text(value, source_id, 128)
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s|m|h)", value)
    if not parts or "".join(amount + unit for amount, unit in parts) != value:
        raise EvidenceError("invalid_metric_window", "Metrics duration must be a bounded source duration, not an assumed month.", source_id)
    units = {"ns": 1e-9, "us": 1e-6, "ms": .001, "s": 1, "m": 60, "h": 3600}
    seconds = sum(float(amount) * units[unit] for amount, unit in parts)
    if not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise EvidenceError("invalid_metric_window", "Metrics window must be positive and at most one day.", source_id)
    return seconds


def metric_freshness(timestamp, window_seconds, reference, source_id):
    if timestamp is None or window_seconds is None:
        return None
    try:
        measured = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        declared = datetime.fromisoformat(reference.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("invalid_metric_time", "Metric timestamp is not a valid source time.", source_id) from exc
    if measured.tzinfo is None or declared.tzinfo is None:
        raise EvidenceError("invalid_metric_time", "Metric/source reference requires an explicit timezone.", source_id)
    return abs((declared - measured).total_seconds()) <= 300


def normalize_kube(raw, collection, source_id, reference, config):
    row = common_row(raw, source_id, reference, config, kubernetes=True, metrics_snapshot=collection in ("node_metrics", "pod_metrics"))
    expected_kind = {
        "namespaces": "Namespace", "nodes": "Node", "pods": "Pod", "services": "Service",
        "pvcs": "PersistentVolumeClaim", "pvs": "PersistentVolume", "resource_quotas": "ResourceQuota",
        "deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet", "replicasets": "ReplicaSet",
        "jobs": "Job", "cronjobs": "CronJob", "pdbs": "PodDisruptionBudget", "hpas": "HorizontalPodAutoscaler",
        "network_policies": "NetworkPolicy", "storage_classes": "StorageClass", "node_metrics": "NodeMetrics", "pod_metrics": "PodMetrics",
    }[collection]
    if row["kind"] not in (None, expected_kind):
        raise EvidenceError("unexpected_kubernetes_kind", "Object kind differs from the fixed collection endpoint.", source_id)
    row["kind"] = expected_kind
    spec, status = obj(raw.get("spec", {}), source_id), obj(raw.get("status", {}), source_id)
    row["resource_kind"] = collection
    if collection in ("deployments", "statefulsets", "daemonsets", "jobs", "cronjobs"):
        template = pick(spec, "jobTemplate", "spec", "template") if collection == "cronjobs" else spec.get("template")
        template = obj(template, source_id)
        pod_spec = obj(template.get("spec"), source_id)
        row.update(
            kind={"deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet",
                  "jobs": "Job", "cronjobs": "CronJob"}[collection],
            workload_name=row["name"], template_labels=labels(pick(template, "metadata", "labels"), source_id),
            containers=normalize_containers(pod_spec.get("containers"), source_id),
            init_containers=normalize_containers(pod_spec.get("initContainers", []), source_id),
            desired_replicas=number(spec.get("replicas", 1), source_id) if collection in ("deployments", "statefulsets") else (
                number(status.get("desiredNumberScheduled"), source_id) if collection == "daemonsets" else None),
            ready_replicas=number(status.get("readyReplicas"), source_id) if collection != "daemonsets"
                           else number(status.get("numberReady"), source_id),
            suspended=boolean(spec.get("suspend"), source_id),
            os_name=text(pick(pod_spec, "os", "name"), source_id),
            node_selector=labels(pod_spec.get("nodeSelector"), source_id),
            pod_security={key: boolean(pick(pod_spec, "securityContext", key), source_id) for key in ("runAsNonRoot",)},
            run_as_user=number(pick(pod_spec, "securityContext", "runAsUser"), source_id),
            pvc_names=[text(pick(volume, "persistentVolumeClaim", "claimName"), source_id)
                       for volume in array(pod_spec.get("volumes", []), source_id) if "persistentVolumeClaim" in volume],
        )
    elif collection == "pods":
        row.update(node_name=text(spec.get("nodeName"), source_id), phase=text(status.get("phase"), source_id),
                   containers=normalize_containers(spec.get("containers"), source_id),
                   init_containers=normalize_containers(spec.get("initContainers", []), source_id),
                   pvc_names=[text(pick(v, "persistentVolumeClaim", "claimName"), source_id)
                              for v in array(spec.get("volumes", []), source_id) if "persistentVolumeClaim" in v])
    elif collection == "nodes":
        row["pool_name"] = pick(row, "labels", "kubernetes.azure.com/agentpool") or pick(row, "labels", "agentpool")
        row["provider_id"] = text(spec.get("providerID"), source_id, 2048)
        row["capacity"] = {key: quantity(pick(status, "capacity", key), unit)
                           for key, unit in (("cpu", "cores"), ("memory", "bytes"))}
        row["allocatable"] = {key: quantity(pick(status, "allocatable", key), unit)
                              for key, unit in (("cpu", "cores"), ("memory", "bytes"))}
        row["conditions"] = [{key: text(c.get(key), source_id) for key in ("type", "status", "lastTransitionTime", "reason")}
                             for c in array(status.get("conditions", []), source_id)]
    elif collection == "services":
        row.update(service_type=text(spec.get("type"), source_id), selector=normalize_selector(spec.get("selector"), source_id, True),
                   cluster_ip=text(spec.get("clusterIP"), source_id),
                   load_balancer_ingress=[{key: text(v.get(key), source_id) for key in ("ip", "hostname")}
                                          for v in array(pick(status, "loadBalancer", "ingress") or [], source_id)],
                   internal_load_balancer=text(pick(raw, "metadata", "annotations", "service.beta.kubernetes.io/azure-load-balancer-internal"), source_id, 5),
                   ports=[{"name": text(p.get("name"), source_id), "port": number(p.get("port"), source_id),
                           "protocol": text(p.get("protocol"), source_id), "node_port": number(p.get("nodePort"), source_id),
                           "target_port": text(p.get("targetPort"), source_id) if isinstance(p.get("targetPort"), str)
                           else number(p.get("targetPort"), source_id)} for p in array(spec.get("ports", []), source_id)])
        annotation_keys = (
            "service.beta.kubernetes.io/azure-load-balancer-internal",
            "service.beta.kubernetes.io/azure-load-balancer-internal-subnet",
            "service.beta.kubernetes.io/azure-load-balancer-resource-group",
            "service.beta.kubernetes.io/azure-pip-name",
            "service.beta.kubernetes.io/azure-load-balancer-health-probe-request-path",
        )
        row["annotations"] = {key: text(value, source_id) for key, value in obj(pick(raw, "metadata", "annotations") or {}, source_id).items()
                              if key in annotation_keys}
    elif collection == "pdbs":
        row.update(selector=normalize_selector(spec.get("selector"), source_id),
                   min_available=int_or_percent(spec.get("minAvailable"), source_id),
                   max_unavailable=int_or_percent(spec.get("maxUnavailable"), source_id),
                   disruptions_allowed=number(status.get("disruptionsAllowed"), source_id),
                   current_healthy=number(status.get("currentHealthy"), source_id),
                   desired_healthy=number(status.get("desiredHealthy"), source_id))
    elif collection == "hpas":
        row.update(target={key: text(pick(spec, "scaleTargetRef", key), source_id) for key in ("apiVersion", "kind", "name")},
                   min_replicas=number(spec.get("minReplicas", 1), source_id), max_replicas=number(spec.get("maxReplicas"), source_id),
                   current_replicas=number(status.get("currentReplicas"), source_id), desired_replicas=number(status.get("desiredReplicas"), source_id),
                   metric_types=[text(item.get("type"), source_id) for item in array(spec.get("metrics", []), source_id)])
    elif collection == "network_policies":
        row.update(selector=normalize_selector(spec.get("podSelector"), source_id), policy_types=strings(spec.get("policyTypes"), source_id),
                   ingress_rule_count=len(array(spec.get("ingress", []), source_id)), egress_rule_count=len(array(spec.get("egress", []), source_id)))
    elif collection == "pvcs":
        row.update(storage_class=text(spec.get("storageClassName"), source_id), volume_name=text(spec.get("volumeName"), source_id),
                   access_modes=strings(spec.get("accessModes"), source_id), phase=text(status.get("phase"), source_id),
                   requested_storage=text(pick(spec, "resources", "requests", "storage"), source_id),
                   requested_bytes=quantity(pick(spec, "resources", "requests", "storage"), "bytes"))
    elif collection == "pvs":
        row.update(storage_class=text(spec.get("storageClassName"), source_id), phase=text(status.get("phase"), source_id),
                   capacity_bytes=quantity(pick(spec, "capacity", "storage"), "bytes"), reclaim_policy=text(spec.get("persistentVolumeReclaimPolicy"), source_id),
                   csi_driver=text(pick(spec, "csi", "driver"), source_id), csi_volume_handle=text(pick(spec, "csi", "volumeHandle"), source_id),
                   claim_ref={key: text(pick(spec, "claimRef", key), source_id) for key in ("namespace", "name", "uid")})
        row["csi_volume_attributes"] = {
            key: text(value, source_id) for key, value in obj(pick(spec, "csi", "volumeAttributes") or {}, source_id).items()
            if key in ("resourceGroup", "storageAccount", "shareName", "protocol")
        }
    elif collection == "storage_classes":
        row.update(provisioner=text(raw.get("provisioner"), source_id), volume_binding_mode=text(raw.get("volumeBindingMode"), source_id),
                   reclaim_policy=text(raw.get("reclaimPolicy"), source_id),
                   parameters={key: text(value, source_id) for key, value in obj(raw.get("parameters", {}), source_id).items()
                               if key.lower() in ("skuname", "kind", "cachingmode", "fstype", "location", "resourcegroup")})
    elif collection == "resource_quotas":
        row.update(hard={text(key, source_id): text(value, source_id) for key, value in obj(spec.get("hard", {}), source_id).items()},
                   used={text(key, source_id): text(value, source_id) for key, value in obj(status.get("used", {}), source_id).items()})
    elif collection in ("node_metrics", "pod_metrics"):
        timestamp, window = text(raw.get("timestamp"), source_id), text(raw.get("window"), source_id)
        seconds = metric_window_seconds(window, source_id)
        row.update(timestamp=timestamp, window=window, window_seconds=seconds, sample_count=1,
                   fresh_for_reference=metric_freshness(timestamp, seconds, reference, source_id),
                   units={"cpu": "cores", "memory": "bytes"})
        if collection == "node_metrics":
            row.update(cpu_cores=quantity(pick(raw, "usage", "cpu"), "cores"),
                       memory_bytes=quantity(pick(raw, "usage", "memory"), "bytes"),
                       cpu_quantity=text(pick(raw, "usage", "cpu"), source_id),
                       memory_quantity=text(pick(raw, "usage", "memory"), source_id))
        else:
            row["containers"] = [{"name": text(c.get("name"), source_id), "cpu_cores": quantity(pick(c, "usage", "cpu"), "cores"),
                                  "memory_bytes": quantity(pick(c, "usage", "memory"), "bytes"),
                                  "cpu_quantity": text(pick(c, "usage", "cpu"), source_id),
                                  "memory_quantity": text(pick(c, "usage", "memory"), source_id)}
                                 for c in array(raw.get("containers"), source_id)]
    return row


def normalize_resource(raw, source_id, reference, config, association):
    row = common_row(raw, source_id, reference, config)
    kind = resource_type(row["id"])
    if kind not in RESOURCE_APIS:
        raise EvidenceError("unsupported_resource_type", "Resource detail type is not in the fixed whitelist.", source_id)
    props = obj(raw.get("properties", {}), source_id)
    row.update(resource_kind=kind, association=association, sku_name=text(pick(raw, "sku", "name"), source_id),
               sku_tier=text(pick(raw, "sku", "tier"), source_id), provisioning_state=text(props.get("provisioningState"), source_id))
    fields = {
        "microsoft.compute/disks": ("diskState", "diskSizeGB", "encryption", "zones"),
        "microsoft.storage/storageaccounts": ("supportsHttpsTrafficOnly", "minimumTlsVersion", "allowBlobPublicAccess",
                                              "allowSharedKeyAccess", "publicNetworkAccess"),
        "microsoft.containerregistry/registries": ("publicNetworkAccess", "zoneRedundancy"),
        "microsoft.keyvault/vaults": ("enableRbacAuthorization", "enablePurgeProtection", "softDeleteRetentionInDays", "publicNetworkAccess"),
        "microsoft.network/publicipaddresses": ("ipAddress", "publicIPAllocationMethod"),
        "microsoft.network/privatednszones": ("numberOfRecordSets", "maxNumberOfRecordSets"),
        "microsoft.network/dnszones": ("numberOfRecordSets", "zoneType"),
    }
    row["configuration"] = {}
    for field in fields.get(kind, ()):
        value = props.get(field) if field != "zones" else raw.get("zones")
        if field == "encryption":
            row["configuration"][field] = {"type": text(pick(props, "encryption", "type"), source_id)}
        elif field == "zones":
            row["configuration"][field] = strings(value, source_id)
        elif isinstance(value, bool) or value is None:
            row["configuration"][field] = value
        elif isinstance(value, (int, float)):
            row["configuration"][field] = number(value, source_id)
        else:
            row["configuration"][field] = text(value, source_id)
    if kind == "microsoft.compute/disks":
        row.update(managed_by=text(raw.get("managedBy"), source_id), managed_by_present="managedBy" in raw,
                   managed_by_extended=strings(raw.get("managedByExtended"), source_id))
    if kind == "microsoft.compute/virtualmachinescalesets":
        row.update(capacity=number(pick(raw, "sku", "capacity"), source_id),
                   pool_tag=text(pick(raw, "tags", "aks-managed-poolName"), source_id),
                   cluster_tag=text(pick(raw, "tags", "aks-managed-cluster-name"), source_id))
        interfaces = array(pick(props, "virtualMachineProfile", "networkProfile", "networkInterfaceConfigurations") or [], source_id)
        row["subnet_ids"] = [text(pick(ip, "properties", "subnet", "id"), source_id)
                             for interface in interfaces for ip in array(pick(interface, "properties", "ipConfigurations") or [], source_id)
                             if pick(ip, "properties", "subnet", "id")]
        row["nsg_ids"] = [text(pick(interface, "properties", "networkSecurityGroup", "id"), source_id) for interface in interfaces
                          if pick(interface, "properties", "networkSecurityGroup", "id")]
        row["load_balancer_backend_pool_ids"] = [
            text(item.get("id"), source_id) for interface in interfaces
            for ip in array(pick(interface, "properties", "ipConfigurations") or [], source_id)
            for item in array(pick(ip, "properties", "loadBalancerBackendAddressPools") or [], source_id)
        ]
    if kind == "microsoft.containerregistry/registries":
        row["login_server"] = text(props.get("loginServer"), source_id, 512)
    if kind == "microsoft.managedidentity/userassignedidentities":
        row["principal_id"] = text(props.get("principalId"), source_id, 36)
    if kind == "microsoft.network/publicipaddresses":
        row.update(ip_configuration_id=text(pick(props, "ipConfiguration", "id"), source_id),
                   ip_configuration_present="ipConfiguration" in props,
                   nat_gateway_id=text(pick(props, "natGateway", "id"), source_id), nat_gateway_present="natGateway" in props)
    if kind == "microsoft.network/loadbalancers":
        row["frontends"] = [
            {"id": text(item.get("id"), source_id), "name": text(item.get("name"), source_id),
             "public_ip_id": text(pick(item, "properties", "publicIPAddress", "id"), source_id),
             "private_ip": text(pick(item, "properties", "privateIPAddress"), source_id),
             "subnet_id": text(pick(item, "properties", "subnet", "id"), source_id)}
            for item in array(props.get("frontendIPConfigurations", []), source_id)
        ]
        row["probes"] = [{"id": text(item.get("id"), source_id), "name": text(item.get("name"), source_id),
                          "request_path": text(pick(item, "properties", "requestPath"), source_id),
                          **{key: text(pick(item, "properties", key), source_id) if key == "protocol"
                             else number(pick(item, "properties", key), source_id) for key in ("protocol", "port", "intervalInSeconds")}}
                         for item in array(props.get("probes", []), source_id)]
        row["backend_pools"] = [
            {"id": text(item.get("id"), source_id), "name": text(item.get("name"), source_id),
             "ip_configuration_ids": [text(value.get("id"), source_id)
                                      for value in array(pick(item, "properties", "backendIPConfigurations") or [], source_id)],
             "addresses": [{"name": text(value.get("name"), source_id),
                            "ip_address": text(pick(value, "properties", "ipAddress"), source_id),
                            "virtual_network_id": text(pick(value, "properties", "virtualNetwork", "id"), source_id)}
                           for value in array(pick(item, "properties", "loadBalancerBackendAddresses") or [], source_id)]}
            for item in array(props.get("backendAddressPools", []), source_id)
        ]
        row["load_balancing_rules"] = [
            {"id": text(item.get("id"), source_id), "name": text(item.get("name"), source_id),
             "frontend_id": text(pick(item, "properties", "frontendIPConfiguration", "id"), source_id),
             "backend_pool_id": text(pick(item, "properties", "backendAddressPool", "id"), source_id),
             "backend_pool_ids": [text(value.get("id"), source_id)
                                  for value in array(pick(item, "properties", "backendAddressPools") or [], source_id)],
             "probe_id": text(pick(item, "properties", "probe", "id"), source_id),
             "protocol": text(pick(item, "properties", "protocol"), source_id),
             "frontend_port": number(pick(item, "properties", "frontendPort"), source_id),
             "backend_port": number(pick(item, "properties", "backendPort"), source_id)}
            for item in array(props.get("loadBalancingRules", []), source_id)
        ]
    if kind in ("microsoft.network/networkinterfaces", "microsoft.network/virtualnetworks/subnets"):
        row["public_ip_ids"] = [
            text(pick(item, "properties", "publicIPAddress", "id"), source_id)
            for item in array(props.get("ipConfigurations", []), source_id) if pick(item, "properties", "publicIPAddress", "id")
        ]
        row["nsg_id"] = text(pick(props, "networkSecurityGroup", "id"), source_id)
        row["address_prefixes"] = strings(props.get("addressPrefixes"), source_id)
        row["address_prefix"] = text(props.get("addressPrefix"), source_id)
        row["subnet_ids"] = [text(pick(item, "properties", "subnet", "id"), source_id)
                             for item in array(props.get("ipConfigurations", []), source_id) if pick(item, "properties", "subnet", "id")]
    if kind == "microsoft.network/virtualnetworks":
        row["subnet_ids"] = [text(item.get("id"), source_id) for item in array(props.get("subnets", []), source_id)]
        row["address_space"] = strings(pick(props, "addressSpace", "addressPrefixes"), source_id)
        row["dns_servers"] = strings(pick(props, "dhcpOptions", "dnsServers"), source_id)
    if kind == "microsoft.network/networksecuritygroups":
        row["security_rules"] = [
            {key: number(pick(item, "properties", key), source_id) if key == "priority"
             else strings(pick(item, "properties", key), source_id) if key.endswith("Prefixes") or key.endswith("Ranges")
             else text(pick(item, "properties", key), source_id)
             for key in ("priority", "direction", "access", "protocol", "sourceAddressPrefix", "sourceAddressPrefixes",
                         "destinationPortRange", "destinationPortRanges", "destinationAddressPrefix")}
            for item in array(props.get("securityRules", []), source_id)
        ]
    return row


def referenced_resource_ids(row):
    references = []
    for field in ("subnet_ids", "nsg_ids", "public_ip_ids", "load_balancer_backend_pool_ids"):
        references.extend((value, field) for value in row.get(field, []) if value)
    if row.get("nsg_id"):
        references.append((row["nsg_id"], "nsg_id"))
    for frontend in row.get("frontends", []):
        references.extend((frontend[field], "frontends." + field) for field in ("public_ip_id", "subnet_id") if frontend.get(field))
    if row.get("resource_kind") == "microsoft.network/virtualnetworks":
        references = [(value, field) for value, field in references if field != "subnet_ids"]
    if row.get("resource_kind") == "microsoft.network/virtualnetworks/subnets":
        references.append((re.split(r"/subnets/", row["id"], flags=re.IGNORECASE)[0], "parent_virtual_network_id"))
    for value, field in references:
        value = same_subscription_id(value)
        if not value:
            continue
        if resource_type(value) == "microsoft.network/loadbalancers/backendaddresspools":
            value = re.split(r"/backendaddresspools/", value, flags=re.IGNORECASE)[0]
        if resource_type(value) in RESOURCE_APIS:
            yield value, field


def normalize_registry_role(raw, source_id, reference, config, registry_id, principal_id):
    properties = obj(raw.get("properties"), source_id)
    scope = text(properties.get("scope"), source_id, 2048)
    assignment_id = text(raw.get("id"), source_id, 2048)
    principal = text(properties.get("principalId"), source_id, 36)
    role_id = text(properties.get("roleDefinitionId"), source_id, 2048)
    if not scope or not assignment_id or not principal or principal.lower() != principal_id.lower() or not (
        registry_id.lower() == scope.lower() or registry_id.lower().startswith(scope.lower().rstrip("/") + "/")
    ) or not re.fullmatch(re.escape(scope) + r"/providers/Microsoft\.Authorization/roleAssignments/[0-9a-f-]{36}",
                          assignment_id, re.IGNORECASE):
        raise EvidenceError("scope_mismatch", "Registry role metadata must match the fixed kubelet principal and registry/ancestor scope.", source_id)
    if not role_id or not re.fullmatch(
        r"/(?:subscriptions/" + re.escape(scope_subscription(config)) + r"/)?providers/Microsoft\.Authorization/roleDefinitions/[0-9a-f-]{36}",
        role_id, re.IGNORECASE,
    ):
        raise EvidenceError("invalid_role_definition", "Role metadata has an unexpected definition ID; no alternate scope is read.", source_id)
    return {"id": assignment_id, "resource_id": registry_id, "scope": scope, "principal_id": principal,
            "role_definition_id": role_id, "condition_present": bool(text(properties.get("condition"), source_id)),
            "source_id": source_id, "observed_at": reference, "cluster_id": config["id"], "cluster_name": config["name"],
            "effective_pull_permission_verified": False}


def kube_reader(arm, identity, cluster, config):
    if cluster["managed_entra"] is not True or cluster["enable_rbac"] is not True:
        raise EvidenceError("entra_required", "Kubernetes collection requires managed Entra and RBAC; non-Entra ClusterUser can be admin.", "kube_access")
    url = ARM_ORIGIN + config["id"] + "/listClusterUserCredential?" + urlencode({"api-version": AKS_API, "format": "exec"})
    arm.allow(url, "POST")
    payload = arm.request("POST", url, "kube_access", body={})
    configs = array(payload.get("kubeconfigs"), "kube_access")
    if len(configs) != 1:
        raise EvidenceError("invalid_kubeconfig", "Expected exactly one scoped clusterUser kubeconfig.", "kube_access")
    try:
        import yaml
    except ImportError as exc:
        raise EvidenceError("dependency_missing", "Kubernetes reads require PyYAML for safe kubeconfig parsing.", "kube_access") from exc
    value = configs[0].get("value")
    if not isinstance(value, str) or len(value) > 131072:
        raise EvidenceError("invalid_kubeconfig", "Credential response exceeded the bounded shape.", "kube_access")
    try:
        document = obj(yaml.safe_load(base64.b64decode(value, validate=True).decode("utf-8")), "kube_access")
    except (ValueError, UnicodeDecodeError, binascii.Error, yaml.YAMLError) as exc:
        raise EvidenceError("invalid_kubeconfig", "Invalid clusterUser configuration; credential material is never exported.", "kube_access") from exc
    contexts = array(document.get("contexts"), "kube_access")
    selected = [item for item in contexts if item.get("name") == document.get("current-context")]
    if len(selected) != 1:
        raise EvidenceError("invalid_kubeconfig", "Current context is not unique.", "kube_access")
    cluster_name = pick(selected[0], "context", "cluster")
    selected_clusters = [item for item in array(document.get("clusters"), "kube_access") if item.get("name") == cluster_name]
    if len(selected_clusters) != 1:
        raise EvidenceError("invalid_kubeconfig", "Selected TLS cluster is not unique.", "kube_access")
    for user in array(document.get("users", []), "kube_access"):
        if set(obj(user.get("user", {}), "kube_access")) & {
            "token", "tokenFile", "password", "username", "client-certificate", "client-certificate-data",
            "client-key", "client-key-data", "auth-provider",
        }:
            raise EvidenceError("local_credentials_rejected", "Local certificates, static tokens and authentication fallbacks are forbidden.", "kube_access")
    tls = obj(selected_clusters[0].get("cluster"), "kube_access")
    server = text(tls.get("server"), "kube_access", 512)
    try:
        parsed = urlsplit(server or "")
        port = parsed.port
    except ValueError as exc:
        raise EvidenceError("unsafe_kube_endpoint", "Malformed Kube HTTPS endpoint; no alternate server is used.", "kube_access") from exc
    hosts = {host.lower() for host in (cluster["fqdn"], cluster["private_fqdn"]) if host}
    if (
        parsed.scheme != "https" or parsed.hostname not in hosts or not parsed.hostname.endswith(".azmk8s.io")
        or port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment
        or parsed.username or parsed.password or tls.get("insecure-skip-tls-verify") or "certificate-authority" in tls
    ):
        raise EvidenceError("unsafe_kube_endpoint", "Kube server must exactly match the approved ARM cluster FQDN with strict inline CA TLS.", "kube_access")
    ca = tls.get("certificate-authority-data")
    if not isinstance(ca, str) or len(ca) > 65536:
        raise EvidenceError("unsafe_tls", "A bounded public CA from clusterUser is required; no file or insecure fallback.", "kube_access")
    try:
        ca_text = base64.b64decode(ca, validate=True).decode("ascii")
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=ca_text)
    except (ValueError, UnicodeDecodeError, binascii.Error, ssl.SSLError) as exc:
        raise EvidenceError("unsafe_tls", "Cluster public CA could not initialize verified TLS.", "kube_access") from exc
    return HttpReader(identity, "https://" + parsed.hostname, AKS_AUDIENCE, context)


def collect_live(config, reference, deadline=None):
    global ACTIVE_SUBSCRIPTION_ID
    previous, ACTIVE_SUBSCRIPTION_ID = ACTIVE_SUBSCRIPTION_ID, scope_subscription(config)
    try:
        return _collect_live(config, reference, deadline)
    finally:
        ACTIVE_SUBSCRIPTION_ID = previous


def _collect_live(config, reference, deadline=None):
    identity = Identity()
    try:
        if deadline is not None:
            identity.deadline = min(identity.deadline, deadline)
        arm = HttpReader(identity)
        raw = {"sources": [], "inventory": {}, "requirements": config["requirements"], "registry_role_assignments": []}
        cluster_url = ARM_ORIGIN + config["id"] + "?api-version=" + AKS_API
        cluster, receipt = read_one(arm, cluster_url, "arm_cluster", reference,
                                    lambda item: normalize_cluster(item, "arm_cluster", reference, config))
        raw["sources"].append(receipt)
        if cluster is None:
            error = receipt["errors"][0]
            raise EvidenceError(error["code"], "The approved primary cluster could not be read; no synthetic/empty live report.", "arm_cluster", error["http_status"])
        raw["cluster"] = cluster
        pools, receipt = read_pages(arm, ARM_ORIGIN + config["id"] + "/agentPools?api-version=" + AKS_API,
                                    "arm_node_pools", reference, lambda item: normalize_pool(item, "arm_node_pools", reference, config))
        raw["inventory"]["node_pools"] = pools
        raw["sources"].append(receipt)
        for key, suffix, version in (
            ("arm_maintenance", "/maintenanceConfigurations", AKS_API),
            ("arm_diagnostics", "/providers/Microsoft.Insights/diagnosticSettings", "2021-05-01-preview"),
        ):
            def normalized(item, source_id=key):
                row = common_row(item, source_id, reference, config)
                props = obj(item.get("properties"), source_id)
                if source_id == "arm_diagnostics":
                    row.update(logs=[{"category": text(log.get("category"), source_id), "category_group": text(log.get("categoryGroup"), source_id),
                                      "enabled": boolean(log.get("enabled"), source_id)} for log in array(props.get("logs", []), source_id)],
                               workspace_id=text(props.get("workspaceId"), source_id),
                               storage_account_id=text(props.get("storageAccountId"), source_id),
                               eventhub_authorization_rule_id=text(props.get("eventHubAuthorizationRuleId"), source_id))
                else:
                    row["configured"] = bool(props.get("maintenanceWindow") or props.get("timeInWeek") or props.get("notAllowedTime"))
                return row
            rows, receipt = read_pages(arm, ARM_ORIGIN + config["id"] + suffix + "?api-version=" + version,
                                      key, reference, normalized)
            raw[key] = rows
            raw["sources"].append(receipt)
        versions_url = (ARM_ORIGIN + "/subscriptions/" + scope_subscription(config) + "/providers/Microsoft.ContainerService/locations/"
                        + quote(cluster["region"] or "", safe="") + "/kubernetesVersions?api-version=" + AKS_API)
        def version_row(item):
            return {"id": text(item.get("version"), "arm_regional_versions"), "version": text(item.get("version"), "arm_regional_versions"),
                    "is_preview": boolean(item.get("isPreview"), "arm_regional_versions"),
                    "is_default": boolean(item.get("isDefault"), "arm_regional_versions"),
                    "support_plans": strings(pick(item, "capabilities", "supportPlan"), "arm_regional_versions"),
                    "patch_versions": {text(key, "arm_regional_versions"): {
                        "upgrades": strings(pick(value, "upgrades"), "arm_regional_versions")
                    } for key, value in obj(item.get("patchVersions", {}), "arm_regional_versions").items()}}
        raw["regional_versions"], receipt = read_pages(arm, versions_url, "arm_regional_versions", reference, version_row, value_key="values")
        raw["sources"].append(receipt)
        def upgrade_row(item):
            props = obj(item.get("properties"), "arm_upgrade_profile")
            profile = obj(props.get("controlPlaneProfile"), "arm_upgrade_profile")
            return {"version": text(profile.get("kubernetesVersion"), "arm_upgrade_profile"),
                    "upgrades": [{key: boolean(value.get(key), "arm_upgrade_profile") if key == "isPreview"
                                  else text(value.get(key), "arm_upgrade_profile") for key in ("kubernetesVersion", "isPreview")}
                                 for value in array(profile.get("upgrades", []), "arm_upgrade_profile")]}
        raw["upgrade_profile"], receipt = read_one(arm, ARM_ORIGIN + config["id"] + "/upgradeProfiles/default?api-version=" + AKS_API,
                                                   "arm_upgrade_profile", reference, upgrade_row)
        raw["sources"].append(receipt)
        related, inventory_receipt = [], source_receipt("arm_related_inventory", reference, "not_configured",
                                                       "Only approved IDs, directly referenced assets and the exact ARM nodeResourceGroup are in scope.")
        related_ids = {value.lower(): (value, {"type": "operator_scoped", "source_id": "configuration"})
                       for value in config["approved_related_resource_ids"]}
        for pool in pools:
            for field in ("vnet_subnet_id", "pod_subnet_id"):
                value = pool[field]
                if value and same_subscription_id(value):
                    related_ids[value.lower()] = (value, {"type": "direct_reference", "source_id": pool["source_id"], "field": field})
        for value in cluster["effective_outbound_ip_ids"]:
            related_ids[value.lower()] = (value, {"type": "direct_reference", "source_id": "arm_cluster", "field": "effective_outbound_ip_ids"})
        for field in ("private_dns_zone", "kubelet_identity_resource_id"):
            value = cluster.get(field)
            if value and value.startswith("/subscriptions/") and same_subscription_id(value):
                related_ids[value.lower()] = (value, {"type": "direct_reference", "source_id": "arm_cluster", "field": field})
        for value in cluster["identity"]["user_assigned_resource_ids"]:
            related_ids[value.lower()] = (value, {
                "type": "direct_reference", "source_id": "arm_cluster", "field": "identity.user_assigned_resource_ids"})
        node_rg = cluster["node_resource_group"]
        if config["include_node_resource_group"] and node_rg:
            if not re.fullmatch(r"[A-Za-z0-9_.()-]{1,90}", node_rg):
                raise EvidenceError("unsafe_scope", "ARM nodeResourceGroup has unsafe syntax.", "arm_related_inventory")
            url = ARM_ORIGIN + "/subscriptions/" + scope_subscription(config) + "/resourceGroups/" + node_rg + "/resources?api-version=2021-04-01"
            items, inventory_receipt = read_pages(arm, url, "arm_related_inventory", reference,
                                                   lambda item: common_row(item, "arm_related_inventory", reference, config))
            raw["inventory"]["related_resource_index"] = items
            for item in items:
                if resource_type(item["id"]) in RESOURCE_APIS:
                    related_ids.setdefault(item["id"].lower(), (item["id"], {"type": "node_resource_group_context", "source_id": "arm_cluster",
                                                                           "node_resource_group": node_rg}))
        raw["sources"].append(inventory_receipt)
        if len(related_ids) > MAX_RELATED_RESOURCES:
            raise EvidenceError("related_scope_cap", "Associated resource scope exceeds its bound; no narrowed report is substituted.", "arm_related_inventory")
        pending = list(related_ids.values())
        next_related = 0
        def queue_related(value, association):
            value = same_subscription_id(value)
            if not value:
                return
            if value.lower() in related_ids:
                return
            if len(related_ids) >= MAX_RELATED_RESOURCES:
                raise EvidenceError("related_scope_cap", "Exact referenced resource closure exceeds its bound; no narrowed report.", "arm_related_inventory")
            related_ids[value.lower()] = (value, association)
            pending.append((value, association))

        def read_related():
            nonlocal next_related
            while next_related < len(pending):
                resource_id, association = pending[next_related]
                next_related += 1
                kind = resource_type(resource_id)
                source_id = "arm_resource_" + str(next_related)
                if kind not in RESOURCE_APIS:
                    raw["sources"].append(source_receipt(source_id, reference, "not_applicable", "Approved resource type has no whitelisted detail reader."))
                    continue
                def normalized(item):
                    row = normalize_resource(item, source_id, reference, config, association)
                    if row["id"].lower() != resource_id.lower():
                        raise EvidenceError("scope_mismatch", "ARM detail response differs from the exact requested related resource.", source_id)
                    return row
                row, receipt = read_one(arm, ARM_ORIGIN + resource_id + "?api-version=" + RESOURCE_APIS[kind], source_id, reference, normalized)
                receipt["resource_id"] = resource_id
                raw["sources"].append(receipt)
                if row:
                    related.append(row)
                    for value, field in referenced_resource_ids(row):
                        queue_related(value, {"type": "direct_reference", "source_id": row["source_id"],
                                              "resource_id": row["id"], "field": field})
        read_related()
        for index, registry in enumerate(row for row in related if row["resource_kind"] == "microsoft.containerregistry/registries"):
            source_id = "arm_registry_roles_" + str(index + 1)
            principal = cluster["identity"]["kubelet_principal_id"]
            if not principal:
                raw["sources"].append(source_receipt(source_id, reference, "not_configured",
                    "No observed kubelet managed-identity principal; no arbitrary/personal identities are queried.", resource_id=registry["id"]))
                continue
            try:
                UUID(principal)
            except ValueError as exc:
                raise EvidenceError("invalid_principal_id", "Observed kubelet identity is not a GUID.", source_id) from exc
            url = ARM_ORIGIN + registry["id"] + "/providers/Microsoft.Authorization/roleAssignments?" + urlencode({
                "api-version": "2022-04-01", "$filter": "principalId eq '" + principal + "'"})
            rows, receipt = read_pages(arm, url, source_id, reference, lambda item: normalize_registry_role(
                item, source_id, reference, config, registry["id"], principal))
            receipt["resource_id"] = registry["id"]
            receipt["note"] += " Fixed kubelet-principal role metadata only; role definitions, conditions and effective data-plane image-pull access are not evaluated."
            raw["sources"].append(receipt)
            raw["registry_role_assignments"].extend(rows)
        raw["inventory"]["related_resources"] = related
        kube = None
        if config["kubernetes_enabled"] and cluster["power_state"] != "Stopped":
            try:
                kube = kube_reader(arm, identity, cluster, config)
                receipt = source_receipt("kube_access", reference, records=1, pages=1,
                                         note="Managed Entra clusterUser + ephemeral AKS-audience UAMI token; strict CA/FQDN TLS, GET only.")
            except EvidenceError as exc:
                receipt = source_receipt("kube_access", reference, "denied" if exc.http_status in (401, 403) else "error", errors=[exc.public()])
        else:
            receipt = source_receipt("kube_access", reference, "not_applicable" if cluster["power_state"] == "Stopped" else "not_configured",
                                     "Stopped clusters are never started; disabled collection does not imply an empty cluster.")
        raw["sources"].append(receipt)
        for collection, path in KUBE_PATHS.items():
            source_id = "kube_" + collection
            if kube is None:
                raw["inventory"][collection] = []
                raw["sources"].append(source_receipt(source_id, reference, receipt["status"],
                                                     "Kubernetes access was not established; no empty-success inference."))
                continue
            rows, page_receipt = read_pages(kube, kube.origin + path + "?limit=" + str(KUBE_PAGE_SIZE), source_id, reference,
                                            lambda item, c=collection, s=source_id: normalize_kube(item, c, s, reference, config), kubernetes=True,
                                            metrics_snapshot=collection in ("node_metrics", "pod_metrics"))
            raw["inventory"][collection] = rows
            raw["sources"].append(page_receipt)
        raw["inventory"]["workloads"] = [row for collection in ("deployments", "statefulsets", "daemonsets", "jobs", "cronjobs")
                                        for row in raw["inventory"][collection]]
        for pv in raw["inventory"]["pvs"]:
            handle = pv.get("csi_volume_handle")
            if pv.get("csi_driver") == "disk.csi.azure.com" and handle and handle.lower().startswith("/subscriptions/"):
                value = same_subscription_id(handle)
                if value and resource_type(value) == "microsoft.compute/disks":
                    queue_related(value, {"type": "direct_reference", "source_id": pv["source_id"],
                                          "kubernetes_uid": pv["uid"], "field": "csi_volume_handle"})
        read_related()
        advisor_source = source_receipt("arm_advisor", reference, "not_configured", "Cached recommendations only; no generateRecommendations action.")
        raw["advisor"] = []
        if config["advisor_enabled"]:
            url = ARM_ORIGIN + "/subscriptions/" + scope_subscription(config) + "/providers/Microsoft.Advisor/recommendations?" + urlencode({
                "api-version": "2025-01-01", "$filter": "ResourceId eq '" + config["id"] + "'", "$top": "100"})
            def advisor_row(item):
                advisory_id = text(item.get("id"), "arm_advisor", 2048)
                if not advisory_id or not advisory_id.lower().startswith("/subscriptions/" + scope_subscription(config).lower() + "/"):
                    raise EvidenceError("scope_mismatch", "Advisor ID changed subscription scope.", "arm_advisor")
                row = {"id": advisory_id, "resource_id": config["id"], "name": text(item.get("name"), "arm_advisor"),
                       "source_id": "arm_advisor", "observed_at": reference, "cluster_name": config["name"]}
                target = pick(item, "properties", "resourceMetadata", "resourceId")
                if not isinstance(target, str) or target.lower() != config["id"].lower():
                    raise EvidenceError("scope_mismatch", "Advisor returned a resource outside the exact cluster filter.", "arm_advisor")
                row.update(target_resource_id=target, category=text(pick(item, "properties", "category"), "arm_advisor"),
                           impact=text(pick(item, "properties", "impact"), "arm_advisor"),
                           last_updated=text(pick(item, "properties", "lastUpdated"), "arm_advisor"),
                           problem=text(pick(item, "properties", "shortDescription", "problem"), "arm_advisor"),
                           solution=text(pick(item, "properties", "shortDescription", "solution"), "arm_advisor"))
                return row
            raw["advisor"], advisor_source = read_pages(arm, url, "arm_advisor", reference, advisor_row)
        raw["sources"].append(advisor_source)
        raw["cost_rows"], cost_source = collect_cost(arm, config, reference, related, cluster)
        raw["sources"].append(cost_source)
        return raw
    finally:
        identity.close()


def collect_cost(reader, config, reference, related, cluster=None):
    ids = config["approved_cost_resource_ids"]
    declared = datetime.fromisoformat(reference.replace("Z", "+00:00"))
    monthly = config["cost_timeframe"] == "month_to_date"
    start_time = declared.replace(day=1, hour=0, minute=0, second=0, microsecond=0) if monthly else declared - timedelta(days=30)
    start = start_time.isoformat().replace("+00:00", "Z")
    mode = config["cost_scope"]
    receipt = source_receipt("azure_cost", reference, "not_configured", "No approved billing scope; no namespace/workload billing inference.",
                             window={"start": start, "end": reference}, timeframe="MonthToDate" if monthly else "Custom",
                             granularity="None" if monthly else "Daily", cost_type="ActualCost", unit="billing_currency", pagination_complete=False)
    if mode == "disabled":
        return [], receipt
    scope = "/subscriptions/" + scope_subscription(config)
    node_rg = pick(cluster, "node_resource_group")
    observed_cluster_id = pick(cluster, "id")
    if mode == "node_resource_group":
        if (
            not isinstance(node_rg, str) or not re.fullmatch(r"[A-Za-z0-9_.()-]{1,90}", node_rg)
            or not isinstance(observed_cluster_id, str) or observed_cluster_id.lower() != config["id"].lower()
        ):
            receipt.update(status="error", errors=[EvidenceError("unverified_cost_scope", "Billing nodeResourceGroup must come from the exact observed approved ARM cluster.", "azure_cost").public()])
            return [], receipt
        scope += "/resourceGroups/" + node_rg
        receipt["association"] = {"type": "node_resource_group_billing_context", "source_id": "arm_cluster", "node_resource_group": node_rg}
        receipt["note"] = "All returned billing rows in exact ARM-observed nodeResourceGroup, including historical/deleted/VMSS child IDs; not current inventory or workload ownership."
    else:
        observed_ids = {row["resource_id"].lower() for row in related} | {config["id"].lower()}
        if not ids or any(value.lower() not in observed_ids for value in ids):
            receipt.update(status="error", errors=[EvidenceError("unverified_cost_scope", "Fixed cost IDs must also be observed in the collected fixed inventory.", "azure_cost").public()])
            return [], receipt
        receipt["association"] = {"type": "approved_observed_resource_ids", "source_id": "configuration"}
    url = ARM_ORIGIN + scope + "/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
    receipt.update(scope=scope, read_signature="POST " + url)
    reader.allow(url, "POST")
    body = {"type": "ActualCost", "timeframe": "MonthToDate" if monthly else "Custom",
            "dataset": {"granularity": "None" if monthly else "Daily",
                        "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": "ResourceId"}]}}
    if not monthly:
        body["timePeriod"] = {"from": start, "to": reference}
    if mode == "resource_ids":
        body["dataset"]["filter"] = {"dimensions": {"name": "ResourceId", "operator": "In", "values": ids}}
    rows, seen = [], set()
    current = url
    try:
        for page_number in range(MAX_PAGES):
            if current in seen:
                raise EvidenceError("pagination_loop", "Cost query repeated a continuation.", "azure_cost")
            seen.add(current)
            payload = reader.request("POST", current, "azure_cost", body=body, allow_empty=True)
            receipt["pages"] = page_number + 1
            if not payload:
                receipt.update(status="complete", pagination_complete=True, records=len(rows), empty_http_204=True)
                return rows, receipt
            props = obj(payload.get("properties"), "azure_cost")
            columns = array(props.get("columns"), "azure_cost")
            names = [text(item.get("name"), "azure_cost", 128) for item in columns]
            receipt["columns"] = [{"name": name, "type": text(item.get("type"), "azure_cost", 32)} for name, item in zip(names, columns)]
            required = {"ResourceId", "Currency"} | (set() if monthly else {"UsageDate"})
            if len(names) != len(set(names)) or not required <= set(names) or not ("PreTaxCost" in names or "Cost" in names):
                raise EvidenceError("invalid_cost_shape", "Required cost columns are absent or repeated.", "azure_cost")
            values = props.get("rows")
            if not isinstance(values, list) or len(rows) + len(values) > MAX_ROWS:
                raise EvidenceError("row_cap", "Cost rows are invalid or exceeded the safety bound.", "azure_cost")
            for value in values:
                if not isinstance(value, list) or len(value) != len(names):
                    raise EvidenceError("invalid_cost_shape", "Cost row/column lengths differ.", "azure_cost")
                record = dict(zip(names, value))
                resource_id = arm_id(record["ResourceId"], scope_subscription(config))
                if (mode == "resource_ids" and resource_id.lower() not in {item.lower() for item in ids}) or (
                    mode == "node_resource_group" and resource_id.split("/")[4].lower() != node_rg.lower()
                ):
                    raise EvidenceError("scope_mismatch", "Cost query returned an unapproved resource ID.", "azure_cost")
                currency = text(record["Currency"], "azure_cost", 8)
                if not currency or not re.fullmatch(r"[A-Z]{3}", currency):
                    raise EvidenceError("invalid_currency", "Cost currency is not an observed ISO-style currency code.", "azure_cost")
                date = record.get("UsageDate")
                if date is not None:
                    if not re.fullmatch(r"\d{8}", str(date)):
                        raise EvidenceError("invalid_cost_date", "Cost UsageDate is not a dated daily row.", "azure_cost")
                    day = datetime.strptime(str(date), "%Y%m%d").replace(tzinfo=timezone.utc)
                    if not start_time.date() <= day.date() <= declared.date():
                        raise EvidenceError("cost_window_mismatch", "A cost row lies outside the requested UTC window.", "azure_cost")
                date = str(date) if date is not None else None
                key = (resource_id.lower(), date, currency)
                if any((r["resource_id"].lower(), r["usage_date"], r["currency"]) == key for r in rows):
                    raise EvidenceError("invalid_pagination", "Cost pagination repeated a resource/date/currency row.", "azure_cost")
                amount = number(record.get("PreTaxCost", record.get("Cost")), "azure_cost")
                if amount is None:
                    raise EvidenceError("invalid_cost_amount", "Cost amount is absent; it is not zero.", "azure_cost")
                rows.append({"resource_id": resource_id, "usage_date": date, "amount": amount,
                             "currency": currency, "source_id": "azure_cost", "observed_at": reference,
                             "cluster_name": config["name"], "window": receipt["window"], "association": receipt["association"],
                             "current_inventory_match": resource_id.lower() in {row["resource_id"].lower() for row in related},
                             "basis": "ActualCost/PreTaxCost in exact approved scope; billing history is not current inventory, cluster ownership or Kubernetes allocation"})
            receipt["records"] = len(rows)
            link = props.get("nextLink")
            if not link:
                receipt.update(status="complete", pagination_complete=True)
                return rows, receipt
            current = next_arm_link(link, url)
        raise EvidenceError("page_cap", "Cost query page safety cap reached.", "azure_cost")
    except (EvidenceError, ValueError) as exc:
        error = exc if isinstance(exc, EvidenceError) else EvidenceError("invalid_cost_date", "Cost date is not a valid calendar date.", "azure_cost")
        receipt.update(status="partial" if rows else "denied" if error.http_status in (401, 403) else "error",
                       records=len(rows), errors=[error.public()])
        return rows, receipt


def selector_matches(selector, actual_labels):
    if selector is None or actual_labels is None:
        return None
    if any(actual_labels.get(key) != value for key, value in (selector.get("match_labels") or {}).items()):
        return False
    for expression in selector.get("match_expressions") or []:
        key, op, values = expression["key"], expression["operator"], expression["values"]
        if op == "In" and (key not in actual_labels or actual_labels[key] not in values):
            return False
        if op == "NotIn" and key in actual_labels and actual_labels[key] in values:
            return False
        if op == "Exists" and key not in actual_labels:
            return False
        if op == "DoesNotExist" and key in actual_labels:
            return False
    return True


def outcome(status, reason, value=None, source_ids=None):
    return {"status": status, "reason": reason, "value": value or {}, "source_ids": source_ids or []}


def decision(value, reason, observed, sources=None):
    return outcome("not_evaluated" if value is None else "passed" if value else "failed", reason, observed, sources)


def source_complete(raw, *ids):
    sources = {source["id"]: source for source in raw["sources"]}
    return all(pick(sources.get(source_id), "status") == "complete" for source_id in ids)


def expected(raw, key, row=None):
    requirements = raw.get("requirements", {})
    if row and row.get("namespace") and row.get("name"):
        values = pick(requirements, "workloads", row["namespace"] + "/" + row["name"])
        if isinstance(values, dict) and key in values:
            return values[key]
        if key in ("availability_required", "autoscaling_required", "probes_required"):
            return None
    return requirements.get(key)


def conditional(value, requirement, reason, observed, sources=None):
    if requirement is False:
        return outcome("not_applicable", "El requisito confirmado no aplica. " + reason, observed, sources)
    if value is True:
        return decision(True, reason + " Configuracion observada; no prueba eficacia.", observed, sources)
    if requirement is not True:
        return outcome("not_evaluated", "Falta requisito o control alternativo confirmado. " + reason, observed, sources)
    return decision(value, reason + " Requisito confirmado; ausencia desconocida no se convierte en fallo.", observed, sources)


def serving_workload(raw, row):
    requirement = expected(raw, "probes_required", row)
    if requirement is not None:
        return requirement
    if not source_complete(raw, "kube_services"):
        return None
    for service in raw["inventory"].get("services", []):
        selector = service.get("selector")
        if service.get("namespace") == row.get("namespace") and selector and selector.get("match_labels"):
            if selector_matches(selector, row.get("template_labels")) is True and service.get("ports"):
                return True
    return None


def evaluate_cluster(rule, row, raw):
    test, cluster, inventory = rule["test"], raw["cluster"], raw["inventory"]
    if test == "managed_identity":
        value = cluster.get("identity_type")
        return decision(None if value is None else any(kind in value.split(", ") for kind in ("SystemAssigned", "UserAssigned")),
                        "Tipo de identidad ARM observado; no se exportan credenciales.", {"identity_type": value})
    if test == "managed_entra":
        return decision(cluster.get("managed_entra"), "aadProfile.managed observado; no acredita permisos efectivos.",
                        {"managed_entra": cluster.get("managed_entra")})
    if test == "rbac":
        return decision(cluster.get("enable_rbac"), "enableRBAC observado; Azure RBAC y minimo privilegio son controles distintos.",
                        {key: cluster.get(key) for key in ("enable_rbac", "azure_rbac")})
    if test == "local_accounts":
        return conditional(cluster.get("disable_local_accounts"), cluster.get("managed_entra"),
                           "disableLocalAccounts observado.", {"disable_local_accounts": cluster.get("disable_local_accounts")})
    if test == "api_access":
        private, ranges = cluster.get("private_cluster"), cluster.get("authorized_ip_ranges")
        value = True if private is True or (ranges and not any(v in ("0.0.0.0/0", "::/0") for v in ranges)) else (
            False if private is False and ranges is not None else None)
        return decision(value, "Endpoint privado o rangos publicos limitados; no prueba conectividad, autenticacion ni seguridad total.",
                        {"private_cluster": private, "authorized_ip_ranges": ranges})
    if test == "workload_identity":
        wi, oidc = cluster.get("workload_identity_enabled"), cluster.get("oidc_enabled")
        value = True if wi is True and oidc is True else False if wi is False or oidc is False else None
        return conditional(value, expected(raw, "workload_identity_required"), "Workload ID/OIDC del cluster, no uso por pod.",
                           {"workload_identity_enabled": wi, "oidc_enabled": oidc})
    if test == "network_policy_engine":
        network = cluster.get("network", {})
        value = True if network.get("dataplane") == "cilium" or network.get("policy") in ("azure", "calico", "cilium") else (
            False if network.get("policy") == "none" else None)
        return conditional(value, expected(raw, "network_policy_required"), "Motor observado; Cilium se admite sin exigir Calico.",
                           {"dataplane": network.get("dataplane"), "policy": network.get("policy")})
    if test == "cni":
        network = cluster.get("network", {})
        value = True if network.get("plugin") == "azure" and network.get("plugin_mode") == "overlay" else (
            False if network.get("plugin") is not None else None)
        return conditional(value, expected(raw, "overlay_required"), "Overlay es recomendacion actual, no obligacion universal.",
                           network)
    if test in ("policy", "defender"):
        value = pick(cluster, "addons", "azurepolicy", "enabled") if test == "policy" else cluster.get("defender_enabled")
        return conditional(value, expected(raw, test + "_required"), "Solo opcion habilitada; cumplimiento, salud y licencia no se han verificado.",
                           {test + "_enabled": value})
    if test == "sla_tier":
        if expected(raw, "sla_required") is not True:
            return outcome("not_applicable" if expected(raw, "sla_required") is False else "not_evaluated",
                           "No se conoce un requisito de SLA de produccion; LAB no se trata como produccion.", {"sku_tier": cluster.get("sku_tier")})
        tier = cluster.get("sku_tier")
        return decision(True if tier in ("Standard", "Premium") else False if tier == "Free" else None,
                        "Tier actual Standard/Premium; valores legacy o desconocidos requieren revision.", {"sku_tier": tier})
    if test == "zones":
        zones = row.get("availability_zones")
        value = len(set(zones)) >= 2 if zones is not None else None
        return conditional(value, expected(raw, "availability_zones_required"), "Zonas configuradas del pool; disponibilidad regional y excepciones requieren requisito confirmado.",
                           {"name": row.get("name"), "availability_zones": zones})
    if test == "pool_state":
        value = row.get("provisioning_state")
        return decision(True if value == "Succeeded" else False if value in ("Failed", "Canceled") else None,
                        "Estado de aprovisionamiento del pool, distinto de Running del cluster y salud del workload.",
                        {"name": row.get("name"), "provisioning_state": value, "cluster_power_state": cluster.get("power_state")})
    if test == "system_taint":
        if row.get("mode") != "System":
            return outcome("not_applicable", "No es un pool System.", {"mode": row.get("mode")})
        taints = row.get("node_taints")
        return decision(None if taints is None else any(v.startswith("CriticalAddonsOnly=") or v.startswith("CriticalAddonsOnly:") for v in taints),
                        "Taint de proteccion observado; controles de aislamiento equivalentes requieren revision.",
                        {"name": row.get("name"), "node_taints": taints})
    if test == "system_isolation":
        if not source_complete(raw, "kube_nodes", "kube_pods", "arm_node_pools"):
            return outcome("not_evaluated", "El join pool-node-pod exige inventarios completos; solo tener pools System no prueba colocacion de aplicaciones.",
                           source_ids=["kube_nodes", "kube_pods", "arm_node_pools"])
        system_pools = {pool["name"] for pool in inventory.get("node_pools", []) if pool.get("mode") == "System"}
        nodes = {node["name"]: node.get("pool_name") for node in inventory.get("nodes", [])}
        apps = [pod for pod in inventory.get("pods", []) if pod.get("namespace") not in SYSTEM_NAMESPACES
                and pod.get("phase") not in ("Succeeded", "Failed")]
        placed = [pod for pod in apps if pod.get("node_name") in nodes]
        if not apps:
            return outcome("not_applicable", "No se observaron pods de aplicacion activos en la coleccion completa.")
        if len(placed) != len(apps) or any(nodes.get(pod["node_name"]) is None for pod in placed):
            return outcome("not_evaluated", "No todos los pods de aplicacion tienen un join node/pool verificable.")
        affected = [{"namespace": pod["namespace"], "pod": pod["name"], "node": pod["node_name"], "pool": nodes[pod["node_name"]]}
                    for pod in placed if nodes[pod["node_name"]] in system_pools]
        return decision(not affected, "Solo joins exactos observados; nombres de nodos no se interpretan como prueba de pool.",
                        {"application_pods": len(apps), "pods_on_system_pools": affected}, ["kube_nodes", "kube_pods", "arm_node_pools"])
    if test == "autoscaling":
        nap = cluster.get("node_provisioning_mode") == "Auto" or cluster.get("mode") == "Automatic"
        pools = inventory.get("node_pools", [])
        observed = {"node_provisioning_mode": cluster.get("node_provisioning_mode"), "mode": cluster.get("mode"),
                    "pool_auto_scaling": [{"name": p["name"], "enabled": p.get("enable_auto_scaling")} for p in pools]}
        value = True if nap or any(p.get("enable_auto_scaling") is True for p in pools) else (
            False if pools and source_complete(raw, "arm_node_pools") and all(p.get("enable_auto_scaling") is False for p in pools) else None)
        return conditional(value, expected(raw, "autoscaling_required"), "NAP/Automatic o autoscaler configurado; no eficacia ni ahorro medidos.",
                           observed, ["arm_cluster", "arm_node_pools"])
    if test in ("upgrade_policy", "node_image_policy"):
        key = "channel" if test == "upgrade_policy" else "node_os_channel"
        value = pick(cluster, "auto_upgrade", key)
        enabled = None if value is None else value.lower() != "none"
        requirement = "automatic_upgrades_required" if test == "upgrade_policy" else "node_image_upgrades_required"
        return conditional(enabled, expected(raw, requirement), "Canal observado; un proceso manual desconocido no se etiqueta como inexistente.",
                           {"auto_upgrade": cluster.get("auto_upgrade")})
    if test == "version_support":
        versions, profile, current = raw.get("regional_versions", []), raw.get("upgrade_profile"), cluster.get("version")
        observed = {"current_version": current, "support_plan": cluster.get("support_plan"), "upgrade_profile": profile,
                    "regional_version_count": len(versions)}
        if not source_complete(raw, "arm_regional_versions", "arm_upgrade_profile") or not current:
            return outcome("not_evaluated", "Faltan catalogo regional actual o upgrade profile; no se usa version latest fija.", observed,
                           ["arm_regional_versions", "arm_upgrade_profile", "arm_cluster"])
        known = any(current in (v.get("patch_versions") or {}) or current == v.get("version") for v in versions)
        return decision(True if known else None,
                        "Version presente en catalogo regional actual." if known else
                        "Ausente del catalogo de oferta; esto no acredita fin de soporte (LTS/politica regional requieren validacion).",
                        observed, ["arm_regional_versions", "arm_upgrade_profile", "arm_cluster"])
    if test == "diagnostics":
        rows = raw.get("arm_diagnostics", [])
        enabled = any(log.get("enabled") is True for r in rows for log in r.get("logs", []))
        value = True if enabled else False if source_complete(raw, "arm_diagnostics") else None
        return conditional(value, expected(raw, "diagnostics_required"), "Diagnostic settings/logs configurados; entrega e ingesta no se han medido.",
                           {"settings": rows}, ["arm_diagnostics"])
    if test == "maintenance":
        rows = raw.get("arm_maintenance", [])
        value = True if any(r.get("configured") is True for r in rows) else False if source_complete(raw, "arm_maintenance") else None
        return conditional(value, expected(raw, "maintenance_required"), "Ventana configurada observada, no ejecucion validada.",
                           {"maintenance_configurations": rows}, ["arm_maintenance"])
    if test == "telemetry":
        rows = inventory.get("node_metrics", []) + inventory.get("pod_metrics", [])
        measured = [row for row in rows if row.get("fresh_for_reference") is True]
        return decision(True if measured else None, "Muestras metrics.k8s.io actuales; sin historial, logs ni garantia de cobertura mensual.",
                        {"observed_samples": len(measured)}, ["kube_node_metrics", "kube_pod_metrics"])
    if test == "advisor":
        return decision(True if source_complete(raw, "arm_advisor") else None,
                        "Lectura de recomendaciones cacheadas del ResourceId exacto; vacio no prueba oportunidades inexistentes.",
                        {"recommendations": raw.get("advisor", [])}, ["arm_advisor"])
    if test in ("dns", "cost_allocation", "rightsizing", "spot"):
        reasons = {
            "dns": "Inventario de DNS/IPAM no mide resolucion, latencia, failover ni requisitos de capacidad.",
            "cost_allocation": "ActualCost de ResourceIds no acredita asignacion de factura a namespace/workload.",
            "rightsizing": "La muestra de uso actual no es una ventana historica representativa ni un ahorro calculado.",
            "spot": "No se han recogido elegibilidad comercial, historial estable ni tolerancia confirmada a eviction.",
        }
        return outcome("not_evaluated", reasons[test], {"source_data_available": bool(raw.get("cost_rows"))},
                       ["azure_cost", "kube_node_metrics", "kube_pod_metrics"] if test != "dns" else ["arm_cluster"])
    raise EvidenceError("unknown_rule", "Catalog rule has no implemented evaluator.", "rule_catalog")


def evaluate_workload(rule, row, raw):
    test = rule["test"]
    if row.get("namespace") in SYSTEM_NAMESPACES:
        return outcome("not_applicable", "Componente de namespace de sistema: requiere baseline del proveedor, no reglas generales de aplicaciones.")
    is_batch = row.get("kind") in ("Job", "CronJob")
    if test in ("probes", "pdb", "hpa", "replicas", "network_policies") and (is_batch or row.get("desired_replicas") == 0):
        return outcome("not_applicable", "Batch o replicas deseadas cero: no se declara una caida ni se impone disponibilidad continua.",
                       {"kind": row.get("kind"), "desired_replicas": row.get("desired_replicas")})
    containers = row.get("containers")
    if not containers:
        return outcome("not_evaluated", "No se dispone de especificacion de contenedores; inventario/telemetria no sustituye manifiestos.")
    if test == "resources":
        required = ["request_cpu_cores", "request_memory_bytes", "limit_memory_bytes"]
        if expected(raw, "cpu_limits_required") is True:
            required.append("limit_cpu_cores")
        combined = containers + (row.get("init_containers") or [])
        missing = [{"container": c["name"], "missing_or_zero": [key for key in required if c.get(key) is None or c[key] <= 0]} for c in combined]
        return decision(all(not item["missing_or_zero"] for item in missing), "Se comprueban reservas positivas y limite de memoria; CPU limits solo si requeridos. No son medidas de uso.",
                        {"containers": combined, "missing": missing})
    if test == "probes":
        applies = serving_workload(raw, row)
        if applies is not True:
            return outcome("not_applicable" if applies is False else "not_evaluated",
                           "Falta prueba de servicio o requisito de probes; no se presupone que todos los contenedores sirven trafico.")
        serving = [c for c in containers if c.get("ports")]
        if not serving and expected(raw, "probes_required", row) is True:
            serving = containers
        if not serving:
            return outcome("not_evaluated", "Service selecciona el workload, pero no se identifica el contenedor de servicio.")
        return decision(all(c.get("readiness_probe") and c.get("liveness_probe") for c in serving),
                        "Solo contenedores de servicio identificados; presencia no prueba que la sonda sea adecuada.",
                        {"serving_containers": serving}, [row["source_id"], "kube_services"])
    if test == "pdb":
        replicas = row.get("desired_replicas")
        applies = replicas is not None and replicas >= 2 and serving_workload(raw, row) is True
        if not applies and expected(raw, "availability_required", row) is not True:
            return outcome("not_evaluated", "PDB requiere servicio replicado o requisito de disponibilidad confirmado.",
                           {"desired_replicas": replicas})
        if not source_complete(raw, "kube_pdbs"):
            return outcome("not_evaluated", "Inventario de PDB incompleto/no autorizado; ausencia no es un fallo.", source_ids=["kube_pdbs"])
        matched = [p for p in raw["inventory"].get("pdbs", []) if p.get("namespace") == row.get("namespace")
                   and selector_matches(p.get("selector"), row.get("template_labels")) is True]
        if row.get("template_labels") is None:
            return outcome("not_evaluated", "Faltan labels de plantilla para el join PDB.")
        unprotected = [p for p in matched if p.get("min_available") in (0, "0%")
                       or p.get("max_unavailable") == "100%"
                       or isinstance(p.get("max_unavailable"), int) and replicas is not None and p["max_unavailable"] >= replicas]
        valid = [p for p in matched if p.get("min_available") is not None or p.get("max_unavailable") is not None]
        if matched and len(valid) != len(matched):
            return outcome("not_evaluated", "PDB seleccionado sin minAvailable/maxUnavailable observado.", {"matched_pdbs": matched}, ["kube_pdbs"])
        if unprotected:
            return decision(False, "PDB seleccionado permite retirar todas las replicas; no ofrece la proteccion requerida frente a interrupcion voluntaria.",
                            {"desired_replicas": replicas, "matched_pdbs": matched}, [row["source_id"], "kube_pdbs"])
        return decision(bool(matched), "Selector policy/v1 evaluado por namespace y labels; un PDB no protege interrupciones involuntarias.",
                        {"desired_replicas": replicas, "matched_pdbs": matched}, [row["source_id"], "kube_pdbs"])
    if test == "hpa":
        if not source_complete(raw, "kube_hpas"):
            return outcome("not_evaluated", "HPA no recogido completamente; no se infiere ausencia.", source_ids=["kube_hpas"])
        matched = [h for h in raw["inventory"].get("hpas", []) if h.get("namespace") == row.get("namespace")
                   and pick(h, "target", "name") == row["name"] and pick(h, "target", "kind") == row["kind"]]
        return conditional(bool(matched), expected(raw, "autoscaling_required", row), "Target de HPA exacto; metricas y eficacia requieren validacion.",
                           {"matched_hpas": matched}, [row["source_id"], "kube_hpas"])
    if test == "replicas":
        requirement, replicas = expected(raw, "availability_required", row), row.get("desired_replicas")
        return conditional(None if replicas is None else replicas >= 2, requirement, "Replicas deseadas, no disponibilidad medida.",
                           {"desired_replicas": replicas, "ready_replicas": row.get("ready_replicas")})
    if test == "container_security":
        if row.get("os_name") == "windows" or pick(row, "node_selector", "kubernetes.io/os") == "windows":
            return outcome("not_applicable", "Contenedor Windows: controles Linux no se aplican por analogia.")
        combined = containers + (row.get("init_containers") or [])
        def effective_uid(container):
            return container["run_as_user"] if container.get("run_as_user") is not None else row.get("run_as_user")
        def effective_nonroot(container):
            value = pick(container, "security", "runAsNonRoot")
            return value if value is not None else pick(row, "pod_security", "runAsNonRoot")
        dangerous = [c["name"] for c in combined if pick(c, "security", "privileged") is True
                     or pick(c, "security", "allowPrivilegeEscalation") is True or effective_uid(c) == 0]
        safe = all(pick(c, "security", "privileged") is False and pick(c, "security", "allowPrivilegeEscalation") is False
                   and (effective_nonroot(c) is True or isinstance(effective_uid(c), (int, float))
                        and effective_uid(c) > 0) for c in combined)
        return decision(False if dangerous else True if safe else None,
                        "Privilegios explicitos; sin UID/securityContext no se infiere que la imagen ejecute como root.",
                        {"containers": combined, "explicitly_elevated": dangerous, "pod_security": row.get("pod_security")})
    if test == "images":
        images = [c.get("image") for c in containers + (row.get("init_containers") or [])]
        if any(image is None for image in images):
            return outcome("not_evaluated", "Falta una referencia de imagen.")
        mutable = [image for image in images if "@sha256:" not in image
                   and (":" not in image.rsplit("/", 1)[-1] or image.rsplit(":", 1)[-1] == "latest")]
        return decision(not mutable, "latest o tag omitido observado; tag especifico no demuestra inmutabilidad ni escaneo.",
                        {"images": images, "mutable_default_references": mutable})
    if test == "network_policies":
        if not source_complete(raw, "kube_network_policies"):
            return outcome("not_evaluated", "NetworkPolicies incompletas/no autorizadas.", source_ids=["kube_network_policies"])
        matched = [p for p in raw["inventory"].get("network_policies", []) if p.get("namespace") == row.get("namespace")
                   and selector_matches(p.get("selector"), row.get("template_labels")) is True]
        if row.get("template_labels") is None:
            return outcome("not_evaluated", "Faltan labels para seleccionar politicas.")
        return conditional(bool(matched), expected(raw, "network_policy_required", row),
                           "Selector observado; tipos/reglas/motor deben validarse para acreditar aislamiento.",
                           {"matched_policies": matched}, [row["source_id"], "kube_network_policies"])
    raise EvidenceError("unknown_rule", "Workload rule has no evaluator.", "rule_catalog")


def evaluate_namespace(rule, row, raw):
    if row.get("name") in SYSTEM_NAMESPACES:
        return outcome("not_applicable", "Namespace de sistema, sujeto a baseline del proveedor.")
    if rule["test"] == "quota":
        if not source_complete(raw, "kube_resource_quotas"):
            return outcome("not_evaluated", "ResourceQuota no disponible completamente.", source_ids=["kube_resource_quotas"])
        matched = [quota for quota in raw["inventory"].get("resource_quotas", []) if quota.get("namespace") == row["name"]]
        return conditional(bool(matched), expected(raw, "quota_required"), "Solo existencia/configuracion de cuota, no consumo facturado.",
                           {"quotas": matched}, ["kube_resource_quotas", "kube_namespaces"])
    value = pick(row, "labels", "pod-security.kubernetes.io/enforce")
    active = True if value in ("baseline", "restricted") else False if value in ("privileged", None) else None
    return conditional(active, expected(raw, "pod_security_required"), "Etiqueta enforce observada; audit/warn o addon Policy no son equivalentes a cumplimiento.",
                               {"enforce": value}, ["kube_namespaces"])


def evaluate_resource(rule, row, raw):
    test = rule["test"]
    if test == "pvc_binding":
        phase = row.get("phase")
        if phase == "Bound":
            return decision(True, "PVC Bound observado; no prueba rendimiento ni recuperabilidad.", {"phase": phase, "volume_name": row.get("volume_name")})
        if phase == "Lost":
            return decision(False, "PVC Lost observado.", {"phase": phase})
        classes = raw["inventory"].get("storage_classes", [])
        sc = next((c for c in classes if c["name"] == row.get("storage_class")), None)
        consumers = [pod for pod in raw["inventory"].get("pods", []) if pod.get("namespace") == row.get("namespace")
                     and row["name"] in pod.get("pvc_names", []) and pod.get("phase") not in ("Succeeded", "Failed")]
        if phase == "Pending" and pick(sc, "volume_binding_mode") == "WaitForFirstConsumer" and not consumers:
            if source_complete(raw, "kube_pods", "kube_storage_classes"):
                return outcome("not_applicable", "WaitForFirstConsumer sin consumidor activo: binding diferido puede ser intencional.",
                               {"phase": phase, "storage_class": sc["name"]}, ["kube_pods", "kube_storage_classes", "kube_pvcs"])
        return decision(False if phase == "Pending" and consumers else None,
                        "Pending con consumidor observado requiere revision; ausencia de consumidor no autorizado no se presupone.",
                        {"phase": phase, "active_consumers": [{"namespace": p["namespace"], "pod": p["name"]} for p in consumers]},
                        ["kube_pvcs", "kube_pods", "kube_storage_classes"])
    config = row.get("configuration", {})
    if test == "load_balancer":
        sku = row.get("sku_name")
        return decision(True if sku == "Standard" else False if sku == "Basic" else None,
                        "SKU Azure observada; frontends publicos/privados no se deducen del nombre.", {"sku_name": sku, "frontends": row.get("frontends")})
    if test == "storage_tls":
        https, tls = config.get("supportsHttpsTrafficOnly"), config.get("minimumTlsVersion")
        value = False if https is False or tls in ("TLS1_0", "TLS1_1") else True if https is True and tls in ("TLS1_2", "TLS1_3") else None
        return decision(value, "Valores ARM de HTTPS y TLS minimo; no se han leido keys ni datos de Storage.", config)
    if test == "keyvault_rbac":
        return decision(True if config.get("enableRbacAuthorization") is True else None,
                        "RBAC habilitado no prueba minimo privilegio; access policies pueden requerir una excepcion justificada.", config)
    if test == "nsg_exposure":
        exposure = []
        for item in row.get("security_rules", []):
            sources = [item.get("sourceAddressPrefix")] + (item.get("sourceAddressPrefixes") or [])
            ports = [item.get("destinationPortRange")] + (item.get("destinationPortRanges") or [])
            broad = any(value in ("*", "Internet", "0.0.0.0/0", "::/0") for value in sources)
            admin = any(value in ("*", "22", "3389", "0-65535") for value in ports)
            if item.get("direction") == "Inbound" and item.get("access") == "Allow" and broad and admin:
                exposure.append(item)
        return decision(False if exposure else None, "Regla explicita amplia; asociacion, precedencia y escucha efectiva requieren revision.",
                        {"broad_administrative_rules": exposure})
    raise EvidenceError("unknown_rule", "Resource rule has no evaluator.", "rule_catalog")


def assessment_check(rule, row, result, raw, reference):
    cluster = raw["cluster"]
    namespace = row.get("namespace") or (row.get("name") if rule["level"] == "namespace" else "") or ""
    workload_name = row.get("workload_name") or ""
    resource_id = row.get("resource_id") or cluster["resource_id"]
    ref = row.get("uid") or row.get("id") or namespace + "/" + workload_name or "cluster"
    check_id = "AKS-" + rule["rule_id"] + "-" + hashlib.sha256(
        (cluster["id"].lower() + "|" + resource_id.lower() + "|" + str(ref)).encode("utf-8")).hexdigest()[:12]
    pillar_anchor = {"cost": "cost-optimization", "operations": "operational-excellence", "performance": "performance-efficiency"}.get(rule["pillar"], rule["pillar"])
    items = {item["id"]: item for item in CATALOG["checklist_items"]}
    item = items[rule["checklist_id"]]
    source_ids = result["source_ids"] or [row.get("source_id") or "arm_cluster"]
    receipts = {source["id"]: source for source in raw["sources"]}
    evidence = [{"source_id": source_id, "resource_id": resource_id,
                         "observed_at": pick(receipts.get(source_id), "time") or reference,
                         "value": result["value"] if index == 0 else {"join_source": source_id},
                         "collection_status": pick(receipts.get(source_id), "status") or "not_configured"}
                        for index, source_id in enumerate(dict.fromkeys(source_ids))]
    return {
                "check_id": check_id, "rule_id": rule["rule_id"], "checklist_id": rule["checklist_id"],
                "checklist_guid": item["guid"], "title": rule["title"], "status": result["status"],
                "level": "cluster" if rule["level"] in ("cluster", "node_pool") else "workload" if rule["level"] in ("workload", "namespace") else "resource",
                "area": rule["area"], "pillar": rule["pillar"], "severity": rule["severity"],
                "severity_label": {"critical": "Critica", "high": "Alta", "medium": "Media", "low": "Baja"}[rule["severity"]],
                "resource_id": resource_id, "resource_name": row.get("name") or cluster["name"],
                "namespace": namespace, "workload_name": workload_name, "cluster_name": cluster["name"], "cluster_id": cluster["id"],
                "kubernetes_ref": {"kind": row.get("kind"), "namespace": namespace, "name": row.get("name"), "uid": row.get("uid")} if row.get("uid") else None,
                "evidence": evidence, "business_impact": rule["business_impact"], "recommendation": rule["recommendation"],
                "learn_url": CATALOG["guide"]["url"] + "#" + pillar_anchor,
                "checklist_url": "https://github.com/Azure/review-checklists/blob/" + CATALOG["catalogSnapshot"]["commit"] + "/checklists/aks_checklist.en.json",
                "kubernetes_url": rule.get("kubernetes_url", ""), "effort": rule["effort"],
                "effort_label": {"low": "L", "medium": "M", "high": "H"}[rule["effort"]], "reason": result["reason"],
    }


def evaluate(raw, reference):
    if not CATALOG.get("rules") or len(CATALOG.get("checklist_items", [])) != CATALOG.get("catalogSnapshot", {}).get("item_count"):
        raise EvidenceError("catalog_missing", "Build the pinned complete catalog into the standalone tool.", "rule_catalog")
    checks, mapped = [], set()
    inventory = raw["inventory"]
    type_for_test = {"load_balancer": "microsoft.network/loadbalancers", "storage_tls": "microsoft.storage/storageaccounts",
                             "keyvault_rbac": "microsoft.keyvault/vaults", "nsg_exposure": "microsoft.network/networksecuritygroups"}
    for rule in CATALOG["rules"]:
        mapped.add(rule["checklist_id"])
        target = rule["level"]
        if target == "cluster":
            records = [raw["cluster"]]
        elif target == "node_pool":
            records = inventory.get("node_pools", [])
        elif target == "workload":
            records = inventory.get("workloads", [])
        elif target == "namespace":
            records = inventory.get("namespaces", [])
        elif target == "pvc":
            records = inventory.get("pvcs", [])
        else:
            records = [r for r in inventory.get("related_resources", []) if r.get("resource_kind") == type_for_test[rule["test"]]]
        if not records:
            sources = ("arm_node_pools",) if target == "node_pool" else ("kube_namespaces",) if target == "namespace" else (
                ("kube_pvcs",) if target == "pvc" else tuple("kube_" + key for key in ("deployments", "statefulsets", "daemonsets", "jobs", "cronjobs"))
                if target == "workload" else ("arm_related_inventory",))
            result = outcome("not_applicable" if source_complete(raw, *sources) else "not_evaluated",
                             "No hay objetos aplicables en inventario completo." if source_complete(raw, *sources)
                             else "Inventario no disponible/completo; no se inventan objetos, hallazgos o salud.", source_ids=list(sources))
            checks.append(assessment_check(rule, {}, result, raw, reference))
            continue
        for row in records:
            result = evaluate_cluster(rule, row, raw) if target in ("cluster", "node_pool") else (
                evaluate_workload(rule, row, raw) if target == "workload" else evaluate_namespace(rule, row, raw)
                if target == "namespace" else evaluate_resource(rule, row, raw))
            checks.append(assessment_check(rule, row, result, raw, reference))
    for item in CATALOG["checklist_items"]:
        if item["id"] in mapped:
            continue
        pillar = {"Reliability": "reliability", "Security": "security", "Cost": "cost", "Operations": "operations",
                  "Performance": "performance"}[item["waf"]]
        rule = {"rule_id": "M" + item["id"], "checklist_id": item["id"], "level": "cluster",
                "area": {"security": "security", "cost": "finops", "performance": "optimization"}.get(pillar, "best_practices"),
                "pillar": pillar, "severity": str(item["severity"]).lower() if str(item["severity"]).lower() in SEVERITIES else "low",
                "effort": "medium", "title": "Revision de arquitectura: " + item["text"],
                "recommendation": "Validar aplicabilidad y evidencia documental con el propietario y la guia actual; no ejecutar snippets legacy.",
                "business_impact": "El riesgo depende de requisitos y evidencia aun no recogidos; no se presume incumplimiento."}
        result = outcome("not_evaluated", "Item de checklist no automatizado por este lector: requisitos/IaC/DR/pruebas u otra fuente faltan.",
                         {"checklist_id": item["id"], "upstream_text": item["text"]}, ["reference_catalog"])
        checks.append(assessment_check(rule, raw["cluster"], result, raw, reference))
    if len({check["check_id"] for check in checks}) != len(checks):
        raise EvidenceError("duplicate_check", "Rule/resource check IDs are not unique; no duplicated score denominator.", "evaluator")
    return checks


def score(checks):
    if len({row["check_id"] for row in checks}) != len(checks):
        raise EvidenceError("duplicate_check", "Duplicate check IDs cannot be scored.", "evaluator")
    counts = Counter(row["status"] for row in checks)
    if set(counts) - set(CHECK_STATUSES):
        raise EvidenceError("invalid_check_status", "Unknown check status cannot be scored.", "evaluator")
    evaluated = counts["passed"] + counts["failed"]
    eligible = evaluated + counts["not_evaluated"]
    return {
                "score": round(100 * counts["passed"] / evaluated, 2) if evaluated else None,
                "passed": counts["passed"], "failed": counts["failed"], "evaluated": evaluated,
                "not_evaluated": counts["not_evaluated"], "not_applicable": counts["not_applicable"], "total": len(checks),
                "coverage_percent": round(100 * evaluated / eligible, 2) if eligible else None,
    }


def actions_for(findings):
    return [
                {"action_id": "ACT-" + check["check_id"], "check_id": check["check_id"], "title": check["title"],
                 "cluster_id": check["cluster_id"], "cluster_name": check["cluster_name"],
                 "resource_id": check["resource_id"], "namespace": check["namespace"], "workload_name": check["workload_name"],
                 "area": check["area"], "pillar": check["pillar"], "severity": check["severity"], "status": "failed",
                 "level": check["level"], "resource_name": check["resource_name"],
                 "effort": check["effort"], "planning_bucket": {"low": "quick_win", "medium": "short_term", "high": "medium_term"}[check["effort"]],
                 "planning_note": "Bucket orientativo: <1 semana / 1-3 meses / medio plazo; no es una medicion ni compromiso de entrega.",
                 "recommendation": check["recommendation"], "learn_url": check["learn_url"], "owner": None}
                for check in sorted(findings, key=lambda row: (SEVERITIES.index(row["severity"]), row["check_id"]))
    ]


def finops_for(raw):
    inventory = raw["inventory"]
    metrics = [{**row, "basis": "metrics.k8s.io current snapshot, not 30-day utilization", "metric_kind": collection}
                       for collection in ("node_metrics", "pod_metrics") for row in inventory.get(collection, [])]
    candidates = []
    nodes = {node["name"]: node for node in inventory.get("nodes", [])}
    for metric in inventory.get("node_metrics", []):
        node = nodes.get(metric["name"])
        capacity, usage = pick(node, "allocatable", "cpu"), metric.get("cpu_cores")
        if capacity is not None and capacity > 0 and usage is not None and metric.get("fresh_for_reference") is True:
            ratio = usage / capacity
            if ratio < .2:
                candidates.append({
                    "id": "CURRENT-NODE-" + metric["name"], "title": "Revisar capacidad con una muestra actual, no infrautilizacion mensual",
                    "resource_id": metric["resource_id"], "source_id": metric["source_id"], "basis": "Local review threshold <20% current CPU/allocatable, no sustained-use claim",
                    "value": round(100 * ratio, 6), "unit": "percent", "numerator": usage, "numerator_unit": "cores",
                    "denominator": capacity, "denominator_unit": "allocatable_cores", "sample_count": 1,
                    "window": {"timestamp": metric["timestamp"], "duration": metric["window"]},
                    "recommendation": "Obtener historial, picos, memoria y requisitos antes de right-sizing; no cambiar capacidad.",
                    "estimated_savings": None,
                })
    owners = {row["uid"]: row for row in inventory.get("workloads", []) + inventory.get("replicasets", [])}
    workloads = {row["uid"]: row for row in inventory.get("workloads", [])}
    def workload_owner(pod):
        current, seen = pod, set()
        for _ in range(4):
            controllers = [ref for ref in current.get("owner_references", []) if ref.get("controller") is True]
            if len(controllers) != 1:
                return None
            uid = controllers[0].get("uid")
            if uid in seen:
                return None
            if uid in workloads:
                return workloads[uid]
            seen.add(uid)
            current = owners.get(uid)
            if current is None:
                return None
        return None
    pod_metrics = {(row.get("namespace"), row["name"]): row for row in inventory.get("pod_metrics", [])}
    joined = {}
    for pod in inventory.get("pods", []):
        workload, metric = workload_owner(pod), pod_metrics.get((pod.get("namespace"), pod["name"]))
        if workload is None or metric is None or metric.get("fresh_for_reference") is not True:
            continue
        measurements = {c["name"]: c for c in metric.get("containers", [])}
        for container in pod.get("containers", []):
            measured = measurements.get(container["name"])
            request = container.get("request_cpu_cores")
            if request is not None and measured is not None and measured.get("cpu_cores") is not None:
                group = joined.setdefault(workload["uid"], {"workload": workload, "requests": [], "usage": [], "windows": [], "samples": 0})
                group["requests"].append(request)
                group["usage"].append(measured["cpu_cores"])
                group["windows"].append({"timestamp": metric["timestamp"], "duration": metric["window"]})
                group["samples"] += 1
    for group in joined.values():
        requests, usage, workload = sum(group["requests"]), sum(group["usage"]), group["workload"]
        if requests > 0 and usage / requests < .5:
            candidates.append({
                "id": "CURRENT-WORKLOAD-" + workload["uid"], "title": "Revisar requests con muestras actuales de contenedores unidos por owner UID",
                "resource_id": workload["resource_id"], "namespace": workload.get("namespace"), "workload_name": workload["name"],
                "source_id": "kube_pod_metrics", "basis": "Current measured containers / same containers' configured requests; not historical or billing allocation",
                "value": round(100 * usage / requests, 6), "unit": "percent", "numerator": usage, "numerator_unit": "cores",
                "denominator": requests, "denominator_unit": "requested_cores", "sample_count": group["samples"], "window": group["windows"],
                "recommendation": "Revisar cobertura y datos historicos antes de cambiar requests; no garantiza right-sizing ni ahorro.",
                "estimated_savings": None,
            })
    complete_refs = source_complete(raw, "arm_related_inventory", "kube_pvs", "kube_services")
    all_details = all(source["status"] == "complete" for source in raw["sources"] if source["id"].startswith("arm_resource_"))
    pv_refs = {row.get("csi_volume_handle", "").lower() for row in inventory.get("pvs", []) if row.get("csi_volume_handle")}
    public_refs = {frontend["public_ip_id"].lower() for row in inventory.get("related_resources", []) for frontend in row.get("frontends", [])
                           if frontend.get("public_ip_id")}
    public_refs |= {value.lower() for row in inventory.get("related_resources", []) for value in row.get("public_ip_ids", [])}
    for resource in inventory.get("related_resources", []):
        if not complete_refs or not all_details or pick(resource, "association", "type") != "operator_scoped":
            continue
        disk = resource.get("resource_kind") == "microsoft.compute/disks"
        pip = resource.get("resource_kind") == "microsoft.network/publicipaddresses"
        unattached = (disk and pick(resource, "configuration", "diskState") == "Unattached"
                      and resource.get("managed_by_present") is True and not resource.get("managed_by")
                      and not resource.get("managed_by_extended") and resource["resource_id"].lower() not in pv_refs)
        unattached |= (pip and resource.get("ip_configuration_present") is True and resource.get("ip_configuration_id") is None
                       and resource.get("nat_gateway_present") is True and resource.get("nat_gateway_id") is None
                       and resource["resource_id"].lower() not in public_refs)
        if unattached:
            candidates.append({"id": "UNATTACHED-" + hashlib.sha256(resource["resource_id"].encode()).hexdigest()[:12],
                               "title": "Revisar activo aprobado sin attachment confirmado",
                               "resource_id": resource["resource_id"], "source_id": resource["source_id"],
                               "basis": "Explicit attachment fields + complete collected PV/LB/NIC scope; not cluster ownership by nodeRG name",
                               "value": "unattached", "unit": "attachment_state", "sample_count": 1, "window": None,
                               "recommendation": "Confirmar retencion, propietario y coste antes de decidir; no borrar ni prometer ahorro.",
                               "estimated_savings": None})
    cost_rows = raw.get("cost_rows", [])
    currencies = {row["currency"] for row in cost_rows}
    cost_source = next((source for source in raw["sources"] if source["id"] == "azure_cost"), None)
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    total = float(sum((Decimal(str(row["amount"])) for row in cost_rows), Decimal(0))) if currency and pick(cost_source, "status") == "complete" else None
    by_currency = {code: float(sum((Decimal(str(row["amount"])) for row in cost_rows if row["currency"] == code), Decimal(0)))
                   for code in sorted(currencies)}
    return {
                "sources": ["azure_cost", "arm_advisor", "kube_node_metrics", "kube_pod_metrics"],
                "window": pick(cost_source, "window"), "currency": currency, "actual_cost": total, "cost_rows": cost_rows,
                "metrics": metrics, "candidates": candidates, "estimated_savings": None,
                "observed_cost_totals_by_currency": by_currency, "cost_source_complete": pick(cost_source, "status") == "complete",
                "notes": [
                    "Requests/capacity are configuration, not usage. Current snapshots are not monthly utilization.",
                    "ActualCost uses observed approved ResourceIds and actual currency; it is not namespace/workload billing.",
                    "Billing latency, current partial day and allocation visibility remain limitations even with complete query paging.",
                    "No computed reservations, savings-plan/Spot savings, rightsizing promise or deletion recommendation.",
                    "Incomplete/denied/unconfigured sources remain No evaluado; different currencies are never added together.",
                ],
    }


def architecture_for(raw, reference, provenance):
    cluster, inventory = raw["cluster"], raw["inventory"]
    scope = {"cluster_id": cluster["id"], "cluster_name": cluster["name"],
             "provenance": provenance, "observed_at": reference}
    nodes, edges, unknowns, arm_nodes, kube_nodes = {}, {}, [], {}, {}
    def unknown(code, kind, message, source_ids, resource_id=None):
        unknowns.append({**scope, "code": code, "kind": kind, "message": message, "source_ids": source_ids,
                         "resource_id": resource_id or cluster["id"], "status": "not_evaluated"})
    def add_node(row, kind, evidence, native_id=None):
        value = native_id or row.get("uid") or row.get("id")
        key = "node-" + hashlib.sha256((cluster["id"].lower() + "|" + str(value).lower()).encode("utf-8")).hexdigest()[:24]
        node = {**scope, "id": key, "resource_id": native_id or row.get("resource_id") or cluster["id"],
                "kind": kind, "label": row.get("name") or str(value), "source_id": row["source_id"], "evidence": evidence}
        if row.get("namespace"):
            node["namespace"] = row["namespace"]
        if row.get("workload_name"):
            node["workload_name"] = row["workload_name"]
        if key in nodes and nodes[key] != node:
            raise EvidenceError("duplicate_architecture_node", "Observed graph IDs collide; no misleading merged node.", "architecture")
        nodes[key] = node
        if native_id or row.get("id"):
            arm_nodes[str(native_id or row["id"]).lower()] = key
        if row.get("uid"):
            kube_nodes[row["uid"]] = key
        return key
    def link(start, end, relation, source_ids, evidence):
        if not start or not end:
            return
        key = "edge-" + hashlib.sha256((start + "|" + relation + "|" + end).encode("utf-8")).hexdigest()[:24]
        edges.setdefault(key, {**scope, "id": key, "from": start, "to": end, "relation": relation,
                               "source_ids": list(dict.fromkeys(source_ids)), "evidence": evidence})
    def arm_link(row, value, relation, field):
        if value:
            link(arm_nodes.get(row["id"].lower()), arm_nodes.get(value.lower()), relation,
                 [row["source_id"]], {"field": field, "value": value})

    root = add_node(cluster, "managed_cluster", {
        "arm_id": cluster["id"], "power_state": cluster.get("power_state"), "provisioning_state": cluster.get("provisioning_state"),
        "identity": cluster.get("identity"), "kubelet_identity_resource_id": cluster.get("kubelet_identity_resource_id")})
    pools = {row["name"]: row for row in inventory.get("node_pools", [])}
    for row in pools.values():
        key = add_node(row, "agent_pool", {"arm_id": row["id"], "mode": row.get("mode"), "vm_size": row.get("vm_size"),
                                         "count": row.get("count"), "provisioning_state": row.get("provisioning_state")})
        link(root, key, "contains_pool", [row["source_id"]], {"agent_pool_id": row["id"]})
    resource_kinds = {
        "microsoft.compute/virtualmachinescalesets": "virtual_machine_scale_set", "microsoft.compute/disks": "managed_disk",
        "microsoft.network/virtualnetworks": "virtual_network", "microsoft.network/virtualnetworks/subnets": "subnet",
        "microsoft.network/networksecuritygroups": "network_security_group", "microsoft.network/loadbalancers": "azure_load_balancer",
        "microsoft.network/publicipaddresses": "public_ip_address", "microsoft.network/networkinterfaces": "network_interface",
        "microsoft.storage/storageaccounts": "storage_account", "microsoft.containerregistry/registries": "container_registry",
        "microsoft.keyvault/vaults": "key_vault", "microsoft.managedidentity/userassignedidentities": "managed_identity",
        "microsoft.network/privatednszones": "private_dns_zone", "microsoft.network/dnszones": "dns_zone",
    }
    resources = {row["id"].lower(): row for row in inventory.get("related_resources", [])}
    for row in resources.values():
        add_node(row, resource_kinds[row["resource_kind"]], {
            "arm_id": row["id"], "association": row["association"], "configuration": row.get("configuration", {})})
    for row in resources.values():
        parent = row.get("association", {}).get("resource_id")
        if parent:
            link(arm_nodes.get(parent.lower()), arm_nodes[row["id"].lower()], "references_resource",
                 [row["association"]["source_id"], row["source_id"]],
                 {"field": row["association"].get("field"), "referenced_resource_id": row["id"]})
        for field, relation in (("subnet_ids", "uses_subnet"), ("nsg_ids", "associated_nsg"), ("public_ip_ids", "references_public_ip")):
            for value in row.get(field, []):
                arm_link(row, value, relation, field)
        arm_link(row, row.get("nsg_id"), "associated_nsg", "nsg_id")
        if row["resource_kind"] == "microsoft.network/virtualnetworks/subnets":
            parent = re.split(r"/subnets/", row["id"], flags=re.IGNORECASE)[0]
            link(arm_nodes.get(parent.lower()), arm_nodes[row["id"].lower()], "contains_subnet", [row["source_id"]],
                 {"subnet_id": row["id"], "parent_virtual_network_id": parent})
        if row["resource_kind"] == "microsoft.compute/virtualmachinescalesets" and row.get("pool_tag") in pools:
            pool = pools[row["pool_tag"]]
            link(arm_nodes[pool["id"].lower()], arm_nodes[row["id"].lower()], "pool_vmss_tag_association",
                 [pool["source_id"], row["source_id"]], {"aks-managed-poolName": row["pool_tag"],
                 "aks-managed-cluster-name": row.get("cluster_tag"), "scope_association": row["association"]})
    for pool in pools.values():
        for field in ("vnet_subnet_id", "pod_subnet_id"):
            arm_link(pool, pool.get(field), "uses_subnet", field)
    for field in ("private_dns_zone", "kubelet_identity_resource_id"):
        arm_link(cluster, cluster.get(field), "configured_resource_reference", field)
    for value in cluster.get("effective_outbound_ip_ids", []):
        arm_link(cluster, value, "configured_outbound_ip", "effective_outbound_ip_ids")
    for value in cluster.get("identity", {}).get("user_assigned_resource_ids", []):
        arm_link(cluster, value, "configured_identity", "identity.user_assigned_resource_ids")

    frontends = []
    for lb in (row for row in resources.values() if row["resource_kind"] == "microsoft.network/loadbalancers"):
        for group, kind, relation in (("frontends", "load_balancer_frontend", "has_frontend"),
                                      ("backend_pools", "load_balancer_backend_pool", "has_backend_pool"),
                                      ("probes", "load_balancer_probe", "has_probe"),
                                      ("load_balancing_rules", "load_balancer_rule", "has_rule")):
            for item in lb.get(group, []):
                if not item.get("id"):
                    unknown("missing_arm_child_id", kind, "ARM child has no ID; no child resource or relationship is invented.", [lb["source_id"]], lb["id"])
                    continue
                actual_kind = kind
                if group == "frontends":
                    actual_kind += "_public" if item.get("public_ip_id") else "_private" if item.get("private_ip") else "_unknown"
                row = {**lb, "name": item.get("name") or item["id"]}
                key = add_node(row, actual_kind, item, item["id"])
                link(arm_nodes[lb["id"].lower()], key, relation, [lb["source_id"]], {"arm_child_id": item["id"]})
                if group == "frontends":
                    frontends.append((lb, item, key))
        for frontend in lb.get("frontends", []):
            if not frontend.get("id"):
                continue
            for field, relation in (("public_ip_id", "references_public_ip"), ("subnet_id", "uses_subnet")):
                value = frontend.get(field)
                if value:
                    link(arm_nodes.get(frontend["id"].lower()), arm_nodes.get(value.lower()), relation, [lb["source_id"]],
                         {"field": field, "value": value})
        for rule in lb.get("load_balancing_rules", []):
            if not rule.get("id"):
                continue
            for field, relation in (("frontend_id", "uses_frontend"), ("probe_id", "uses_probe"), ("backend_pool_id", "uses_backend_pool")):
                value = rule.get(field)
                if value:
                    link(arm_nodes.get(rule["id"].lower()), arm_nodes.get(value.lower()), relation, [lb["source_id"]],
                         {"field": field, "value": value})
            for value in rule.get("backend_pool_ids", []):
                link(arm_nodes.get(rule["id"].lower()), arm_nodes.get(value.lower()), "uses_backend_pool", [lb["source_id"]],
                     {"field": "backend_pool_ids", "value": value})
    for row in resources.values():
        for value in row.get("load_balancer_backend_pool_ids", []):
            arm_link(row, value, "uses_backend_pool", "load_balancer_backend_pool_ids")

    kinds = {"namespaces": "namespace", "nodes": "node", "workloads": "workload", "pods": "pod",
             "replicasets": "replica_set", "services": "service", "pvcs": "persistent_volume_claim",
             "pvs": "persistent_volume", "storage_classes": "storage_class"}
    kube_rows = []
    for collection, kind in kinds.items():
        for row in inventory.get(collection, []):
            evidence = {"uid": row["uid"], "kind": row.get("kind"), "name": row["name"], "namespace": row.get("namespace"),
                        "resource_version": row.get("resource_version")}
            if collection == "services":
                evidence.update({field: row.get(field) for field in ("service_type", "annotations", "load_balancer_ingress", "ports")})
            if collection == "pvs":
                evidence.update({field: row.get(field) for field in ("csi_driver", "csi_volume_handle", "csi_volume_attributes", "claim_ref")})
            add_node(row, kind, evidence)
            kube_rows.append(row)
    namespaces = {row["name"]: row for row in inventory.get("namespaces", [])}
    by_uid = {row["uid"]: row for row in kube_rows}
    pvcs = {(row.get("namespace"), row["name"]): row for row in inventory.get("pvcs", [])}
    pvs = {row["name"]: row for row in inventory.get("pvs", [])}
    classes = {row["name"]: row for row in inventory.get("storage_classes", [])}
    for row in kube_rows:
        if row.get("namespace") in namespaces:
            namespace = namespaces[row["namespace"]]
            link(kube_nodes[namespace["uid"]], kube_nodes[row["uid"]], "contains_object",
                 [namespace["source_id"], row["source_id"]], {"namespace": row["namespace"]})
        for owner in row.get("owner_references", []):
            parent = by_uid.get(owner.get("uid"))
            if parent and parent.get("namespace") == row.get("namespace") and (
                parent["name"] == owner.get("name") and parent.get("kind") == owner.get("kind")
            ):
                link(kube_nodes[parent["uid"]], kube_nodes[row["uid"]], "owner_reference",
                     [parent["source_id"], row["source_id"]], {"owner_reference": owner})
        for name in row.get("pvc_names", []):
            pvc = pvcs.get((row.get("namespace"), name))
            if pvc:
                link(kube_nodes[row["uid"]], kube_nodes[pvc["uid"]], "references_pvc", [row["source_id"], pvc["source_id"]],
                     {"namespace": row.get("namespace"), "claim_name": name})
    for namespace in namespaces.values():
        link(root, kube_nodes[namespace["uid"]], "contains_namespace", [namespace["source_id"]], {"namespace_uid": namespace["uid"]})
    for node in inventory.get("nodes", []):
        pool = pools.get(node.get("pool_name"))
        if pool:
            link(arm_nodes[pool["id"].lower()], kube_nodes[node["uid"]], "node_pool_label",
                 [pool["source_id"], node["source_id"]], {"agentpool_label": node["pool_name"]})
        provider = node.get("provider_id")
        if provider and provider.lower().startswith("azure:///subscriptions/"):
            value = provider[len("azure://"):]
            parent = re.split(r"/virtualmachines/", value, flags=re.IGNORECASE)[0]
            link(kube_nodes[node["uid"]], arm_nodes.get(parent.lower()), "backed_by_vmss", [node["source_id"]],
                 {"provider_id": provider})
    by_node_name = {row["name"]: row for row in inventory.get("nodes", [])}
    for pod in inventory.get("pods", []):
        node = by_node_name.get(pod.get("node_name"))
        if node:
            link(kube_nodes[pod["uid"]], kube_nodes[node["uid"]], "scheduled_on", [pod["source_id"], node["source_id"]],
                 {"node_name": pod["node_name"]})
    for service in inventory.get("services", []):
        for workload in inventory.get("workloads", []):
            if workload.get("namespace") == service.get("namespace") and service.get("selector") and (
                selector_matches(service["selector"], workload.get("template_labels")) is True
            ):
                link(kube_nodes[service["uid"]], kube_nodes[workload["uid"]], "selects_workload",
                     [service["source_id"], workload["source_id"]], {"selector": service["selector"], "template_labels": workload.get("template_labels")})
        if service.get("service_type") != "LoadBalancer":
            continue
        matched = False
        for lb, frontend, key in frontends:
            pip = resources.get((frontend.get("public_ip_id") or "").lower())
            address = pick(pip, "configuration", "ipAddress") if frontend.get("public_ip_id") else frontend.get("private_ip")
            if not address or not any(item.get("ip") == address for item in service.get("load_balancer_ingress", [])):
                continue
            internal = service.get("internal_load_balancer")
            if (internal == "true" and frontend.get("public_ip_id")) or (internal == "false" and not frontend.get("public_ip_id")):
                unknown("frontend_annotation_conflict", "service", "Service annotation conflicts with the observed frontend type; no ELB/ILB join.",
                        [service["source_id"], lb["source_id"]], service["resource_id"])
                continue
            matched = True
            link(kube_nodes[service["uid"]], key, "observed_load_balancer_frontend",
                 [service["source_id"], lb["source_id"]] + ([pip["source_id"]] if pip else []),
                 {"service_ingress_ip": address, "frontend_id": frontend["id"], "public_ip_id": frontend.get("public_ip_id"),
                  "internal_annotation": internal, "reachability_tested": False})
        if not matched:
            unknown("load_balancer_frontend_not_joined", "service",
                    "No observed ARM frontend/public-IP address matched Service status; a pending/configured Service is not an observed ELB/ILB.",
                    [service["source_id"], "arm_related_inventory"], service["resource_id"])
    for pvc in pvcs.values():
        pv = pvs.get(pvc.get("volume_name"))
        claim = pv.get("claim_ref", {}) if pv else {}
        if pv and claim.get("namespace") == pvc.get("namespace") and claim.get("name") == pvc["name"] and claim.get("uid") in (None, pvc["uid"]):
            link(kube_nodes[pvc["uid"]], kube_nodes[pv["uid"]], "bound_volume_reference", [pvc["source_id"], pv["source_id"]],
                 {"volume_name": pvc["volume_name"], "claim_ref": claim, "mount_tested": False})
        storage_class = classes.get(pvc.get("storage_class"))
        if storage_class:
            link(kube_nodes[pvc["uid"]], kube_nodes[storage_class["uid"]], "storage_class_reference",
                 [pvc["source_id"], storage_class["source_id"]], {"storage_class": pvc["storage_class"]})
    for pv in pvs.values():
        handle = pv.get("csi_volume_handle")
        if pv.get("csi_driver") == "disk.csi.azure.com" and handle:
            link(kube_nodes[pv["uid"]], arm_nodes.get(handle.lower()), "csi_disk_reference", [pv["source_id"]],
                 {"csi_driver": pv["csi_driver"], "volume_handle": handle, "mount_tested": False})
        if pv.get("csi_driver") == "file.csi.azure.com":
            attributes = pv.get("csi_volume_attributes", {})
            for account in resources.values():
                parts = account["id"].split("/")
                if account["resource_kind"] == "microsoft.storage/storageaccounts" and (
                    account["name"] == attributes.get("storageAccount") and parts[4] == attributes.get("resourceGroup")
                ):
                    link(kube_nodes[pv["uid"]], arm_nodes[account["id"].lower()], "csi_storage_account_reference",
                         [pv["source_id"], account["source_id"]], {"csi_volume_attributes": attributes, "mount_tested": False})
            unknown("azure_files_mount_not_validated", "persistent_volume",
                    "CSI reference is configuration; share data, keys and mount/read success are not collected.", [pv["source_id"]], pv["resource_id"])
    registries = [row for row in resources.values() if row["resource_kind"] == "microsoft.containerregistry/registries" and row.get("login_server")]
    for workload in inventory.get("workloads", []):
        for registry in registries:
            images = [container["image"] for container in workload.get("containers", []) + workload.get("init_containers", [])
                      if container.get("image") and container["image"].split("/", 1)[0].lower() == registry["login_server"].lower()]
            if images:
                link(kube_nodes[workload["uid"]], arm_nodes[registry["id"].lower()], "references_registry_image",
                     [workload["source_id"], registry["source_id"]], {"images": images, "arm_login_server": registry["login_server"],
                     "pull_permission_verified": False, "image_pull_tested": False})
    for role in raw.get("registry_role_assignments", []):
        identity = next((row for row in resources.values() if row.get("principal_id") == role["principal_id"]), None)
        start = arm_nodes[identity["id"].lower()] if identity else root
        link(start, arm_nodes.get(role["resource_id"].lower()), "kubelet_registry_role_assignment",
             ["arm_cluster", role["source_id"]] + ([identity["source_id"]] if identity else []),
             {"role_assignment_id": role["id"], "principal_id": role["principal_id"], "role_scope": role["scope"],
              "role_definition_id": role["role_definition_id"], "condition_present": role["condition_present"],
              "effective_pull_permission_verified": False})
    for registry in registries:
        unknown("effective_registry_pull_not_validated", "container_registry",
                "Observed loginServer/image and optional kubelet role metadata are references, not effective pull permission or image-pull success.",
                [registry["source_id"]], registry["id"])
    if pick(cluster, "addons", "azureKeyvaultSecretsProvider", "enabled") is True:
        unknown("key_vault_mount_not_observed", "key_vault",
                "The addon is configured; no SecretProviderClass, vault mount or secret value is read or inferred.", ["arm_cluster"])
    if cluster.get("power_state") == "Stopped":
        unknown("stopped_cluster_manifests_unavailable", "source",
                "The cluster is observed Stopped. Kubernetes manifests are not collected or inferred empty/healthy, and the collector never starts it.",
                ["arm_cluster", "kube_access"])
    for source in raw["sources"]:
        if source["status"] not in ("complete", "not_applicable") and (
            source["id"].startswith(("kube_", "arm_resource_", "arm_registry_roles_")) or source["id"] in ("arm_cluster", "arm_node_pools", "arm_related_inventory")
        ):
            unknown("architecture_source_incomplete", "source", source["id"] + ": " + source["status"] + ". " + source["note"],
                    [source["id"]], source.get("resource_id"))
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "unknowns": unknowns}


def multi_snapshot(runs, reference, roster):
    if not 1 <= len(runs) <= 5 or len({run["cluster_id"].lower() for run in runs}) != len(runs):
        raise EvidenceError("invalid_cluster_runs", "Multi-cluster runs require distinct fixed cluster IDs.", "assessment")
    if any(run["status"] not in ("ok", "partial", "error") or (
        (run["snapshot"] is None) != (run["status"] == "error")
    ) or (run["snapshot"] is None and not isinstance(run["error"], dict)) or (
        run["snapshot"] is not None and run["error"] is not None) for run in runs):
        raise EvidenceError("invalid_cluster_runs", "Each run is an explicit snapshot success/partial or a structured error, never both.", "assessment")
    snapshots = [run["snapshot"] for run in runs if run["snapshot"] is not None]
    provenances = {snapshot["provenance"] for snapshot in snapshots}
    if len(provenances) > 1:
        raise EvidenceError("mixed_provenance", "LIVE and SIMULATED snapshots cannot be combined.", "assessment")
    for run in runs:
        if run["snapshot"] is not None and run["snapshot"]["cluster"]["id"].lower() != run["cluster_id"].lower():
            raise EvidenceError("scope_mismatch", "A cluster run cannot contain another cluster's snapshot.", "assessment")
    checks = [check for snapshot in snapshots for check in snapshot["checks"]]
    scores = {"global": score(checks), "areas": {area: score([row for row in checks if row["area"] == area]) for area in AREAS},
              "pillars": {pillar: score([row for row in checks if row["pillar"] == pillar]) for pillar in PILLARS}}
    scores["coverage"] = dict(scores["global"])
    return {"schema_version": "aks-waf-multi-cluster-v1", "reference_utc": reference,
            "provenance": next(iter(provenances)) if provenances else "LIVE",
            "client": {"name": CLIENT_NAME, "logo_data_uri": CLIENT_LOGO_DATA_URI, "logo_alt": CLIENT_LOGO_ALT},
            "available_clusters": roster, "selected_cluster_names": [run["cluster_name"] for run in runs], "cluster_runs": runs,
            "aggregate": {"scores": scores, "inventory_counts": {
                key: sum(len(snapshot["inventory"].get(key) or []) for snapshot in snapshots)
                for key in ("node_pools", "namespaces", "workloads", "related_resources")},
                "findings_total": sum(len(snapshot["findings"]) for snapshot in snapshots),
                "critical_total": sum(check["status"] == "failed" and check["severity"] == "critical" for check in checks),
                "cluster_status_counts": dict(Counter(run["status"] for run in runs)),
                "inventory_basis": "Observed records from successful snapshots only; failed clusters are unknown, not empty.",
                "score_basis": "Unique cluster/rule/resource checks from available snapshots only; failed clusters are not scored.",
                "failed_cluster_count": sum(run["snapshot"] is None for run in runs),
                "official_waf_score": False,
                "collection_seconds_bound": MAX_COLLECTION_SECONDS,
                "estimated_savings": None, "actual_cost": None,
                "cost_basis": "Per-cluster billing currency, time window and scope remain separate; no cross-cluster currency/scope sum."}}


def finish_snapshot(raw, reference, provenance="LIVE", environment="UNSPECIFIED"):
    if provenance not in ("LIVE", "SIMULATED") or environment not in ("LAB", "PRODUCTION", "UNSPECIFIED"):
        raise EvidenceError("invalid_provenance", "Explicit source provenance and environment are required.")
    raw = {**raw, "sources": list(raw["sources"]) + [
                source_receipt("reference_catalog", reference, records=CATALOG.get("catalogSnapshot", {}).get("item_count", 0),
                               note="Build-time pinned reference catalog, not a fresh runtime network read."),
                source_receipt("configuration_requirements", reference, "complete" if raw.get("requirements") else "not_configured",
                               records=len(raw.get("requirements", {})), note="Explicit operator intent only; unknown requirements are not inferred.")
    ]}
    checks = evaluate(raw, reference)
    findings = [check for check in checks if check["status"] == "failed"]
    global_score = score(checks)
    scores = {"global": global_score, "coverage": dict(global_score),
                      "areas": {area: score([check for check in checks if check["area"] == area]) for area in AREAS},
                      "pillars": {pillar: score([check for check in checks if check["pillar"] == pillar]) for pillar in PILLARS}}
    blind_spots = [source["id"] for source in raw["sources"] if source["status"] not in ("complete", "not_applicable")]
    critical = sum(check["severity"] == "critical" for check in findings)
    mapped = {rule["checklist_id"] for rule in CATALOG["rules"]}
    catalog_count = CATALOG["catalogSnapshot"]["item_count"]
    return {
                "schema_version": "1.0", "provenance": provenance, "environment": environment, "reference_utc": reference,
                "client": {"name": CLIENT_NAME, "logo_data_uri": CLIENT_LOGO_DATA_URI, "logo_alt": CLIENT_LOGO_ALT},
                "cluster": raw["cluster"], "sources": raw["sources"], "inventory": raw["inventory"],
                "architecture": architecture_for(raw, reference, provenance),
                "source_data": {key: raw.get(key) for key in ("regional_versions", "upgrade_profile", "arm_maintenance", "arm_diagnostics", "advisor", "cost_rows", "registry_role_assignments")},
                "checks": checks, "findings": findings, "scores": scores, "finops": finops_for(raw), "actions": actions_for(findings),
                "executive_messages": [
                    str(len(findings)) + " hallazgos de configuracion observados; " + str(critical) + " criticos reales, sin completar un Top 5 ficticio.",
                    str(global_score["evaluated"]) + " de " + str(global_score["evaluated"] + global_score["not_evaluated"]) + " indicadores aplicables evaluados; cobertura separada de puntuacion.",
                    str(global_score["not_evaluated"]) + " indicadores No evaluado y " + str(global_score["not_applicable"]) + " No aplica; no equivalen a salud.",
                    str(len(blind_spots)) + " fuentes con acceso, configuracion o cobertura incompletos; revisar las limitaciones antes de priorizar.",
                    "Ahorro estimado no calculado; " + str(len(raw.get("cost_rows", []))) + " filas de coste real y "
                    + str(len(raw["inventory"].get("node_metrics", [])) + len(raw["inventory"].get("pod_metrics", []))) + " muestras actuales conservadas.",
                ],
                "methodology": {
                    "score_kind": "local_assessment_indicator", "official_waf_score": False, "read_only": True,
                    "score_formula": "100 * passed / (passed + failed), unique cluster/rule/resource check_id. No evaluated checks => null.",
                    "coverage_formula": "100 * evaluated / (evaluated + not_evaluated). N/A excluded. Mapping IDs may repeat across distinct local indicators.",
                    "catalogSnapshot": CATALOG["catalogSnapshot"], "catalog_coverage": {"total_items": catalog_count, "mapped_items": len(mapped),
                        "unmapped_items": catalog_count - len(mapped), "local_rules": len(CATALOG["rules"])},
                    "source_pins": [CATALOG["catalogSnapshot"], {"name": "AKS Well-Architected service guide", **CATALOG["guide"]}],
                    "assessment_url": CATALOG["assessment_url"], "interpretation": CATALOG["interpretation"],
                    "scope": {"cluster_id": raw["cluster"]["id"], "cluster_name": raw["cluster"]["name"], "environment": environment,
                              "requirements": raw.get("requirements", {})},
                    "queries": [{"source_id": source["id"], "command": source.get("read_signature", "Fixed read-only source; see source receipt and README"),
                                 "description": source["note"], "read_limit": {"pages": MAX_PAGES, "rows": MAX_ROWS, "response_bytes": HTTP_BYTES,
                                     "collection_seconds": MAX_COLLECTION_SECONDS, "http_attempts": MAX_HTTP_ATTEMPTS}}
                                for source in raw["sources"] if source["id"] not in ("reference_catalog", "configuration_requirements")],
                    "limitations": [
                        "Point-in-time ARM/configuration assessment and independent per-kind Kubernetes list snapshots, not whole-cluster availability.",
                        "No IaC, DR test, RTO/RPO, license or policy-enforcement evidence is inferred from missing sources or addon flags.",
                        "Kubernetes owner/selector joins require observed fields and namespace; metrics use timestamp/window/sample counts.",
                        "Quick-win/short/medium buckets are advisory planning categories, not measured durations or guaranteed savings.",
                        "This is not an official Microsoft assessment, certification, Polaris or WARA score.",
                    ],
                },
    }
