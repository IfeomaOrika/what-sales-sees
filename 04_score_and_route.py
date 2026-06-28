"""
Plait v3 Scoring Layer
======================

Replaces the placeholder scoring in enrich.py with a real qualification model:

  1. RECENCY DECAY — each event weight decays exponentially by age.
     Half-life of 14 days, so an event 14 days old counts half, 28 days old
     a quarter, etc. Recent behavior matters more than old behavior.

  2. STAGE-AWARE FIT/INTENT BLEND — fit and intent are weighted differently
     depending on where the account is in their journey:
       Awareness (just signed up):     20% fit / 80% intent
       Trial (activated):              30% fit / 70% intent
       Evaluation (hit wall / shopping): 45% fit / 55% intent
       Conversion (demo/billing/sales): 60% fit / 40% intent
     Early stages reward interest. Late stages reward fit.

  3. NEGATIVE SIGNAL CAPS — active rules that cap the score regardless of
     positive signals:
       Single session, <5 events:       score capped at 15
       Dormant 21+ days:                score capped at 40
       Deletion ratio >50%:             score capped at 35
       No activation after signup:      score capped at 25

  4. TIER ASSIGNMENT — A/B/C/D output written to HubSpot. Sales reps work
     A and B lists; C goes to nurture; D suppressed.
       A: 70+    B: 50-69    C: 25-49    D: 0-24

  AE_QUEUE override: enterprise-intent accounts get tier B floor regardless
  of composite score (low engagement enterprise leads still warrant AE time).

Usage:
  source ~/Downloads/plait_env.sh
  python3 score.py
"""

import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
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

if not all([POSTHOG_PROJECT_ID, POSTHOG_PERSONAL_KEY, HUBSPOT_TOKEN]):
    print("Missing env vars. Source ~/Downloads/plait_env.sh first.")
    sys.exit(1)

NOW = datetime.now(timezone.utc)
HALF_LIFE_DAYS = 14  # intent signals lose half their weight every 14 days


# ============================================================
# Event weights — used before decay
# ============================================================
INTENT_EVENT_WEIGHTS = {
    # Activation
    "data_source_connected": 12,
    "first_dashboard_created": 8,
    "dashboard_published": 5,
    # Depth (per-event, will sum across many events)
    "query_run": 0.3,
    "dashboard_viewed": 0.2,
    "dashboard_edited": 0.5,
    # Expansion (strong signals)
    "teammate_invited": 15,
    "dashboard_shared": 8,
    "comment_added": 4,
    # Intent
    "pricing_page_viewed": 6,
    "billing_page_viewed": 18,
    "upgrade_modal_opened": 15,
    "dashboard_limit_reached": 20,
    "comparison_page_viewed": 18,
    "case_study_viewed": 12,
    "integration_page_viewed": 4,
    "docs_viewed": 1,
    # Conversion (strongest)
    "demo_requested": 25,
    "contact_sales_clicked": 22,
    # Negative
    "dashboard_deleted": -8,
}

STAGE_WEIGHTS = {
    "awareness":  (0.20, 0.80),  # (fit_weight, intent_weight)
    "trial":      (0.30, 0.70),
    "evaluation": (0.45, 0.55),
    "conversion": (0.60, 0.40),
}

# Tier thresholds in descending order — score >= threshold → tier
# Tightened from v3.0: A is now reserved for genuinely qualified accounts.
# In real PLG, A should be ~5-15% of the dataset.
TIER_THRESHOLDS = [(80, "A"), (60, "B"), (35, "C"), (0, "D")]


# ============================================================
# PostHog client
# ============================================================
EVENT_QUERY = """
SELECT
    person.properties.company_domain AS domain,
    event,
    timestamp,
    properties
FROM events
WHERE timestamp > now() - INTERVAL 90 DAY
  AND event NOT IN ('$identify', '$groupidentify', '$autocapture', '$pageview')
  AND person.properties.company_domain IS NOT NULL
LIMIT 100000
"""


def posthog_query(hogql: str) -> list[dict]:
    url = f"{POSTHOG_HOST.rstrip('/')}/api/projects/{POSTHOG_PROJECT_ID}/query/"
    headers = {
        "Authorization": f"Bearer {POSTHOG_PERSONAL_KEY}",
        "Content-Type": "application/json",
    }
    body = {"query": {"kind": "HogQLQuery", "query": hogql}}
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
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
        except (requests.ConnectionError, requests.Timeout):
            if attempt < 2:
                time.sleep(2 ** attempt)
    sys.exit(1)


