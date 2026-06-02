"""
List all Domino Model API endpoints with owner, project, status, and hardware tier.
Uses the Domino Public API (/api/modelServing/v1).
Domino 6.1+  |  Requires admin API token for cross-project visibility.

Note: hardwareTierId is missing from the list endpoint (bug) — fetched individually.

Prerequisites:
    pip install requests pandas
"""

import os
import sys
import requests
import pandas as pd

# ── Config ───────────────────────────────────────────────────────────────────
DOMINO_HOST    = os.environ.get("DOMINO_HOST", "https://wgamage73590.cs.domino.tech")
DOMINO_API_KEY = os.environ.get("DOMINO_USER_API_KEY", "your-api-key-or-pat")

if not DOMINO_HOST.startswith("http"):
    DOMINO_HOST = f"https://{DOMINO_HOST}"

HEADERS = {
    "X-Domino-Api-Key": DOMINO_API_KEY,
    "Content-Type": "application/json",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_get(url, params=None):
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        if r.status_code >= 400:
            return None
        if "html" in r.headers.get("Content-Type", ""):
            return None
        return r.json()
    except Exception:
        return None


def get_all_model_apis():
    """List endpoint — hardwareTierId missing (bug), use for enumeration only."""
    body = safe_get(f"{DOMINO_HOST}/api/modelServing/v1/modelApis")
    if body is None:
        print("[ERROR] Failed to fetch model APIs — check DOMINO_HOST and API key.")
        sys.exit(1)
    return body.get("items", [])


def get_model_api(model_api_id):
    """Individual GET — includes hardwareTierId correctly."""
    return safe_get(f"{DOMINO_HOST}/api/modelServing/v1/modelApis/{model_api_id}") or {}


def get_model_api_version(model_api_id, version_id):
    """Version detail — includes projectId."""
    return safe_get(
        f"{DOMINO_HOST}/api/modelServing/v1/modelApis/{model_api_id}/versions/{version_id}"
    ) or {}


def get_hardware_tiers():
    """Build lookup {hardwareTierId -> hardwareTierName}."""
    body = safe_get(f"{DOMINO_HOST}/api/hardwaretiers/v1/hardwaretiers")
    if body is None:
        print("[WARN] Could not fetch hardware tiers.")
        return {}
    tiers = body if isinstance(body, list) else body.get("hardwareTiers", [])
    return {t["id"]: t["name"] for t in tiers}


def get_project_lookup():
    """Build lookup {projectId -> projectName}."""
    body = safe_get(f"{DOMINO_HOST}/api/projects/beta/projects", params={"limit": 500})
    if body is None:
        return {}
    items = body if isinstance(body, list) else body.get("projects", [])
    return {p["id"]: p["name"] for p in items}


def get_user_lookup():
    """Build lookup {userId -> username}."""
    body = safe_get(f"{DOMINO_HOST}/v4/users")
    if body is None:
        return {}
    users = body if isinstance(body, list) else body.get("users", [])
    return {u["id"]: u.get("userName", u.get("username", "unknown")) for u in users}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"DOMINO_HOST = {DOMINO_HOST}")
    print(f"API key set = {'yes' if DOMINO_API_KEY != 'your-api-key-or-pat' else 'NO — using placeholder!'}\n")

    print("Fetching hardware tiers …")
    hw_lookup = get_hardware_tiers()
    print(f"  → {len(hw_lookup)} tiers loaded.")

    print("Fetching projects …")
    project_lookup = get_project_lookup()
    print(f"  → {len(project_lookup)} projects loaded.")

    print("Fetching user lookup …")
    user_lookup = get_user_lookup()
    print(f"  → {len(user_lookup)} users loaded.")

    print("Fetching model API list …")
    model_apis = get_all_model_apis()
    print(f"  → {len(model_apis)} model APIs found.\n")

    records = []

    for api in model_apis:
        model_api_id = api.get("id")
        model_name   = api.get("name")

        # Fetch individual model to get hardwareTierId (missing from list endpoint)
        detail       = get_model_api(model_api_id)
        hw_tier_id   = detail.get("hardwareTierId")
        hw_tier_name = hw_lookup.get(hw_tier_id, hw_tier_id or "N/A")
        replicas     = detail.get("replicas", api.get("replicas", "?"))
        is_async     = detail.get("isAsync", api.get("isAsync", False))

        # Status and active version from activeVersion sub-object
        active_version = detail.get("activeVersion", api.get("activeVersion", {}))
        version_id     = active_version.get("id")
        version_number = active_version.get("number", "?")
        status         = active_version.get("deployment", {}).get("status", "Unknown")

        # Owner from collaborators
        owner = "unknown"
        for c in detail.get("collaborators", api.get("collaborators", [])):
            if c.get("role") == "Owner":
                ref = c.get("collaborator", "")
                if "UserRef(" in ref:
                    user_id = ref.replace("UserRef(", "").replace(")", "")
                    owner = user_lookup.get(user_id, user_id)
                break

        # Project from version detail
        project_name = "unknown"
        if version_id:
            version_detail = get_model_api_version(model_api_id, version_id)
            project_id = version_detail.get("projectId")
            if project_id:
                project_name = project_lookup.get(project_id, project_id)

        records.append({
            "Model Name":    model_name,
            "Status":        status,
            "Owner":         owner,
            "Project":       project_name,
            "Hardware Tier": hw_tier_name,
            "Replicas":      replicas,
            "Async":         is_async,
            "Active Ver.":   version_number,
            "Model API ID":  model_api_id,
        })

    if not records:
        print("No model APIs found.")
        return

    df = pd.DataFrame(records)
    print("=== Domino Model API Endpoints ===\n")
    print(df.to_string(index=False))

    out_file = "domino_model_apis.csv"
    df.to_csv(out_file, index=False)
    print(f"\nSaved to {out_file}")


if __name__ == "__main__":
    main()