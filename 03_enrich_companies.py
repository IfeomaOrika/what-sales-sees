"""
Plait Enrichment Layer
======================

Adds *fit* context to the companies already in HubSpot — employee count,
industry, tech stack, funding stage. Combines real API calls (Apollo, Hunter,
BuiltWith) with a synthetic fallback for unmatched domains.

Pipeline:
  1. Pull all Plait companies from HubSpot
  2. For each company domain:
       - Try Apollo enrichment (company size, industry, funding)
       - Try Hunter for contact patterns
       - Try BuiltWith for tech stack
       - On miss, synthesize plausible data from domain pattern + size band
  3. Compute Fit Score (0-100) from enrichment
  4. Recompute Composite Score = blend of Fit + Intent
  5. Update HubSpot Company records

Usage:
  export HUBSPOT_TOKEN=...
  export APOLLO_API_KEY=...     # optional
  export HUNTER_API_KEY=...     # optional
  export BUILTWITH_API_KEY=...  # optional
  python3 enrich.py
"""

import json
import os
import random
import sys
import time
from typing import Optional

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip install requests")
    sys.exit(1)


# ============================================================
# Config
# ============================================================
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY")
BUILTWITH_API_KEY = os.getenv("BUILTWITH_API_KEY")

if not HUBSPOT_TOKEN:
    print("HUBSPOT_TOKEN not set. Run: export HUBSPOT_TOKEN=...")
    sys.exit(1)

random.seed(42)  # reproducible synthetic fallback


# ============================================================
# Domain → persona pool mapping (mirrors generate_events.py)
# ============================================================
MID_SIZE_DOMAINS = {
    "northwind.co", "acmegrowth.io", "loomify.com", "pulsestack.io",
    "harborlytics.com", "tidemark.app", "fernpath.io", "stagecraft.co",
    "yieldhub.com", "anvilbase.io", "wovenpath.com", "lattice-co.io",
    "stackline.app", "everleaf.com", "outpostlabs.io", "bluespan.co",
}
ENTERPRISE_DOMAINS = {
    "globexcorp.com", "meridianholdings.com", "ironforgeenterprises.com",
    "vertexpharma.com", "atlasretail.com", "summitfinancial.com",
    "kestrelbank.com", "magnoliahealth.com",
}
SMALL_COMPANY_DOMAINS = {
    "twobitstudio.com", "soloventures.io", "tinypress.co", "draftboard.io",
    "spareclock.com", "kindlinglabs.com", "saplingstack.io", "tinkershop.co",
}


# ============================================================
# HubSpot client
# ============================================================
def hubspot_get_all_companies() -> list[dict]:
    """Pull every Plait company with the properties we need."""
    url = "https://api.hubapi.com/crm/v3/objects/companies"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    params = {
        "limit": 100,
        "properties": (
            "domain,name,plait_account_id,plait_composite_score,"
            "plait_routing_queue,plait_enterprise_intent,plait_signal_summary"
        ),
    }
    out = []
    after = None
    while True:
        if after:
            params["after"] = after
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code >= 300:
            print(f"HubSpot read failed: {r.status_code} {r.text[:200]}")
            sys.exit(1)
        data = r.json()
        out.extend(data.get("results", []))
        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break
    return out


def hubspot_batch_update_companies(updates: list[dict]):
    """Batch update companies by HubSpot ID."""
    url = "https://api.hubapi.com/crm/v3/objects/companies/batch/update"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    for i in range(0, len(updates), 100):
        batch = updates[i:i + 100]
        body = {"inputs": batch}
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=60)
                if r.status_code == 401:
                    print(f"HubSpot auth error: {r.text[:200]}")
                    sys.exit(1)
                if r.status_code >= 300:
                    print(f"  Batch error {r.status_code}: {r.text[:200]}")
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    break
                print(f"  Updated batch of {len(batch)} companies")
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt < 2:
                    time.sleep(2 ** attempt)