# ============================================================
# HubSpot client
# ============================================================
def hubspot_get_companies() -> list[dict]:
    url = "https://api.hubapi.com/crm/v3/objects/companies"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    params = {
        "limit": 100,
        "properties": (
            "domain,name,plait_account_id,plait_composite_score,"
            "plait_routing_queue,plait_signal_summary"
        ),
    }
    out, after = [], None
    while True:
        if after:
            params["after"] = after
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code >= 300:
            print(f"HubSpot read failed: {r.status_code} {r.text[:200]}")
            sys.exit(1)
        data = r.json()
        out.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return out


def hubspot_batch_update(updates: list[dict]):
    url = "https://api.hubapi.com/crm/v3/objects/companies/batch/update"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    for i in range(0, len(updates), 100):
        batch = updates[i:i + 100]
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json={"inputs": batch}, timeout=60)
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
# Recency decay
# ============================================================
def parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse PostHog's ISO 8601 timestamps to timezone-aware datetime."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def decay_weight(ts_str: str) -> float:
    """Exponential decay: weight halves every HALF_LIFE_DAYS."""
    ts = parse_timestamp(ts_str)
    if ts is None:
        return 0.0
    days_old = (NOW - ts).total_seconds() / 86400
    if days_old < 0:
        return 1.0  # future events (clock skew?) get full weight
    return 0.5 ** (days_old / HALF_LIFE_DAYS)


# ============================================================
# Per-domain event aggregation with decay
# ============================================================
def aggregate_events(events: list[dict]):
    """Returns:
       decayed[domain][event_type] = sum of weight * decay across all events
       raw_counts[domain][event_type] = int
       latest_ts[domain] = datetime
       earliest_ts[domain] = datetime
    """
    decayed = defaultdict(lambda: defaultdict(float))
    raw_counts = defaultdict(lambda: defaultdict(int))
    latest_ts = {}
    earliest_ts = {}

    for row in events:
        domain = row.get("domain")
        event = row.get("event")
        ts_str = row.get("timestamp")
        if not domain or not event:
            continue

        raw_counts[domain][event] += 1

        ts = parse_timestamp(ts_str)
        if ts:
            if domain not in latest_ts or ts > latest_ts[domain]:
                latest_ts[domain] = ts
            if domain not in earliest_ts or ts < earliest_ts[domain]:
                earliest_ts[domain] = ts

        weight = INTENT_EVENT_WEIGHTS.get(event, 0)
        if weight == 0:
            continue
        decayed[domain][event] += weight * decay_weight(ts_str)

    return decayed, raw_counts, latest_ts, earliest_ts


# ============================================================
# Stage classification
# ============================================================
def classify_stage(event_counts: dict) -> str:
    """Decide which funnel stage an account is in based on which events fired."""
    n_demo = event_counts.get("demo_requested", 0)
    n_sales = event_counts.get("contact_sales_clicked", 0)
    n_billing = event_counts.get("billing_page_viewed", 0)
    n_wall = event_counts.get("dashboard_limit_reached", 0)
    n_comparison = event_counts.get("comparison_page_viewed", 0)
    n_case_study = event_counts.get("case_study_viewed", 0)
    n_activation = event_counts.get("data_source_connected", 0)

    # Conversion: explicit conversion intent
    if n_demo > 0 or n_sales > 0 or n_billing >= 2:
        return "conversion"
    # Evaluation: hit a wall OR doing real comparison shopping
    if n_wall > 0 or n_comparison > 0 or n_case_study > 0 or n_billing >= 1:
        return "evaluation"
    # Trial: connected data and started using
    if n_activation > 0:
        return "trial"
    # Default: awareness (signed up, no real product engagement)
    return "awareness"


# ============================================================
# Fit score extraction (parsed from signal_summary written by enrich.py)
# ============================================================
# Matches both enrich.py output "(Fit 80 / Intent 76)" AND
# score.py output "(Fit 80 / Intent 76, Evaluation stage)" — anything after
# the intent number is ignored. Idempotent against re-runs.
FIT_INTENT_RE = re.compile(r"\(Fit\s*(\d+)\s*/\s*Intent\s*(\d+)")


def extract_fit(signal_summary: str) -> int:
    """Pull the fit score from the enriched signal_summary string. Default 50."""
    if not signal_summary:
        return 50
    m = FIT_INTENT_RE.search(signal_summary)
    if m:
        return int(m.group(1))
    return 50


