#!/usr/bin/env python3
"""Deploy the SRE use-case builder (tool + skill + subagent) to an Azure SRE Agent.

Usage:
    az login
    python deploy-to-sre-agent.py --endpoint https://<your-agent>.azuresre.ai [--subscription <id>] [--dry-run]

What it does (idempotent PUTs, safe to rerun):
    1. Python tool  sre-usecase-builder-kit   <- sre-usecase-builder-kit.py
    2. Skill        sre-usecase-builder       <- skills/sre-usecase-builder/SKILL.md
    3. Subagent     sre-usecase-builder       <- skills/subagent-instructions.md
Then it runs the kit once in the agent sandbox (operation=catalog) to prove the tool loads.

You can do the same by hand in Builder: see README.md -> "Manual setup".
"""
import argparse
import json
import pathlib
import subprocess
import sys

import requests

HERE = pathlib.Path(__file__).resolve().parent
NAME = "sre-usecase-builder"
TOOL = "sre-usecase-builder-kit"

TOOL_DESCRIPTION = (
    "Builds working Azure SRE Agent use cases. operation=catalog returns a compact index of recipes, source types and "
    "spec fields (spec_json=<recipe or topic> returns its details); environment discovers what the agent identity can read "
    "(subscriptions, Log Analytics workspaces, storage, roles); probe reads a few live rows for a spec; scaffold generates, "
    "lints, self-tests and live-checks the Python tool; get returns the generated Builder blocks, code and guides page by "
    "page; lint checks tool code. Every result fits the tool-result size limit."
)
TOOL_PARAMETERS = [
    {"name": "operation", "type": "string", "required": False, "value": None,
     "description": "catalog | environment | probe | scaffold | get | lint. Default: catalog"},
    {"name": "spec_json", "type": "string", "required": False, "value": None,
     "description": "probe/scaffold: use-case spec as JSON (start from a catalog recipe). catalog: recipe name or topic "
                    "(spec, operators, a source type). get: '<name>/<part>[/<page>]', e.g. 'vm-heartbeat-gaps/answer'."},
    {"name": "code", "type": "string", "required": False, "value": None,
     "description": "Python tool code to lint (operation=lint only)."},
]
AGENT_HANDOFF = ("Turns an operations idea into a working SRE Agent use case: tested Python tool plus Skill, Subagent "
                 "and Scheduled task blocks ready for Builder. Asks almost nothing; verifies with live data when it can.")
SKILL_DESCRIPTION = ("Turn any operations idea into a working Azure SRE Agent use case - tested Python tool plus the "
                     "Skill, Subagent and Scheduled task to paste in Builder.")


def token(subscription):
    cmd = ["az", "account", "get-access-token", "--resource", "https://azuresre.ai", "--query", "accessToken", "-o", "tsv"]
    if subscription:
        cmd += ["--subscription", subscription]
    return subprocess.check_output(cmd, text=True, shell=(sys.platform == "win32")).strip()


def put(session, endpoint, kind, name, body, dry):
    url = f"{endpoint}/api/v2/extendedAgent/{kind}/{name}"
    if dry:
        print(f"[dry-run] PUT {url} ({len(json.dumps(body))} bytes)")
        return
    r = session.put(url, json=body, timeout=120)
    if r.status_code >= 300:
        sys.exit(f"PUT {kind}/{name} failed: {r.status_code} {r.text[:500]}")
    print(f"OK  {kind}/{name}  ({r.status_code})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", required=True, help="Agent URL, e.g. https://my-agent--1234.abcd.eastus2.azuresre.ai")
    ap.add_argument("--subscription", default="", help="Subscription that hosts the SRE Agent (for the az token)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    endpoint = a.endpoint.rstrip("/")

    code = (HERE / "sre-usecase-builder-kit.py").read_text(encoding="utf-8")
    skill = (HERE / "skills" / NAME / "SKILL.md").read_text(encoding="utf-8")
    instructions = (HERE / "skills" / "subagent-instructions.md").read_text(encoding="utf-8")

    s = requests.Session()
    if not a.dry_run:
        s.headers["Authorization"] = "Bearer " + token(a.subscription)

    put(s, endpoint, "tools", TOOL, {"name": TOOL, "type": "ExtendedAgentTool", "tags": None, "properties": {
        "type": "PythonFunctionTool", "functionCode": code, "timeoutSeconds": 300, "dependencies": [],
        "authEnabled": False, "authScopes": None, "connector": None, "description": TOOL_DESCRIPTION,
        "parameters": TOOL_PARAMETERS, "attributes": [], "toolMode": "Auto"}}, a.dry_run)

    put(s, endpoint, "skills", NAME, {"name": NAME, "type": "Skill", "tags": None, "properties": {
        "description": SKILL_DESCRIPTION, "tools": [TOOL], "skillContent": skill, "additionalFiles": []}}, a.dry_run)

    put(s, endpoint, "agents", NAME, {"name": NAME, "type": "ExtendedAgent", "tags": None, "properties": {
        "instructions": instructions, "handoffDescription": AGENT_HANDOFF, "handoffs": [], "tools": [TOOL],
        "mcpTools": [], "connectors": [], "allowParallelToolCalls": False, "agentsAsTools": [],
        "maxReflectionCount": 0, "criticPromptPath": "", "criticOnHandOff": False, "customReflectionNote": "",
        "commonPrompts": [], "disableDocumentRetrieval": True, "enableHandoffPromptOverride": False,
        "userPromptOverride": None, "handoffPromptOverride": None, "instructionsOverride": None, "commonTools": [],
        "temperature": None, "llmModelName": None, "enableVanillaMode": False, "enableSkills": True,
        "addSystemSkills": False, "allowedSkills": [NAME], "outputType": None, "hooks": None,
        "permissions": None}}, a.dry_run)

    if a.dry_run:
        return
    smoke = code + "\n\ndef __smoke():\n    r = main('catalog')\n    return {'status': r.get('status'), 'kit_version': r.get('kit_version'), 'recipes': [x.get('name') for x in r.get('recipes', [])]}\n"
    smoke = smoke.replace("\ndef main(operation", "\ndef _kit_entry(operation").replace("r = main('catalog')", "r = _kit_entry('catalog')")
    smoke += "\n\ndef main() -> dict:\n    return __smoke()\n"
    r = s.post(f"{endpoint}/api/v1/extendedAgent/tools/python/test", timeout=300, json={
        "functionCode": smoke, "timeoutSeconds": 120, "parameters": {}, "parameterDefinitions": []})
    try:
        print("Sandbox smoke test:", json.loads(r.json().get("result") or "{}"))
    except Exception:
        print("Sandbox smoke test response:", r.status_code, r.text[:400])
    print(f"\nDone. In the agent chat run:  /agent {NAME}  and describe your use case.")


if __name__ == "__main__":
    main()