# ============================================================
# Real API integrations (all gracefully skip if no key)
# ============================================================
def apollo_enrich(domain: str) -> Optional[dict]:
    if not APOLLO_API_KEY:
        return None
    url = "https://api.apollo.io/api/v1/organizations/enrich"
    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Api-Key": APOLLO_API_KEY,
    }
    try:
        r = requests.get(url, headers=headers, params={"domain": domain}, timeout=15)
        if r.status_code != 200:
            return None
        org = r.json().get("organization")
        if not org:
            return None
        return {
            "employees": org.get("estimated_num_employees"),
            "industry": org.get("industry"),
            "founded_year": org.get("founded_year"),
            "country": org.get("country"),
            "latest_funding_stage": org.get("latest_funding_stage"),
        }
    except (requests.ConnectionError, requests.Timeout):
        return None


def hunter_enrich(domain: str) -> Optional[dict]:
    if not HUNTER_API_KEY:
        return None
    url = "https://api.hunter.io/v2/domain-search"
    try:
        r = requests.get(
            url,
            params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 5},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json().get("data", {})
        if not data.get("emails"):
            return None
        decision_makers = [
            f"{e.get('first_name', '')} {e.get('last_name', '')} ({e.get('position', '')})"
            for e in data.get("emails", [])
            if e.get("position")
            and any(t in (e.get("position", "")).lower()
                    for t in ["vp", "head", "director", "chief", "lead"])
        ]
        return {
            "email_pattern": data.get("pattern"),
            "decision_makers": decision_makers[:3],
        }
    except (requests.ConnectionError, requests.Timeout):
        return None


