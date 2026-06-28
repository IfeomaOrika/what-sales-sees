"""
Plait PostHog → HubSpot Pipe
============================

Reads product behavior from PostHog via HogQL, aggregates signals per user
and per company, and upserts the results into HubSpot Contact and Company
records using a Service Key.

Pipeline:
  1. Query PostHog for per-user aggregates (HogQL)
  2. Roll up per-company aggregates in Python
  3. Apply v1 scoring + routing logic (placeholder — real scoring is Week 4)
  4. Upsert Contacts into HubSpot (keyed by plait_user_id)
  5. Upsert Companies into HubSpot (keyed by plait_account_id)

Usage:
  export POSTHOG_PROJECT_ID=12345
  export POSTHOG_PERSONAL_KEY=phx_xxxxxxxxxxxx
  export POSTHOG_HOST=https://us.posthog.com
  export HUBSPOT_TOKEN=your_service_key_here
  python3 pipe.py

Setup:
  pip install requests

Notes:
  - Idempotent: re-running upserts based on plait_user_id / plait_account_id.
  - Scoring is v1 placeholder logic. Week 4 replaces with real scoring layer.
  - HubSpot Free tier batch limit is 100 records — this respects that.
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip install requests")
    sys.exit(1)


# ============================================================
# Config
# ============================================================
POSTHOG_PROJECT_ID = os.getenv("POSTHOG_PROJECT_ID")
POSTHOG_PERSONAL_KEY = os.getenv("POSTHOG_PERSONAL_KEY")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.posthog.com")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN")

# Validate config upfront — fail fast
def validate_config():
    missing = []
    if not POSTHOG_PROJECT_ID: missing.append("POSTHOG_PROJECT_ID")
    if not POSTHOG_PERSONAL_KEY: missing.append("POSTHOG_PERSONAL_KEY")
    if not HUBSPOT_TOKEN: missing.append("HUBSPOT_TOKEN")
    if missing:
        print(f"Missing env vars: {', '.join(missing)}")
        print("Set them with: export VAR=value")
        sys.exit(1)


# ============================================================
# PostHog client
# ============================================================
def posthog_query(hogql: str) -> list[dict]:
    """Run a HogQL query against PostHog and return rows as list of dicts."""
    url = f"{POSTHOG_HOST.rstrip('/')}/api/projects/{POSTHOG_PROJECT_ID}/query/"
    headers = {
        "Authorization": f"Bearer {POSTHOG_PERSONAL_KEY}",
        "Content-Type": "application/json",
    }
    body = {"query": {"kind": "HogQLQuery", "query": hogql}}

    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
            if r.status_code == 401:
                print(f"PostHog auth error: {r.text[:200]}")
                print("Check POSTHOG_PERSONAL_KEY scopes (need query:read, person:read).")
                sys.exit(1)
            if r.status_code >= 300:
                print(f"PostHog query failed ({r.status_code}): {r.text[:300]}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                sys.exit(1)
            data = r.json()
            cols = data.get("columns", [])
            rows = data.get("results", [])
            return [dict(zip(cols, row)) for row in rows]
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < 2:
                print(f"PostHog network error ({str(e)[:80]}), retrying...")
                time.sleep(2 ** attempt)
            else:
                print(f"PostHog network error, giving up: {e}")
                sys.exit(1)


# ============================================================
# Step 1: Pull per-user aggregates from PostHog via HogQL
# ============================================================
USER_AGGREGATES_QUERY = """
SELECT
    distinct_id,
    person.properties.email AS email,
    person.properties.name AS name,
    person.properties.company_id AS company_id,
    person.properties.company_name AS company_name,
    person.properties.company_domain AS company_domain,
    person.properties.company_size_band AS company_size_band,
    person.properties.email_type AS email_type,
    person.properties.signup_source AS signup_source,
    min(if(event = 'user_signed_up', timestamp, NULL)) AS signup_date,
    max(if(event NOT IN ('$identify', '$groupidentify'), timestamp, NULL)) AS last_active_date,
    countIf(event = 'data_source_connected') AS data_source_connections,
    countIf(event = 'first_dashboard_created') AS dashboards_created,
    countIf(event = 'teammate_invited') AS invite_count,
    countIf(event = 'dashboard_limit_reached') AS limit_hit_count,
    countIf(event = 'pricing_page_viewed') AS pricing_view_count,
    max(if(event = 'pricing_page_viewed', timestamp, NULL)) AS last_pricing_view,
    countIf(event = 'upgrade_modal_opened') AS upgrade_modal_count,
    countIf(event = 'comparison_page_viewed') AS comparison_view_count,
    countIf(event = 'case_study_viewed') AS case_study_count,
    countIf(event = 'demo_requested') AS demo_request_count,
    countIf(event = 'contact_sales_clicked') AS contact_sales_count,
    countIf(event = 'dashboard_deleted') AS dashboard_delete_count,
    countIf(
        event = 'docs_viewed'
        AND properties.doc_section IN ('sso', 'soc2', 'audit_logs', 'data_residency', 'gdpr')
    ) AS enterprise_docs_count,
    count() AS total_events
