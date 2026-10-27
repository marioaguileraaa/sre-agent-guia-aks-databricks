

# ----------------------------------------------------------------------------- spec validation
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
CRON_RE = re.compile(r"^(\S+\s+){4}\S+$")
KNOWN_TOP = set(SPEC_FIELDS) - {"condition"}


def _is_placeholder(value):
    return isinstance(value, str) and bool(re.search(r"<[^<>]{2,80}>", value))


def _walk_placeholders(obj, path=""):
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key != "sample_rows":
                found += _walk_placeholders(value, f"{path}.{key}" if path else key)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found += _walk_placeholders(value, f"{path}[{index}]")
    elif _is_placeholder(obj):
        found.append({"path": path, "value": obj})
    return found


def _conditions(cond):
    stack, out = [cond or {}], []
    while stack:
        c = stack.pop()
        if not isinstance(c, dict):
            continue
        stack += list(c.get("all", [])) + list(c.get("any", [])) + ([c["not"]] if "not" in c else [])
        if "field" in c or "op" in c:
            out.append(c)
    return out


def _check_condition(cond, where, errors):
    if cond in (None, {}):
        return
    if not isinstance(cond, dict):
        errors.append({"path": where, "error": "condition must be an object", "fix": "use {field, op, value} or {all|any: [...]}"})
        return
    for c in _conditions(cond):
        op = c.get("op", "eq")
        if op not in OPERATORS:
            errors.append({"path": where, "error": f"unknown operator '{op}'", "fix": "use one of " + ", ".join(OPERATORS)})
        if not c.get("field"):
            errors.append({"path": where, "error": "condition without field", "fix": "add the column name in field"})
        if op not in NO_VALUE_OPS and "value" not in c:
            errors.append({"path": where, "error": f"operator '{op}' needs value", "fix": OPERATORS.get(op, "")})
        if op in ("in", "not_in") and not isinstance(c.get("value"), list):
            errors.append({"path": where, "error": f"'{op}' needs a list value", "fix": 'e.g. "value": ["A", "B"]'})
        if op == "regex":
            try:
                re.compile(str(c.get("value")))
            except re.error as exc:
                errors.append({"path": where, "error": f"invalid regex: {exc}", "fix": "escape special characters"})


