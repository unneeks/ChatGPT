#!/usr/bin/env python3
"""Jira T2.3 setup — creates custom fields, workflow statuses, and webhook.

Run this locally (not in the container) with your Jira credentials.

Usage:
  export JIRA_EMAIL=your@email.com
  export JIRA_TOKEN=your-api-token
  export JIRA_BASE=https://jira-sanz.atlassian.net
  export JIRA_PROJECT_KEY=BANK           # project key to target
  export REQSMITH_WEBHOOK_URL=https://...  # your deployed app URL (or ngrok for local dev)
  export JIRA_WEBHOOK_SECRET=any-random-string

  python deploy/scripts/jira_setup.py

What it does:
  1. Validates credentials against /rest/api/3/myself
  2. Creates custom fields (Risk Tier, Agent Confidence, Provenance Link, Run State)
  3. Records the field IDs for use in reqsmith settings
  4. Registers the reqsmith webhook (issue created/updated, comment created)
  5. Writes a .env.jira snippet you can paste into your deployment config
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "")
JIRA_BASE = os.environ.get("JIRA_BASE", "https://jira-sanz.atlassian.net").rstrip("/")
PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "BANK")
WEBHOOK_URL = os.environ.get("REQSMITH_WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("JIRA_WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_header() -> str:
    raw = f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()
    return f"Basic {base64.b64encode(raw).decode()}"


def _jira(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{JIRA_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header())
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        print(f"  HTTP {exc.code} on {method} {path}: {body_text[:300]}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_validate() -> str:
    """Returns the account display name."""
    print("\n[1/4] Validating credentials...")
    me = _jira("GET", "/rest/api/3/myself")
    name = me.get("displayName", "unknown")
    email = me.get("emailAddress", "unknown")
    print(f"  ✓ Logged in as: {name} <{email}>")
    return email


def step_custom_fields() -> dict[str, str]:
    """Create required custom fields. Returns {field_name: field_id}."""
    print("\n[2/4] Setting up custom fields...")

    # Check existing fields to avoid duplicates
    existing = _jira("GET", "/rest/api/3/field")
    existing_by_name = {f["name"]: f["id"] for f in existing}

    field_ids: dict[str, str] = {}

    fields_to_create = [
        {
            "name": "Risk Tier",
            "type": "com.atlassian.jira.plugin.system.customfieldtypes:select",
            "searcherKey": "com.atlassian.jira.plugin.system.customfieldtypes:multiselectsearcher",
        },
        {
            "name": "Agent Confidence",
            "type": "com.atlassian.jira.plugin.system.customfieldtypes:float",
            "searcherKey": "com.atlassian.jira.plugin.system.customfieldtypes:exactnumber",
        },
        {
            "name": "Provenance Link",
            "type": "com.atlassian.jira.plugin.system.customfieldtypes:url",
            "searcherKey": "com.atlassian.jira.plugin.system.customfieldtypes:textsearcher",
        },
        {
            "name": "Run State",
            "type": "com.atlassian.jira.plugin.system.customfieldtypes:textfield",
            "searcherKey": "com.atlassian.jira.plugin.system.customfieldtypes:textsearcher",
        },
    ]

    for field_def in fields_to_create:
        name = field_def["name"]
        if name in existing_by_name:
            fid = existing_by_name[name]
            print(f"  → '{name}' already exists: {fid}")
            field_ids[name] = fid
        else:
            try:
                result = _jira("POST", "/rest/api/3/field", {
                    "name": name,
                    "type": field_def["type"],
                    "searcherKey": field_def["searcherKey"],
                })
                fid = result["id"]
                print(f"  ✓ Created '{name}': {fid}")
                field_ids[name] = fid
            except Exception as exc:
                print(f"  ✗ Failed to create '{name}': {exc}")

    # Add options to Risk Tier select field
    if "Risk Tier" in field_ids:
        rt_id = field_ids["Risk Tier"]
        for option in ("low", "medium", "high"):
            try:
                _jira("POST", f"/rest/api/3/field/{rt_id}/context/defaultContextId/option", {
                    "options": [{"value": option}]
                })
                print(f"  ✓ Added Risk Tier option: {option}")
            except Exception:
                pass  # option may already exist

    return field_ids


def step_webhook() -> str | None:
    """Register the reqsmith webhook. Returns webhook ID or None."""
    if not WEBHOOK_URL:
        print("\n[3/4] Skipping webhook — REQSMITH_WEBHOOK_URL not set")
        print("      Set it and rerun, or register manually in Jira Settings → WebHooks")
        return None

    print(f"\n[3/4] Registering webhook → {WEBHOOK_URL}/webhooks/jira")

    # List existing webhooks to check for duplicates
    try:
        existing = _jira("GET", "/rest/webhooks/1.0/webhook")
        for wh in existing:
            if WEBHOOK_URL in (wh.get("url") or ""):
                print(f"  → Webhook already registered: id={wh['self'].split('/')[-1]}")
                return wh["self"].split("/")[-1]
    except Exception:
        pass

    payload = {
        "name": "reqsmith",
        "url": f"{WEBHOOK_URL}/webhooks/jira",
        "events": [
            "jira:issue_created",
            "jira:issue_updated",
            "comment_created",
        ],
        "jqlFilter": f"project = {PROJECT_KEY}",
        "excludeBody": False,
    }
    if WEBHOOK_SECRET:
        payload["secret"] = WEBHOOK_SECRET

    try:
        result = _jira("POST", "/rest/webhooks/1.0/webhook", payload)
        wh_id = result.get("self", "").split("/")[-1] if isinstance(result, dict) else "?"
        print(f"  ✓ Webhook registered (id={wh_id})")
        return wh_id
    except Exception as exc:
        print(f"  ✗ Webhook registration failed: {exc}")
        print("    Register manually: Jira Settings → System → WebHooks → Create WebHook")
        return None


def step_write_env(field_ids: dict[str, str], actual_email: str) -> None:
    """Write .env.jira snippet with all discovered IDs."""
    print("\n[4/4] Writing .env.jira configuration snippet...")

    def fid(name: str) -> str:
        return field_ids.get(name, "FILL_IN")

    lines = [
        "# reqsmith Jira integration — paste these into your Container App env vars",
        f"JIRA_BASE_URL={JIRA_BASE}",
        f"JIRA_EMAIL={actual_email}",
        "JIRA_API_TOKEN=<rotate-and-paste-new-token>",
        f"JIRA_PROJECT_KEY={PROJECT_KEY}",
        f"JIRA_WEBHOOK_SECRET={WEBHOOK_SECRET or 'SET_THIS'}",
        f"JIRA_FIELD_RISK_TIER={fid('Risk Tier')}",
        f"JIRA_FIELD_CONFIDENCE={fid('Agent Confidence')}",
        f"JIRA_FIELD_PROVENANCE={fid('Provenance Link')}",
        f"JIRA_FIELD_RUN_STATE={fid('Run State')}",
    ]

    env_path = ".env.jira"
    with open(env_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  ✓ Written to {env_path}")
    print()
    print("  Next:")
    print("  1. Regenerate your API token at https://id.atlassian.com/manage-api-tokens")
    print("     (the one used here was transmitted in plaintext — rotate it)")
    print(f"  2. Paste the contents of {env_path} into your Azure Container App env vars")
    print("  3. Create an Epic in the pilot project and watch reqsmith pick it up")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not JIRA_EMAIL or not JIRA_TOKEN:
        print("ERROR: Set JIRA_EMAIL and JIRA_TOKEN environment variables", file=sys.stderr)
        sys.exit(1)

    print(f"reqsmith Jira setup → {JIRA_BASE} (project: {PROJECT_KEY})")

    try:
        actual_email = step_validate()
        field_ids = step_custom_fields()
        step_webhook()
        step_write_env(field_ids, actual_email)
        print("\n✓ Jira setup complete.")
    except Exception as exc:
        print(f"\n✗ Setup failed: {exc}", file=sys.stderr)
        sys.exit(1)