FROM events
WHERE timestamp > now() - INTERVAL 90 DAY
GROUP BY
    distinct_id, email, name, company_id, company_name, company_domain,
    company_size_band, email_type, signup_source
LIMIT 10000
"""


def fetch_user_aggregates() -> list[dict]:
    print("Querying PostHog for per-user aggregates (HogQL)...")
    rows = posthog_query(USER_AGGREGATES_QUERY)
    # Filter out rows where person properties didn't resolve (incomplete identify)
    rows = [r for r in rows if r.get("email") and r.get("company_id")]
    print(f"  Returned {len(rows)} users with full identity.")
    return rows


def dedupe_users_by_email(users: list[dict]) -> list[dict]:
    """Merge users that share an email. Sum count fields, take max/min on dates.
    Real production data sometimes has email collisions from imports — the pipe
    should handle this gracefully rather than failing on duplicate IDs."""
    count_fields = [
        "data_source_connections", "dashboards_created", "invite_count",
        "limit_hit_count", "pricing_view_count", "upgrade_modal_count",
        "comparison_view_count", "case_study_count", "demo_request_count",
        "contact_sales_count", "dashboard_delete_count", "enterprise_docs_count",
        "total_events",
    ]
    by_email: dict[str, dict] = {}
    for u in users:
        email = u["email"]
        if email not in by_email:
            by_email[email] = dict(u)
            continue
        existing = by_email[email]
        for k in count_fields:
            existing[k] = (existing.get(k) or 0) + (u.get(k) or 0)
        # Max of last_active and last_pricing
        for k in ("last_active_date", "last_pricing_view"):
            a, b = existing.get(k), u.get(k)
            if b and (not a or b > a):
                existing[k] = b
        # Min of signup
        a, b = existing.get("signup_date"), u.get("signup_date")
        if b and (not a or b < a):
            existing["signup_date"] = b
    merged = list(by_email.values())
    if len(merged) < len(users):
        print(f"  Merged {len(users) - len(merged)} duplicate-email users.")
    return merged


# ============================================================
# Step 2: Roll up per-company aggregates in Python
# ============================================================
def compute_company_aggregates(users: list[dict]) -> dict[str, dict]:
    """Group users by company DOMAIN and roll up account-level signals.
    Domain is the natural unique identifier for a company in real data —
    multiple PostHog company groups with the same domain merge into one."""
    by_domain: dict[str, dict] = {}

    for user in users:
        domain = user.get("company_domain")
        if not domain:
            continue
        if domain not in by_domain:
            by_domain[domain] = {
                "company_domain": domain,
                "company_id": user.get("company_id"),  # one representative ID
                "company_name": user.get("company_name"),
                "company_size_band": user.get("company_size_band"),
                "user_count": 0,
                "activated_user_count": 0,
                "total_dashboards": 0,
                "total_invites": 0,
                "total_limit_hits": 0,
                "total_pricing_views": 0,
                "total_comparison_views": 0,
                "total_case_study_views": 0,
                "total_demo_requests": 0,
                "total_contact_sales": 0,
                "total_dashboard_deletes": 0,
                "enterprise_intent": False,
                "earliest_signup": None,
                "latest_activity": None,
            }
        c = by_domain[domain]
        c["user_count"] += 1
        if (user.get("data_source_connections") or 0) > 0:
            c["activated_user_count"] += 1
        c["total_dashboards"] += user.get("dashboards_created") or 0
        c["total_invites"] += user.get("invite_count") or 0
        c["total_limit_hits"] += user.get("limit_hit_count") or 0
        c["total_pricing_views"] += user.get("pricing_view_count") or 0
        c["total_comparison_views"] += user.get("comparison_view_count") or 0
        c["total_case_study_views"] += user.get("case_study_count") or 0
        c["total_demo_requests"] += user.get("demo_request_count") or 0
        c["total_contact_sales"] += user.get("contact_sales_count") or 0
        c["total_dashboard_deletes"] += user.get("dashboard_delete_count") or 0
        if (user.get("enterprise_docs_count") or 0) > 0:
            c["enterprise_intent"] = True
        if user.get("signup_date"):
            if c["earliest_signup"] is None or user["signup_date"] < c["earliest_signup"]:
                c["earliest_signup"] = user["signup_date"]
        if user.get("last_active_date"):
            if c["latest_activity"] is None or user["last_active_date"] > c["latest_activity"]:
                c["latest_activity"] = user["last_active_date"]
    return by_domain


# ============================================================
# Step 3: v1 scoring + routing logic (placeholder — Week 4 replaces this)
# ============================================================
def compute_composite_score(company: dict) -> int:
    """Rough v1 score 0–100. Week 4 will replace with proper fit + intent model."""
    score = 0

    # Activation signal
    if company["activated_user_count"] > 0:
        score += 15
    if company["activated_user_count"] >= 2:
        score += 5

    # Depth / engagement
    if company["total_dashboards"] >= 3:
        score += 10
    if company["total_dashboards"] >= 5:
        score += 5

    # Expansion
    if company["total_invites"] >= 2:
        score += 20
    elif company["total_invites"] >= 1:
        score += 10

    # Intent — wall hit + pricing
    if company["total_limit_hits"] > 0:
        score += 15
    if company["total_pricing_views"] >= 2:
        score += 10
    if company["total_comparison_views"] > 0:
        score += 5
    if company["total_case_study_views"] > 0:
        score += 5

    # Conversion-stage signals
    if company["total_demo_requests"] > 0:
        score += 15
    if company["total_contact_sales"] > 0:
        score += 10

    # Negative signals
    if company["total_dashboard_deletes"] > 0:
        score -= 10  # "won't pay" tell
    if company["activated_user_count"] == 0:
        score -= 20

    # Fit dampener for very small accounts
    if company["company_size_band"] == "1-10":
        score = int(score * 0.7)

    return max(0, min(100, score))


def compute_routing_queue(company: dict, score: int) -> str:
    """Determine which queue this account routes to."""
    if company["enterprise_intent"]:
        return "AE_QUEUE"  # Override — bypass score, route to AE
    if score >= 60:
        if company["company_size_band"] in ("1-10",):
            return "NURTURE"  # Strong signals but too small to convert today
        return "PLG_QUEUE"
    if score >= 30:
        return "NURTURE"
    return "SUPPRESS"


def compute_signal_summary(company: dict, score: int) -> str:
    """Human-readable 'why this lead matters' string for the sales view."""
    bits = []
    if company["activated_user_count"] > 0:
        bits.append(f"{company['activated_user_count']} activated user(s)")
    if company["total_dashboards"] > 0:
        bits.append(f"{company['total_dashboards']} dashboards")
    if company["total_invites"] >= 1:
        bits.append(f"{company['total_invites']} teammates invited")
    if company["total_limit_hits"] > 0:
        bits.append("hit dashboard limit")
    if company["total_pricing_views"] >= 1:
        bits.append(f"viewed pricing {company['total_pricing_views']}x")
    if company["total_comparison_views"] > 0:
        bits.append("viewed competitor comparison")
    if company["total_case_study_views"] > 0:
        bits.append("viewed case studies")
    if company["total_demo_requests"] > 0:
        bits.append("requested a demo")
    if company["enterprise_intent"]:
        bits.append("ENTERPRISE INTENT: viewed SSO/security docs")
    if company["total_dashboard_deletes"] > 0:
        bits.append(f"deleted {company['total_dashboard_deletes']} dashboard(s) — price-sensitive")
    if not bits:
        bits.append("Signed up, no further activity")
    return f"Score {score}/100. " + "; ".join(bits) + "."


def identify_champions(users: list[dict]) -> set[str]:
    """For each company (by domain), the user with the highest engagement is the champion.
    Returns a set of emails — used to set is_plait_champion on Contact records."""
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for u in users:
        if u.get("company_domain"):
            by_domain[u["company_domain"]].append(u)

    champion_emails = set()
    for domain, members in by_domain.items():
        scored = [
            (
                m["email"],
                (m.get("invite_count") or 0)
                + (m.get("dashboards_created") or 0) * 2
                + (m.get("total_events") or 0) * 0.1,
            )
            for m in members
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        if scored and scored[0][1] > 0:
            champion_emails.add(scored[0][0])
    return champion_emails


# ============================================================
# HubSpot client
# ============================================================
def hubspot_batch_upsert(object_type: str, inputs: list[dict]):
    """Upsert a batch of Contact or Company records using HubSpot's batch API."""
    url = f"https://api.hubapi.com/crm/v3/objects/{object_type}/batch/upsert"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {"inputs": inputs}

    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=60)
            if r.status_code == 401:
                print(f"HubSpot auth error: {r.text[:200]}")
                sys.exit(1)
            if r.status_code >= 300:
                print(f"  HubSpot error {r.status_code}: {r.text[:300]}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return False
            return True
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < 2:
                print(f"  HubSpot network error, retry {attempt + 1}/2...")
                time.sleep(2 ** attempt)
            else:
                print(f"  HubSpot network error, giving up: {e}")
                return False


# ============================================================
# Step 4 & 5: Push to HubSpot
# ============================================================
def iso_date(dt_str: Optional[str]) -> Optional[str]:
    """Convert a PostHog timestamp string to a HubSpot-compatible ISO date (YYYY-MM-DD)."""
    if not dt_str:
        return None
    try:
        # PostHog returns ISO 8601 strings; strip time portion
        return dt_str.split("T")[0]
    except Exception:
        return None


def push_contacts(users: list[dict], champion_emails: set[str]):
    """Upsert Contact records, batched by 100."""
    print(f"\nUpserting {len(users)} Contacts to HubSpot...")
    inputs = []
    for u in users:
        inputs.append({
            "idProperty": "email",
            "id": u["email"],
            "properties": {
                "plait_user_id": u["distinct_id"],
                "plait_account_id": u.get("company_domain") or u["company_id"],
                "plait_signup_date": iso_date(u.get("signup_date")),
                "plait_last_active_date": iso_date(u.get("last_active_date")),
                "is_plait_champion": "true" if u["email"] in champion_emails else "false",
                # Standard HubSpot fields — needed so records have email + name
                "email": u["email"],
                "firstname": (u.get("name") or "").split(" ")[0] if u.get("name") else "",
                "lastname": " ".join((u.get("name") or "").split(" ")[1:]) if u.get("name") else "",
                "company": u.get("company_name") or "",
            },
        })
    # Strip None values (HubSpot rejects null properties)
    for inp in inputs:
        inp["properties"] = {k: v for k, v in inp["properties"].items() if v not in (None, "")}

    total_batches = (len(inputs) + 99) // 100
    success_count = 0
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        batch_num = i // 100 + 1
        ok = hubspot_batch_upsert("contacts", batch)
        if ok:
            print(f"  Batch {batch_num}/{total_batches}: upserted {len(batch)} contacts")
            success_count += len(batch)
        time.sleep(0.3)
    print(f"  Contacts: {success_count}/{len(inputs)} upserted.")


def push_companies(companies: dict[str, dict]):
    """Upsert Company records, batched by 100."""
    print(f"\nUpserting {len(companies)} Companies to HubSpot...")
    inputs = []
    for domain, c in companies.items():
        score = compute_composite_score(c)
        routing = compute_routing_queue(c, score)
        summary = compute_signal_summary(c, score)
        if not domain:
            continue
        inputs.append({
            # plait_account_id is our custom unique field (set to ON for uniqueness).
            # We use it instead of HubSpot's built-in `domain` because the built-in
            # is NOT marked unique-by-default — this is a common HubSpot gotcha.
            "idProperty": "plait_account_id",
            "id": domain,
            "properties": {
                "plait_account_id": domain,  # stable across runs — uses domain
                "plait_composite_score": score,
                "plait_routing_queue": routing,
                "plait_enterprise_intent": "true" if c["enterprise_intent"] else "false",
                "plait_signal_summary": summary,
                # Standard HubSpot fields
                "name": c.get("company_name") or domain,
                "domain": domain,
            },
        })
    for inp in inputs:
        inp["properties"] = {k: v for k, v in inp["properties"].items() if v not in (None, "")}

    total_batches = (len(inputs) + 99) // 100
    success_count = 0
    for i in range(0, len(inputs), 100):
        batch = inputs[i:i + 100]
        batch_num = i // 100 + 1
        ok = hubspot_batch_upsert("companies", batch)
        if ok:
            print(f"  Batch {batch_num}/{total_batches}: upserted {len(batch)} companies")
            success_count += len(batch)
        time.sleep(0.3)
    print(f"  Companies: {success_count}/{len(inputs)} upserted.")


# ============================================================
# Main
# ============================================================
def main():
    print("=== Plait PostHog → HubSpot Pipe ===\n")
    validate_config()

    # 1. Pull per-user data from PostHog
    users = fetch_user_aggregates()
    if not users:
        print("No users returned — check that $identify events fired and person properties are set.")
        return

    # Dedupe duplicate-email users — real CRM data is messy
    users = dedupe_users_by_email(users)

    # 2. Roll up to per-company aggregates (by domain)
    print(f"\nRolling up {len(users)} users into companies...")
    companies = compute_company_aggregates(users)
    print(f"  {len(companies)} unique companies (by domain).")

    # 3. Identify champions
    champion_emails = identify_champions(users)
    print(f"  {len(champion_emails)} champions identified.")

    # 4. Push to HubSpot — companies first so contacts can associate
    push_companies(companies)
    push_contacts(users, champion_emails)

    # Summary
    print("\n=== Done. Check HubSpot Contacts and Companies. ===")
    print("Spot-check tips:")
    print("  - Filter Companies by 'Plait Routing Queue = AE_QUEUE' to see enterprise leads")
    print("  - Sort Companies by 'Plait Composite Score' desc to see your top PQLs")
    print("  - Open a top-scoring company → check the Plait Signal Summary field")


if __name__ == "__main__":
    main()