def _validate(spec):
    errors, warnings = [], []
    if not isinstance(spec, dict):
        return None, None, [{"path": "", "error": "spec_json must be a JSON object", "fix": "start from a recipe in operation catalog"}], []
    for key in spec:
        if key not in KNOWN_TOP:
            warnings.append(f"Unknown spec field '{key}' ignored.")
    name = str(spec.get("name", "")).strip().lower()
    if not NAME_RE.match(name):
        errors.append({"path": "name", "error": f"invalid name '{name}'", "fix": "kebab-case, 3-40 chars, e.g. vm-heartbeat-gaps"})
    title = str(spec.get("title") or name.replace("-", " ").capitalize()).replace('"""', "'").replace("\\", "/")
    description = str(spec.get("description") or "").strip()
    if not description:
        warnings.append("Add 'description' (what it detects and why it matters): it becomes the business value in skill and task.")
    language = spec.get("language", "en") if spec.get("language") in ("es", "en") else "en"
    accent = spec.get("accent") if HEX_RE.match(str(spec.get("accent", ""))) else "#0f6cbd"
    days = spec.get("lookback_days", 7)
    if not isinstance(days, int) or not 1 <= days <= 366:
        errors.append({"path": "lookback_days", "error": "must be an integer 1-366", "fix": "e.g. 7"})
        days = 7
    sources = spec.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append({"path": "sources", "error": "at least one source is required", "fix": "see catalog.source_types"})
        sources = []
    ids = []
    for i, src in enumerate(sources):
        where = f"sources[{i}]"
        if not isinstance(src, dict):
            errors.append({"path": where, "error": "source must be an object", "fix": "{id, type, ...}"})
            continue
        sid, stype = src.get("id"), src.get("type")
        if not ID_RE.match(str(sid or "")):
            errors.append({"path": where + ".id", "error": f"invalid id '{sid}'", "fix": "snake_case, e.g. heartbeat"})
        if sid in ids:
            errors.append({"path": where + ".id", "error": f"duplicate id '{sid}'", "fix": "ids must be unique"})
        ids.append(sid)
        meta = SOURCE_TYPES.get(stype)
        if not meta:
            errors.append({"path": where + ".type", "error": f"unknown type '{stype}'", "fix": "use " + ", ".join(SOURCE_TYPES)})
            continue
        for field in meta["required"]:
            if src.get(field) in (None, ""):
                errors.append({"path": f"{where}.{field}", "error": f"{stype} needs {field}", "fix": meta["notes"]})
        for group in meta.get("one_of", []):
            if not any(src.get(f) for f in group):
                errors.append({"path": where, "error": f"{stype} needs one of {group}", "fix": meta["notes"]})
        allowed = set(meta["required"]) | set(meta["optional"]) | {f for g in meta.get("one_of", []) for f in g} | {"id", "type"}
        for field in src:
            if field not in allowed:
                warnings.append(f"{where}: field '{field}' is not used by {stype}.")
        if stype == "arm" and "api-version" not in str(src.get("path", "")):
            errors.append({"path": where + ".path", "error": "ARM path without api-version", "fix": "append ?api-version=YYYY-MM-DD"})
        if stype == "graph" and not re.match(r"^/?(v1\.0|beta)/", str(src.get("path", ""))):
            errors.append({"path": where + ".path", "error": "Graph path must start with v1.0/ or beta/", "fix": "e.g. v1.0/applications?$select=..."})
        if stype in ("log_analytics", "adx", "resource_graph") and re.search(r"\b(\.drop|\.set|\.append|\.delete|\.alter)\b", str(src.get("query", ""))):
            errors.append({"path": where + ".query", "error": "control command in query", "fix": "queries must be read-only"})
        if src.get("sample_rows") is not None and not (isinstance(src["sample_rows"], list) and all(isinstance(r, dict) for r in src["sample_rows"])):
            errors.append({"path": where + ".sample_rows", "error": "sample_rows must be a list of objects", "fix": "[{\"col\": value}]"})
    datasets = set(ids)

    def ds(obj, where):
        if obj.get("dataset") not in datasets:
            errors.append({"path": where + ".dataset", "error": f"unknown dataset '{obj.get('dataset')}'", "fix": "use a source id: " + ", ".join(map(str, ids))})

    derive = spec.get("derive") or []
    for i, d in enumerate(derive):
        ds(d, f"derive[{i}]")
        if d.get("type") not in ("days_until", "days_since") or not d.get("name") or not d.get("field"):
            errors.append({"path": f"derive[{i}]", "error": "derive needs name, field and type days_until|days_since", "fix": "see recipe entra-app-secrets-expiry"})
    rules, rule_ids = spec.get("rules") or [], set()
    for i, r in enumerate(rules):
        ds(r, f"rules[{i}]")
        if not ID_RE.match(str(r.get("id", ""))) or r.get("id") in rule_ids:
            errors.append({"path": f"rules[{i}].id", "error": f"invalid or duplicate rule id '{r.get('id')}'", "fix": "unique snake_case"})
        rule_ids.add(r.get("id"))
        if r.get("severity", "medium") not in SEVERITIES:
            errors.append({"path": f"rules[{i}].severity", "error": f"severity '{r.get('severity')}'", "fix": "use " + ", ".join(SEVERITIES)})
        if not r.get("when"):
            errors.append({"path": f"rules[{i}].when", "error": "rule without condition", "fix": "add when: {field, op, value}"})
        _check_condition(r.get("when"), f"rules[{i}].when", errors)
    kpis = spec.get("kpis") or []
    for i, k in enumerate(kpis):
        ds(k, f"kpis[{i}]")
        if k.get("agg", "count") not in AGGREGATIONS:
            errors.append({"path": f"kpis[{i}].agg", "error": f"unknown agg '{k.get('agg')}'", "fix": "use " + ", ".join(AGGREGATIONS)})
        if k.get("agg", "count") != "count" and not k.get("field"):
            errors.append({"path": f"kpis[{i}].field", "error": f"agg {k.get('agg')} needs field", "fix": "the numeric or key column"})
        if k.get("good") not in (None, "up", "down"):
            errors.append({"path": f"kpis[{i}].good", "error": "good must be up or down", "fix": "down = lower is better"})
        _check_condition(k.get("where"), f"kpis[{i}].where", errors)
    tables = spec.get("tables") or []
    for i, t in enumerate(tables):
        ds(t, f"tables[{i}]")
        _check_condition(t.get("where"), f"tables[{i}].where", errors)
    keys = spec.get("keys") or {}
    for name_, fields in keys.items():
        if name_ not in datasets or not isinstance(fields, list) or not fields:
            errors.append({"path": f"keys.{name_}", "error": "keys must map a source id to a non-empty list of fields", "fix": '{"heartbeat": ["Computer"]}'})
    for sid in ids:
        if sid and sid not in keys:
            warnings.append(f"No keys for '{sid}': findings are matched by row hash, so comparison with the previous run is less precise. "
                            "Add keys (scaffold proposes them from live columns when possible).")
    publish = spec.get("publish") or {}
    if publish.get("sharepoint") and not ((publish["sharepoint"].get("site_id") or publish["sharepoint"].get("site_url")) and "folder" in publish["sharepoint"]):
        errors.append({"path": "publish.sharepoint", "error": "needs site_url (or site_id) and folder", "fix": '{"site_url": "https://contoso.sharepoint.com/sites/Ops", "folder": "Reports/X"}'})
    if publish.get("blob") and not (publish["blob"].get("account") and publish["blob"].get("container")):
        errors.append({"path": "publish.blob", "error": "needs account and container", "fix": '{"account": "stops", "container": "reports", "prefix": "x"}'})
    history = spec.get("history") or ("sharepoint" if publish.get("sharepoint") else "blob" if publish.get("blob") else "none")
    if history not in ("none", "sharepoint", "blob") or (history != "none" and not publish.get(history)):
        errors.append({"path": "history", "error": f"history '{history}' needs the same publish target", "fix": "set publish.sharepoint or publish.blob, or history none"})
    schedule = str(spec.get("schedule") or "weekly").strip()
    cron = CRON_PRESETS.get(schedule.lower(), schedule)
    if not CRON_RE.match(cron):
        errors.append({"path": "schedule", "error": f"invalid cron '{schedule}'", "fix": "5 fields (UTC) or " + ", ".join(CRON_PRESETS)})
    if not kpis:
        kpis = [{"id": f"{s}_rows", "label": f"{s}: rows", "dataset": s} for s in ids if s]
        kpis += [{"id": f"{r['id']}_count", "label": r.get("title", r["id"]), "dataset": r["dataset"], "good": "down", "where": r["when"]}
                 for r in rules if isinstance(r, dict) and r.get("when") and r.get("id")]
        warnings.append("No kpis given: added row counts per source and one count per rule.")
    if not rules:
        warnings.append("No rules: the report will show KPIs and tables but no findings.")
    if not tables:
        tables = [{"title": s, "dataset": s, "columns": list(dict.fromkeys(list(keys.get(s, [])) + [f for r in rules if r.get("dataset") == s for f in r.get("show", [])])), "limit": 50}
                  for s in ids if s]
    config = {"tool_name": f"run-{name}", "title": title, "client": str(spec.get("client", "")), "language": language, "accent": accent,
              "identity_client_id": "", "lookback_days": days, "max_rows_per_source": int(spec.get("max_rows_per_source", 50000)),
              "file_prefix": name, "sources": sources, "derive": derive, "kpis": kpis, "rules": rules, "tables": tables, "keys": keys,
              "publish": publish, "history": history}
    cadence = {v: k for k, v in CRON_PRESETS.items()}.get(cron, "scheduled")
    meta = {"name": name, "title": title, "description": description, "audience": str(spec.get("audience", "")), "cron": cron,
            "cadence": cadence, "tool": f"run-{name}", "skill": name, "agent": name, "task": f"{name}-{cadence}"}
    return config, meta, errors, warnings