def builtwith_enrich(domain: str) -> Optional[dict]:
    if not BUILTWITH_API_KEY:
        return None
    url = "https://api.builtwith.com/v21/api.json"
    try:
        r = requests.get(
            url,
            params={"KEY": BUILTWITH_API_KEY, "LOOKUP": domain},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        results = r.json().get("Results", [])
        if not results:
            return None
        tech_names = []
        for path in results[0].get("Result", {}).get("Paths", []):
            for tech in path.get("Technologies", []):
                tech_names.append(tech.get("Name", ""))
        data_stack_keywords = [
            "snowflake", "bigquery", "redshift", "postgresql", "postgres",
            "mysql", "databricks", "looker", "tableau", "segment",
        ]
        data_stack = [
            t for t in tech_names
            if any(kw in t.lower() for kw in data_stack_keywords)
        ]
        return {"tech_stack": tech_names[:10], "data_stack": data_stack}
    except (requests.ConnectionError, requests.Timeout):
        return None


# ============================================================
# Synthetic fallback
# ============================================================
INDUSTRIES_MID = ["SaaS", "Fintech", "Marketing Technology", "Developer Tools",
                  "E-commerce", "HR Tech"]
INDUSTRIES_ENTERPRISE = ["Financial Services", "Healthcare", "Retail",
                         "Pharmaceuticals", "Manufacturing", "Banking"]
INDUSTRIES_SMALL = ["SaaS", "Creative Services", "Consulting",
                    "Developer Tools", "Education"]

FUNDING_STAGES_MID = ["Series A", "Series B", "Series C"]
FUNDING_STAGES_ENTERPRISE = ["Public", "Series D", "Series E", "Private Equity"]
FUNDING_STAGES_SMALL = ["Pre-seed", "Seed", "Bootstrapped"]

DATA_STACK_MID = [
    ["Postgres", "Segment"],
    ["Snowflake", "dbt", "Segment"],
    ["BigQuery", "dbt"],
    ["Postgres", "Looker"],
    ["Snowflake", "Looker", "Segment"],
]
DATA_STACK_ENTERPRISE = [
    ["Snowflake", "Tableau", "Informatica"],
    ["Databricks", "Power BI"],
    ["Redshift", "Looker", "dbt"],
    ["Snowflake", "Tableau", "dbt"],
]
DATA_STACK_SMALL = [
    ["Postgres"],
    ["Supabase"],
    ["MySQL"],
    [],
]


def synthesize_enrichment(domain: str) -> dict:
    """Plausible enrichment based on which persona pool the domain belongs to."""
    if domain in ENTERPRISE_DOMAINS:
        return {
            "employees": random.choice([800, 1200, 1800, 2400, 3500, 5000]),
            "industry": random.choice(INDUSTRIES_ENTERPRISE),
            "founded_year": random.randint(1985, 2010),
            "country": "United States",
            "latest_funding_stage": random.choice(FUNDING_STAGES_ENTERPRISE),
            "data_stack": random.choice(DATA_STACK_ENTERPRISE),
            "_source": "synthetic",
        }
    if domain in SMALL_COMPANY_DOMAINS:
        return {
            "employees": random.randint(2, 9),
            "industry": random.choice(INDUSTRIES_SMALL),
            "founded_year": random.randint(2021, 2025),
            "country": random.choice(["United States", "United Kingdom", "Canada"]),
            "latest_funding_stage": random.choice(FUNDING_STAGES_SMALL),
            "data_stack": random.choice(DATA_STACK_SMALL),
            "_source": "synthetic",
        }
    # Default: mid-size SaaS pool
    return {
        "employees": random.randint(60, 400),
        "industry": random.choice(INDUSTRIES_MID),
        "founded_year": random.randint(2014, 2022),
        "country": "United States",
        "latest_funding_stage": random.choice(FUNDING_STAGES_MID),
        "data_stack": random.choice(DATA_STACK_MID),
        "_source": "synthetic",
    }


def enrich_company(domain: str) -> dict:
    """Try real APIs, fall back to synthetic. Always returns a complete record."""
    enrichment = {}
    real_hit = False

    apollo_data = apollo_enrich(domain)
    if apollo_data:
        enrichment.update({k: v for k, v in apollo_data.items() if v})
        real_hit = True
    hunter_data = hunter_enrich(domain)
    if hunter_data:
        enrichment.update({k: v for k, v in hunter_data.items() if v})
        real_hit = True
    builtwith_data = builtwith_enrich(domain)
    if builtwith_data:
        enrichment.update({k: v for k, v in builtwith_data.items() if v})
        real_hit = True

    has_essentials = enrichment.get("employees") and enrichment.get("industry")
    if has_essentials:
        enrichment["_source"] = "real"
        return enrichment

    # Fallback — fill gaps with synthetic
    synth = synthesize_enrichment(domain)
    for k, v in synth.items():
        if k not in enrichment or enrichment[k] is None:
            enrichment[k] = v
    enrichment["_source"] = "hybrid" if real_hit else "synthetic"
    return enrichment


# ============================================================
# Fit scoring
# ============================================================
def compute_fit_score(enrichment: dict) -> int:
    """Fit = how well does this company match Plait's ICP?
    Sweet spot: mid-size SaaS, Series A-C, has a data warehouse, 50-500 employees."""
    score = 0
    emp = enrichment.get("employees") or 0
    industry = (enrichment.get("industry") or "").lower()
    funding = (enrichment.get("latest_funding_stage") or "").lower()
    data_stack = [s.lower() for s in (enrichment.get("data_stack") or [])]

    # Employee size — sweet spot 50-500
    if 50 <= emp <= 500:
        score += 35
    elif 500 < emp <= 1500:
        score += 25
    elif 10 <= emp < 50:
        score += 15
    elif 1500 < emp <= 5000:
        score += 20
    elif emp > 5000:
        score += 10
    else:
        score += 0

    # Industry
    if any(kw in industry for kw in ["saas", "software", "tech", "fintech", "marketing technology"]):
        score += 25
    elif any(kw in industry for kw in ["e-commerce", "developer", "hr"]):
        score += 15
    elif any(kw in industry for kw in ["financial", "banking", "healthcare"]):
        score += 10
    else:
        score += 5

    # Funding stage
    if any(kw in funding for kw in ["series a", "series b", "series c"]):
        score += 25
    elif any(kw in funding for kw in ["series d", "series e", "private equity"]):
        score += 15
    elif "seed" in funding:
        score += 10
    elif "public" in funding:
        score += 10
    elif "bootstrap" in funding:
        score += 5

    # Data stack — having a warehouse means Plait can connect
    warehouse_terms = ["snowflake", "bigquery", "redshift", "postgres", "databricks"]
    if any(any(w in tech for w in warehouse_terms) for tech in data_stack):
        score += 15

    return max(0, min(100, score))


def compute_blended_composite(fit: int, intent: int, enterprise_intent: bool) -> int:
    """v2 composite: weighted blend of fit + intent.
    40% fit, 60% intent. Enterprise intent floors at 50."""
    blended = int(0.4 * fit + 0.6 * intent)
    if enterprise_intent:
        return max(50, blended)
    return blended


def enriched_summary(props: dict, enrichment: dict, fit: int, intent: int, composite: int) -> str:
    """Rewrite the signal summary to include enrichment context."""
    parts = [f"Score {composite}/100 (Fit {fit} / Intent {intent})."]
    fit_bits = []
    if enrichment.get("employees"):
        fit_bits.append(f"{enrichment['employees']} employees")
    if enrichment.get("industry"):
        fit_bits.append(enrichment["industry"])
    if enrichment.get("latest_funding_stage"):
        fit_bits.append(enrichment["latest_funding_stage"])
    if enrichment.get("data_stack"):
        fit_bits.append(f"stack: {', '.join(enrichment['data_stack'][:3])}")
    if fit_bits:
        parts.append(" | ".join(fit_bits) + ".")
    # Preserve old behavioral signals from the previous summary
    old = props.get("plait_signal_summary") or ""
    if old.startswith("Score "):
        old = old.split(".", 1)[-1].strip()
    if old:
        parts.append(old)
    return " ".join(parts)


# ============================================================
# Main
# ============================================================
def main():
    print("=== Plait Enrichment Layer ===\n")
    api_status = [
        f"Apollo: {'enabled' if APOLLO_API_KEY else 'fallback only'}",
        f"Hunter: {'enabled' if HUNTER_API_KEY else 'fallback only'}",
        f"BuiltWith: {'enabled' if BUILTWITH_API_KEY else 'fallback only'}",
    ]
    print("API status: " + " | ".join(api_status) + "\n")

    print("Fetching companies from HubSpot...")
    companies = hubspot_get_all_companies()
    print(f"  {len(companies)} companies retrieved.\n")

    print("Enriching...")
    updates = []
    real_count = 0
    hybrid_count = 0
    synth_count = 0
    for c in companies:
        props = c.get("properties", {})
        domain = props.get("domain") or props.get("plait_account_id")
        if not domain:
            continue
        intent_score = int(props.get("plait_composite_score") or 0)
        enterprise_intent = props.get("plait_enterprise_intent") == "true"

        enrichment = enrich_company(domain)
        src = enrichment.get("_source", "synthetic")
        if src == "real":
            real_count += 1
        elif src == "hybrid":
            hybrid_count += 1
        else:
            synth_count += 1

        fit = compute_fit_score(enrichment)
        composite = compute_blended_composite(fit, intent_score, enterprise_intent)

        updates.append({
            "id": c["id"],
            "properties": {
                "plait_composite_score": composite,
                "plait_signal_summary": enriched_summary(
                    props, enrichment, fit, intent_score, composite
                ),
            },
        })

    print(f"  Enrichment sources: {real_count} real | "
          f"{hybrid_count} hybrid | {synth_count} synthetic\n")

    print("Updating HubSpot...")
    hubspot_batch_update_companies(updates)
    print(f"\nDone. {len(updates)} companies enriched and re-scored.")
    print("Spot-check tips:")
    print("  - Sort by Plait Composite Score desc — small-co champions should drop")
    print("  - Open any company → signal summary now shows fit context")
    print("  - Look for the (Fit X / Intent Y) breakdown in the summary string")


if __name__ == "__main__":
    main()
