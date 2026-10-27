"""Local tests for sre-usecase-builder-kit: every recipe must scaffold to READY with a passing self_test."""
import copy
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("kit", ROOT / "sre-usecase-builder-kit.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)
os.environ.pop("AZURE_CLIENT_ID", None)
FILL = {"<workspace customerId>": "00000000-0000-0000-0000-000000000001", "<subscription id>": "00000000-0000-0000-0000-000000000002"}


def fill(obj):
    if isinstance(obj, dict):
        return {k: fill(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill(v) for v in obj]
    if isinstance(obj, str):
        for a, b in FILL.items():
            obj = obj.replace(a, b)
        obj = obj.replace("https://<tenant>.sharepoint.com/sites/<site>", "https://contoso.sharepoint.com/sites/ops")
        obj = obj.replace("<library folder, e.g. Shared Documents/Certificates>", "Certificates").replace("<folder for reports>", "Reports/certs")
    return obj


failures = 0
work = Path(tempfile.mkdtemp(prefix="kit-test-"))
os.chdir(work)
cat = kit.main("catalog")
assert cat["status"] == "OK" and len(cat["recipes"]) >= 6
BUDGET = 4000


def budget(label, res):
    global failures
    size = len(json.dumps(res, ensure_ascii=False, default=str))
    if size > BUDGET:
        failures += 1
        print(f"FAIL budget {label}: {size} chars")
    return size


print(f"PASS catalog index {budget('catalog', cat)} chars")
for topic in ["spec", "operators", "graph", "sharepoint_file", "all", "nope"]:
    budget("catalog " + topic, kit.main("catalog", topic))
recipes = []
for item in cat["recipes"]:
    detail = kit.main("catalog", item["name"])
    budget("catalog " + item["name"], detail)
    recipes.append({"name": item["name"], "spec": detail["spec"]})
for recipe in recipes:
    for variant, s in (("raw", copy.deepcopy(recipe["spec"])), ("filled", fill(copy.deepcopy(recipe["spec"])))):
        r = kit.main("scaffold", json.dumps(s))
        size = budget(f"scaffold {recipe['name']}", r)
        ok = r["status"] in ("READY", "READY_WITH_INPUTS")
        if variant == "filled" and r["status"] != "READY":
            ok = False
        fired = r.get("verification", {}).get("self_test", "")
        print(f"{'PASS' if ok else 'FAIL'} {recipe['name']:28s} {variant:6s} {r['status']:18s} {size:5d}ch {fired[:90]}")
        if not ok:
            failures += 1
            print(json.dumps({k: r.get(k) for k in ("errors", "stage", "self_test", "lint", "verification")}, indent=1, default=str)[:3000])
        for w in r.get("warnings", []):
            if "never fired" in w:
                print("   WARN", w)
# get: every part fits and pages reassemble the full content
name = recipes[1]["spec"]["name"]
for part in kit.GET_PARTS:
    first = kit.main("get", f"{name}/{part}")
    budget(f"get {part}", first)
    text, pages = first.get("content", ""), first.get("pages", 0)
    for p in range(2, pages + 1):
        nxt = kit.main("get", f"{name}/{part}/{p}")
        budget(f"get {part}/{p}", nxt)
        text += nxt["content"]
    ok = first["status"] == "OK" and len(text) > 20
    if part == "code":
        ok = ok and text == (work / "sre-usecase-builder" / name / f"run-{name}.py").read_text(encoding="utf-8")
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'} get {part:12s} pages={pages} chars={len(text)}")
bad_get = kit.main("get", "nope/answer")
failures += bad_get["status"] != "NOT_FOUND"
# lint negative cases
bad = {"eval": "def main(x: str = ''):\n    return eval(x)\n",
       "delete": "import requests\ndef main(x: str = ''):\n    return requests.delete('https://management.azure.com/x').status_code\n",
       "method": "import requests\ndef main(x: str = ''):\n    return requests.request('DELETE', 'https://x').status_code\n",
       "nodefault": "def main(x: str):\n    return {}\n",
       "secret": "def main(x: str = ''):\n    client_secret = 'abcdefghijklmnop'\n    return {}\n",
       "subprocess": "import subprocess\ndef main(x: str = ''):\n    return {}\n"}
for label, code in bad.items():
    res = kit.main("lint", code=code)
    ok = res["status"] == "FAIL"
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'} lint-negative {label}: {res['errors'][:1]}")
good = kit.main("lint", code="import requests\ndef main(mode: str = 'report') -> dict:\n    return {'q': \"AzureActivity | where OperationNameValue endswith '/DELETE'\"}\n")
failures += good["status"] != "PASS"
print(f"{'PASS' if good['status'] == 'PASS' else 'FAIL'} lint-positive read-only query mentioning DELETE")
inv = kit.main("scaffold", json.dumps({"name": "X", "sources": [{"id": "a", "type": "nope"}], "rules": [{"id": "r", "dataset": "b", "when": {"field": "f", "op": "zz"}}]}))
failures += inv["status"] != "INVALID"
print(f"{'PASS' if inv['status'] == 'INVALID' else 'FAIL'} invalid spec -> {len(inv.get('errors', []))} errors with fixes")
print("work dir:", work)
print("FAILURES:", failures)
sys.exit(1 if failures else 0)