# ----------------------------------------------------------------------------- code generation
def _referenced_fields(config):
    refs = {}

    def add(dataset, field, used):
        if field:
            refs.setdefault(dataset, {}).setdefault(field, set()).add(used)

    derived = {(d["dataset"], d["name"]) for d in config.get("derive", [])}
    for d in config.get("derive", []):
        add(d["dataset"], d["field"], f"derive.{d['name']}")
    for r in config.get("rules", []):
        for c in _conditions(r.get("when")):
            add(r["dataset"], c.get("field"), f"rule {r['id']}")
        for f in r.get("show", []):
            add(r["dataset"], f, f"rule {r['id']} show")
        for f in re.findall(r"\{([^{}]+)\}", r.get("message", "")):
            add(r["dataset"], f.strip(), f"rule {r['id']} message")
    for k in config.get("kpis", []):
        add(k["dataset"], k.get("field"), f"kpi {k['id']}")
        for c in _conditions(k.get("where")):
            add(k["dataset"], c.get("field"), f"kpi {k['id']}")
    for t in config.get("tables", []):
        for f in t.get("columns", []) + ([t["sort"]] if t.get("sort") else []):
            add(t["dataset"], f, f"table {t.get('title', '')}")
        for c in _conditions(t.get("where")):
            add(t["dataset"], c.get("field"), f"table {t.get('title', '')}")
    for dataset, fields in (config.get("keys") or {}).items():
        for f in fields:
            add(dataset, f, "keys")
    return {ds_: {f: sorted(u) for f, u in fields.items() if (ds_, f) not in derived} for ds_, fields in refs.items()}