# ============================================================
# Negative signal rules
# ============================================================
def apply_negative_signals(
    score: int,
    event_counts: dict,
    latest_ts: Optional[datetime],
    earliest_ts: Optional[datetime],
) -> tuple[int, list[str]]:
    """Cap the score based on red flags. Returns (capped_score, reasons)."""
    cap = 100
    reasons = []
    total_events = sum(event_counts.values())

    # 1. Single session / minimal engagement
    if total_events < 5:
        cap = min(cap, 15)
        reasons.append("single-session")

    # 2. Dormancy after some engagement
    if latest_ts:
        days_dormant = (NOW - latest_ts).days
        if days_dormant >= 21:
            cap = min(cap, 40)
            reasons.append(f"dormant {days_dormant}d")

    # 3. High deletion ratio
    deletes = event_counts.get("dashboard_deleted", 0)
    creates = event_counts.get("first_dashboard_created", 0)
    if creates > 0 and deletes / max(creates, 1) > 0.5:
        cap = min(cap, 35)
        reasons.append("high deletion ratio")

    # 4. No activation
    has_activation = event_counts.get("data_source_connected", 0) > 0
    if not has_activation:
        cap = min(cap, 25)
        reasons.append("no activation")

    # 5. No expansion after 21+ days
    if earliest_ts:
        account_age = (NOW - earliest_ts).days
        if account_age >= 21:
            invites = event_counts.get("teammate_invited", 0)
            if invites == 0 and has_activation:
                cap = min(cap, 50)
                reasons.append("no expansion in 21d")

    return min(score, cap), reasons


# ============================================================
# Scoring
# ============================================================
# Per-event-type contribution caps. No single event type can contribute
# more than this to intent, no matter how many times it fires. This prevents
# runaway scores when a domain has many users firing the same event.
EVENT_TYPE_CAPS = {
    # Activation — once is enough
    "data_source_connected": 15,
    "first_dashboard_created": 12,
    "dashboard_published": 8,
    # Depth — easy to inflate via repeated low-value events
    "query_run": 10,
    "dashboard_viewed": 6,
    "dashboard_edited": 6,
    # Expansion — diminishing returns past a few
    "teammate_invited": 30,
    "dashboard_shared": 12,
    "comment_added": 5,
    # Intent — strong signals but still cap to prevent runaway
    "pricing_page_viewed": 15,
    "billing_page_viewed": 22,
    "upgrade_modal_opened": 18,
    "dashboard_limit_reached": 22,
    "comparison_page_viewed": 20,
    "case_study_viewed": 18,
    "integration_page_viewed": 8,
    "docs_viewed": 6,
    # Conversion — these are decisive but still bounded
    "demo_requested": 32,
    "contact_sales_clicked": 28,
    # Negative
    "dashboard_deleted": -15,  # floor (negative cap)
}


def compute_intent_score(per_event_decayed: dict) -> int:
    """Sum capped contributions per event type. Architecturally correct:
    multiple firings of the same signal have diminishing returns, just
    like in real buyer psychology."""
    total = 0.0
    for event, weight_sum in per_event_decayed.items():
        cap = EVENT_TYPE_CAPS.get(event)
        if cap is None:
            total += weight_sum
        elif cap >= 0:
            total += min(weight_sum, cap)
        else:
            total += max(weight_sum, cap)  # negative cap = floor
    return max(0, min(100, int(total)))


def compute_composite(fit: int, intent: int, stage: str) -> int:
    """Stage-aware blended score."""
    fit_w, intent_w = STAGE_WEIGHTS.get(stage, (0.4, 0.6))
    return int(fit * fit_w + intent * intent_w)


def assign_tier(score: int, fit: int = 0, intent: int = 0) -> str:
    """A-tier requires BOTH dimensions strong, not just composite.
    This is a real GTM principle: 'great fit + hot intent' is genuinely
    different from 'great fit but quiet' or 'low fit but hot' — the former
    is call-today, the latter two are different sales motions."""
    # A-tier gate: must clear all three bars
    if score >= 80 and fit >= 70 and intent >= 70:
        return "A"
    # Fall through to threshold-based tiers
    if score >= 60:
        return "B"
    if score >= 35:
        return "C"
    return "D"