def _build_code(config, meta):
    sources = config["sources"]
    publish = config.get("publish") or {}
    keep = {"source:" + s["type"] for s in sources}
    if any(s["type"] == "sharepoint_file" for s in sources) or publish.get("sharepoint"):
        keep.add("sharepoint")
    if any(s["type"] == "blob_file" for s in sources) or publish.get("blob"):
        keep.add("blob")
    for target in ("sharepoint", "blob"):
        if publish.get(target):
            keep.add("publish:" + target)
    lines, skipping = [], False
    for line in TEMPLATE.splitlines():
        block = re.match(r"^# @block (\S+)", line)
        if block:
            skipping = block.group(1) not in keep
            continue
        if line.startswith("# @endblock"):
            skipping = False
            continue
        if not skipping:
            lines.append(line)
    code = re.sub(r"\n{4,}", "\n\n\n", "\n".join(lines)) + "\n"
    literal = pprint.pformat(config, width=118, sort_dicts=False)
    code = code.replace("CONFIG = {}  # @config", "# Everything this tool does is driven by CONFIG: edit queries, thresholds and rules here.\nCONFIG = " + literal, 1)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (code.replace("__TOOL_NAME__", config["tool_name"]).replace("__TITLE__", meta["title"])
            .replace("__KIT_VERSION__", KIT_VERSION).replace("__GENERATED_UTC__", stamp))