def upgrade_tier(tier: str, levels: int = 1) -> str:
    """Promote a tier (A is best). upgrade_tier('C', 1) → 'B'."""
    order = ["D", "C", "B", "A"]
    idx = order.index(tier)
    new_idx = min(len(order) - 1, idx + levels)
    return order[new_idx]


# ============================================================
# Signal summary rewrite
# ============================================================
def rewrite_summary(
    existing_summary: str,
    fit: int,
    intent: int,
    composite: int,
    tier: str,
    stage: str,
    negative_reasons: list[str],
) -> str:
    """Rebuild the signal summary string to include stage + tier + reasons."""
    head = f"Tier {tier} — Score {composite}/100 (Fit {fit} / Intent {intent}, {stage.title()} stage)."

    # Preserve the fit + behavior context from the previous summary
    body = existing_summary
    # Strip the old "Score X/100 (...)." prefix
    body = re.sub(r"^Tier [A-D]\s*—\s*", "", body)
    body = re.sub(r"^Score \d+/100[^.]*\.\s*", "", body)
    body = body.strip()

    parts = [head]
    if body:
        parts.append(body)
    if negative_reasons:
        parts.append("Flags: " + ", ".join(negative_reasons) + ".")
    return " ".join(parts)


# ============================================================
# Main
# ============================================================
def main():
    print("=== Plait v3 Scoring Layer ===\n")

    print("Fetching events from PostHog (HogQL)...")
    events = posthog_query(EVENT_QUERY)
    print(f"  {len(events)} events retrieved.\n")

    print("Aggregating with recency decay (14-day half-life)...")
    decayed_intent, raw_counts, latest_ts, earliest_ts = aggregate_events(events)
    print(f"  {len(decayed_intent)} unique domains with intent activity.\n")

    print("Fetching companies from HubSpot...")
    companies = hubspot_get_companies()
    print(f"  {len(companies)} companies retrieved.\n")

    print("Computing v3 scores...")
    updates = []
    tier_counts = defaultdict(int)
    stage_counts = defaultdict(int)

    for c in companies:
        props = c.get("properties", {})
        domain = props.get("domain") or props.get("plait_account_id")
        if not domain:
            continue

        # Inputs
        fit = extract_fit(props.get("plait_signal_summary") or "")
        is_enterprise = props.get("plait_routing_queue") == "AE_QUEUE"
        events_for_domain = raw_counts.get(domain, {})

        # Compute pieces
        stage = classify_stage(events_for_domain)
        intent_decayed_per_event = decayed_intent.get(domain, {})
        intent = compute_intent_score(intent_decayed_per_event)
        composite_pre_cap = compute_composite(fit, intent, stage)

        # Negative signals — apply cap
        composite, neg_reasons = apply_negative_signals(
            composite_pre_cap,
            events_for_domain,
            latest_ts.get(domain),
            earliest_ts.get(domain),
        )

        # Tier — A requires BOTH fit and intent strong, not just composite
        tier = assign_tier(composite, fit=fit, intent=intent)

        # AE_QUEUE override — floor at B (unless negative signals cap them lower)
        # AE leads get B floor regardless of fit/intent gate (enterprise quiet
        # behavior is normal — they're buyers, not users)
        if is_enterprise and tier in ("C", "D") and not neg_reasons:
            tier = "B"

        # Track distributions
        tier_counts[tier] += 1
        stage_counts[stage] += 1

        # Build update
        new_summary = rewrite_summary(
            props.get("plait_signal_summary") or "",
            fit, intent, composite, tier, stage, neg_reasons,
        )
        updates.append({
            "id": c["id"],
            "properties": {
                "plait_composite_score": composite,
                "plait_lead_tier": tier,
                "plait_signal_summary": new_summary,
            },
        })

    # Print distribution summary
    print(f"\nTier distribution:")
    for tier in ["A", "B", "C", "D"]:
        print(f"  {tier}: {tier_counts[tier]}")
    print(f"\nStage distribution:")
    for stage in ["awareness", "trial", "evaluation", "conversion"]:
        print(f"  {stage}: {stage_counts[stage]}")

    print(f"\nUpdating HubSpot...")
    hubspot_batch_update(updates)
    print(f"\nDone. {len(updates)} companies re-scored with v3 model.")
    print("Spot-check tips:")
    print("  - Filter Companies by Plait Lead Tier = A — these are call-today")
    print("  - Open an A-tier company → summary now shows stage and any flags")
    print("  - Look for tier drops on small-co champions — fit + dormancy caps")


if __name__ == "__main__":
    main()