# ----------------------------------------------------------------------------- lint
SECRET_PATTERNS = [
    (r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.", "JWT/bearer token"),
    (r"(?i)(AccountKey|SharedAccessKey)=[A-Za-z0-9+/=]{20,}", "storage/service bus key"),
    (r"(?i)[?&]sig=[A-Za-z0-9%+/=]{20,}", "SAS signature"),
    (r"(?i)(client_secret|password|passwd|api_key|apikey)\s*[=:]\s*['\"][^'\"<>]{8,}['\"]", "hard-coded secret"),
    (r"[A-Za-z0-9_~.-]{3}8Q~[A-Za-z0-9_~.-]{30,}", "Entra client secret"),
]
FORBIDDEN_MODULES = {"subprocess", "pickle", "marshal", "ctypes", "pty", "shelve"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_ATTRS = {("os", "system"), ("os", "popen"), ("os", "execv"), ("os", "execl"), ("os", "spawnl"), ("shutil", "rmtree"),
                   ("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("requests", "delete"), ("requests", "patch")}
WRITE_MARKERS = ("/permissions", "roleassignments", "approleassignments", "microsoft.authorization/locks", "/delete")


def _lint(code):
    errors, warnings, info = [], [], {"bytes": len(code.encode()), "lines": code.count("\n") + 1}
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"status": "FAIL", "errors": [f"SyntaxError line {exc.lineno}: {exc.msg}"], "warnings": [], "info": info}
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if not mains:
        errors.append("No top-level def main(...): Builder calls main.")
    else:
        args = mains[0].args
        positional = args.posonlyargs + args.args
        if len(args.defaults) != len(positional) or None in args.kw_defaults:
            errors.append("Every main() parameter needs a default so the tool can run with no arguments.")
        for a in positional + args.kwonlyargs:
            if a.annotation is None:
                warnings.append(f"main() parameter '{a.arg}' has no type annotation (str, int, float or bool).")
        info["parameters"] = [a.arg for a in positional + args.kwonlyargs]
        offset = len("\n".join(code.splitlines()[:mains[0].lineno - 1]))
        if offset > 40000:
            warnings.append(f"main() starts {offset} characters into the file: the Builder Test playground may not detect its "
                            "parameters. Define main() near the top and call the logic defined below it.")
    methods, hosts = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                if n.split(".")[0] in FORBIDDEN_MODULES:
                    errors.append(f"Forbidden import '{n}' (line {node.lineno}).")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
                errors.append(f"Forbidden call {fn.id}() (line {node.lineno}).")
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and (fn.value.id, fn.attr) in FORBIDDEN_ATTRS:
                errors.append(f"Forbidden call {fn.value.id}.{fn.attr}() (line {node.lineno}).")
            fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            candidates = [kw.value for kw in node.keywords if kw.arg == "method"]
            if fname in ("request", "_http") and node.args:
                candidates.append(node.args[0])
            for cand in candidates:
                if isinstance(cand, ast.Constant) and isinstance(cand.value, str):
                    verb = cand.value.upper()
                    if verb in ("DELETE", "PATCH"):
                        errors.append(f"HTTP {verb} call (line {node.lineno}): generated tools must not modify or delete resources.")
                    else:
                        methods.add(verb)
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "requests" and fn.attr in ("get", "post", "put"):
                methods.add(fn.attr.upper())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower()
            for marker in WRITE_MARKERS[:4]:
                if marker in low:
                    warnings.append(f"References '{marker}' (line {node.lineno}): allowed for reading, but confirm the tool never grants or changes access.")
                    break
            for host in re.findall(r"https://([a-z0-9.-]+)", low):
                hosts.add(host)
    for pattern, label in SECRET_PATTERNS:
        if re.search(pattern, code):
            errors.append(f"Possible {label} in code: use the managed identity, never secrets in code.")
    placeholders = sorted(set(re.findall(r"['\"](<[^<>'\"\n]{2,80}>)['\"]", code)))
    if placeholders:
        warnings.append("Placeholders to fill before a live run: " + ", ".join(placeholders[:12]))
    if info["bytes"] > 250_000:
        warnings.append("Code is larger than 250 KB; Builder may reject it.")
    info.update(http_methods=sorted(methods), endpoints=sorted(hosts),
                writes="PUT/POST present: confirm they are only queries (KQL/ARG/Cost) or report publishing." if methods & {"PUT", "POST"} else "none")
    return {"status": "FAIL" if errors else "PASS", "errors": list(dict.fromkeys(errors)), "warnings": warnings, "info": info}
