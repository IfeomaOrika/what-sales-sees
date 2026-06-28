"""
Plait synthetic event generator
================================

Generates a 400-user synthetic cohort and emits realistic PLG product events
to PostHog's /capture endpoint with backdated timestamps spanning ~45 days.

Persona distribution (from build spec):
  - PQL: 15% (60 users)
  - Tire Kicker: 50% (200 users)
  - Champion at Small Company: 20% (80 users)
  - Enterprise Evaluator: 5% (20 users)
  - Noise buffer: 10% (40 users, simplified in v1)

Usage:
  # Dry run (default — prints events, doesn't send)
  python generate_events.py

  # Send to PostHog
  export POSTHOG_API_KEY=phc_xxxxxxxxxxxx
  export POSTHOG_HOST=https://us.i.posthog.com    # or https://eu.i.posthog.com
  python generate_events.py

Setup:
  pip install requests

Notes:
  - Backdated events use PostHog's historical_migration flag on /batch.
  - Each event is tagged with a `company` group so account-level rollups work
    in the eventual HubSpot sync and sales view.
  - Random seed is fixed for reproducibility. Change RANDOM_SEED to regenerate.
"""

import json
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip install requests")
    sys.exit(1)


# ============================================================
# Config
# ============================================================
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
DRY_RUN = POSTHOG_API_KEY is None
COHORT_SIZE = 400
TIME_WINDOW_DAYS = 45
RANDOM_SEED = 42
BATCH_SIZE = 100  # events per /batch request

random.seed(RANDOM_SEED)

# Anchor "now" — events backdate from here
NOW = datetime.now(timezone.utc)


# ============================================================
# Domain pools — used to give synthetic users realistic email domains.
# These are illustrative, not endorsements. Real domains help PostHog
# look like real product analytics; you can swap to fictional domains
# if preferred.
# ============================================================
MID_SIZE_DOMAINS = [
    "northwind.co", "acmegrowth.io", "loomify.com", "pulsestack.io",
    "harborlytics.com", "tidemark.app", "fernpath.io", "stagecraft.co",
    "yieldhub.com", "anvilbase.io", "wovenpath.com", "lattice-co.io",
    "stackline.app", "everleaf.com", "outpostlabs.io", "bluespan.co",
]

ENTERPRISE_DOMAINS = [
    "globexcorp.com", "meridianholdings.com", "ironforgeenterprises.com",
    "vertexpharma.com", "atlasretail.com", "summitfinancial.com",
    "kestrelbank.com", "magnoliahealth.com",
]

SMALL_COMPANY_DOMAINS = [
    "twobitstudio.com", "soloventures.io", "tinypress.co", "draftboard.io",
    "spareclock.com", "kindlinglabs.com", "saplingstack.io", "tinkershop.co",
]

PERSONAL_DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "icloud.com"]

FIRST_NAMES = ["alex", "sam", "jordan", "casey", "morgan", "riley", "taylor",
               "drew", "quinn", "avery", "blake", "cameron", "dakota", "emerson",
               "finley", "harper", "jamie", "kai", "logan", "parker"]

LAST_NAMES = ["chen", "patel", "rodriguez", "kim", "okafor", "novak", "ahmed",
              "silva", "tran", "kowalski", "ito", "mendez", "fischer", "reyes",
              "obrien", "hassan", "park", "duarte", "lindqvist", "abebe"]


# ============================================================
# Data classes
# ============================================================
@dataclass
class Company:
    company_id: str
    name: str
    domain: str
    size_band: str  # "1-10", "11-50", "51-200", "201-500", "501-2000", "2000+"


@dataclass
class User:
    distinct_id: str
    email: str
    email_type: str  # "work" or "personal"
    user_role: str   # "admin", "member", "viewer"
    signup_source: str  # "organic", "paid", "referral", "community", "direct"
    persona: str
    subtype: Optional[str]  # for tire kicker sub-flavors
    company: Company
    signup_offset_days: int  # how many days before NOW they signed up


# ============================================================
# Cohort generation
# ============================================================
def make_company(size_band: str, domain_pool: list[str]) -> Company:
    domain = random.choice(domain_pool)
    name = domain.split(".")[0].replace("-", " ").title()
    return Company(
        company_id=f"co_{uuid.uuid4().hex[:10]}",
        name=name,
        domain=domain,
        size_band=size_band,
    )


def make_user(persona: str, subtype: Optional[str] = None) -> User:
    """Build a single synthetic user with persona-appropriate attributes."""
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    user_id = f"user_{uuid.uuid4().hex[:10]}"

    if persona == "pql":
        size_band = random.choice(["51-200", "201-500"])
        company = make_company(size_band, MID_SIZE_DOMAINS)
        email = f"{first}.{last}@{company.domain}"
        email_type = "work"
        signup_source = random.choices(
            ["organic", "community", "referral"], weights=[5, 3, 2]
        )[0]
        signup_offset = random.randint(28, 45)

    elif persona == "tire_kicker":
        # Mix of personal and unknown-work domains
        if random.random() < 0.4:
            email = f"{first}.{last}{random.randint(1, 999)}@{random.choice(PERSONAL_DOMAINS)}"
            email_type = "personal"
            company = make_company("1-10", SMALL_COMPANY_DOMAINS)  # placeholder
        else:
            company = make_company(
                random.choice(["11-50", "1-10"]),
                SMALL_COMPANY_DOMAINS + MID_SIZE_DOMAINS,
            )
            email = f"{first}.{last}@{company.domain}"
            email_type = "work"
        signup_source = random.choices(
            ["paid", "organic", "referral"], weights=[5, 3, 2]
        )[0]
        signup_offset = random.randint(1, 45)

    elif persona == "champion_small":
        size_band = "1-10"
        # Some founders use personal email
        if random.random() < 0.3:
            email = f"{first}.{last}@{random.choice(PERSONAL_DOMAINS)}"
            email_type = "personal"
            company = make_company(size_band, SMALL_COMPANY_DOMAINS)
        else:
            company = make_company(size_band, SMALL_COMPANY_DOMAINS)
            email = f"{first}@{company.domain}"
            email_type = "work"
        signup_source = random.choices(["organic", "referral"], weights=[6, 4])[0]
        signup_offset = random.randint(25, 45)

    elif persona == "enterprise_eval":
        size_band = random.choice(["501-2000", "2000+"])
        company = make_company(size_band, ENTERPRISE_DOMAINS)
        email = f"{first}.{last}@{company.domain}"
        email_type = "work"
        signup_source = random.choices(["organic", "direct"], weights=[4, 6])[0]
        signup_offset = random.randint(5, 14)

    else:  # noise — random characteristics
        company = make_company(
            random.choice(["11-50", "51-200", "201-500"]),
            MID_SIZE_DOMAINS,
        )
        email = f"{first}.{last}@{company.domain}"
        email_type = "work"
        signup_source = random.choice(["organic", "paid", "referral"])
        signup_offset = random.randint(1, 45)

    return User(
        distinct_id=user_id,
        email=email,
        email_type=email_type,
        user_role="admin",
        signup_source=signup_source,
        persona=persona,
        subtype=subtype,
        company=company,
        signup_offset_days=signup_offset,
    )


def generate_cohort() -> list[User]:
    """Build the full 400-user cohort with spec-defined distribution."""
    cohort = []

    # PQL — 15% (60)
    cohort.extend(make_user("pql") for _ in range(60))

    # Tire Kicker — 50% (200), split by sub-flavor
    tk_subtypes = (
        ["pure_bounce"] * 70 +
        ["pricing_sniff_bounce"] * 50 +
        ["window_shopper"] * 50 +
        ["false_starter"] * 20 +
        ["ghost"] * 10
    )
    random.shuffle(tk_subtypes)
    cohort.extend(make_user("tire_kicker", subtype=st) for st in tk_subtypes)

    # Champion at Small Company — 20% (80)
    cohort.extend(make_user("champion_small") for _ in range(80))

    # Enterprise Evaluator — 5% (20)
    cohort.extend(make_user("enterprise_eval") for _ in range(20))

    # Noise — 10% (40)
    cohort.extend(make_user("noise") for _ in range(40))

    random.shuffle(cohort)
    return cohort


# ============================================================
# Event helpers
# ============================================================
def ts(user: User, day_offset: int, hour: int = None, minute: int = None) -> str:
    """Build an ISO 8601 timestamp `day_offset` days after the user's signup."""
    base = NOW - timedelta(days=user.signup_offset_days) + timedelta(days=day_offset)
    if hour is None:
        hour = random.randint(8, 22)
    if minute is None:
        minute = random.randint(0, 59)
    dt = base.replace(hour=hour, minute=minute, second=random.randint(0, 59))
    return dt.isoformat()


def base_props(user: User) -> dict:
    """Properties attached to every event for this user."""
    return {
        "$groups": {"company": user.company.company_id},
        "account_id": user.company.company_id,
        "user_role": user.user_role,
        "plan": "free",
    }


def make_event(user: User, event_name: str, day: int, extra_props: dict = None,
               hour: int = None) -> dict:
    """Construct a single event payload."""
    props = base_props(user)
    if extra_props:
        props.update(extra_props)
    props["$insert_id"] = str(uuid.uuid4())  # PostHog dedup key — safe to re-run
    return {
        "event": event_name,
        "distinct_id": user.distinct_id,
        "properties": props,
        "timestamp": ts(user, day, hour=hour),
    }


def group_identify(user: User) -> dict:
    """One-time event that sets company-level properties in PostHog Groups."""
    return {
        "event": "$groupidentify",
        "distinct_id": user.distinct_id,
        "properties": {
            "$group_type": "company",
            "$group_key": user.company.company_id,
            "$group_set": {
                "name": user.company.name,
                "domain": user.company.domain,
                "size_band": user.company.size_band,
            },
            "$insert_id": str(uuid.uuid4()),
        },
        "timestamp": ts(user, 0, hour=8),
    }


def person_identify(user: User) -> dict:
    """Sets email, name, and other user-level properties as PostHog Person properties.
    Required for the HubSpot pipe — HubSpot uses email as the primary Contact identifier."""
    # Reconstruct first/last from the email local part
    local_part = user.email.split("@")[0]
    name_parts = local_part.replace(".", " ").title()
    return {
        "event": "$identify",
        "distinct_id": user.distinct_id,
        "properties": {
            "$set": {
                "email": user.email,
                "name": name_parts,
                "email_type": user.email_type,
                "user_role": user.user_role,
                "signup_source": user.signup_source,
                "company_id": user.company.company_id,
                "company_name": user.company.name,
                "company_domain": user.company.domain,
                "company_size_band": user.company.size_band,
            },
            "$insert_id": str(uuid.uuid4()),
        },
        "timestamp": ts(user, 0, hour=7),  # Fires just before group_identify
    }


# ============================================================
# Persona event sequences
# ============================================================
def events_pql(user: User) -> list[dict]:
    """PQL behavioral arc — see build spec section 4."""
    events = [group_identify(user)]
    e = events.append

    # Day 0 — signup, pricing sniff, activation
    e(make_event(user, "user_signed_up", 0, {
        "signup_source": user.signup_source,
        "email_domain": user.email.split("@")[1],
        "email_type": user.email_type,
    }, hour=9))
    e(make_event(user, "account_created", 0, hour=9))
    e(make_event(user, "pricing_page_viewed", 0, {
        "viewed_plan": "team",
        "time_on_page_seconds": random.randint(15, 45),
    }, hour=9))
    e(make_event(user, "data_source_connected", 0, {
        "source_type": random.choice(["snowflake", "postgres", "bigquery"]),
    }, hour=10))
    e(make_event(user, "first_query_run", 0, {
        "query_type": "sql",
        "rows_returned": random.randint(50, 5000),
        "execution_time_ms": random.randint(200, 3000),
    }, hour=11))

    # Day 0–1 — first dashboard
    e(make_event(user, "first_dashboard_created", random.choice([0, 1]), {
        "creation_method": "sql",
    }))
    e(make_event(user, "dashboard_published", random.choice([0, 1])))

    # Day 2–5 — depth
    for day in range(2, 6):
        for _ in range(random.randint(3, 8)):
            e(make_event(user, "dashboard_viewed", day, {"is_owner": True}))
        for _ in range(random.randint(2, 4)):
            e(make_event(user, "query_run", day, {
                "query_type": "sql",
                "rows_returned": random.randint(50, 5000),
                "execution_time_ms": random.randint(200, 3000),
            }))

    # Day 5–7 — invites
    invite_day = random.randint(5, 7)
    for i in range(random.randint(1, 2)):
        e(make_event(user, "teammate_invited", invite_day, {
            "invited_email_domain": user.company.domain,
            "invite_count_so_far": i + 1,
        }))

    # Day 7–14 — sharing + comments
    e(make_event(user, "dashboard_shared", random.randint(7, 14), {
        "share_type": "slack",
        "recipient_count": random.randint(2, 5),
    }))
    e(make_event(user, "comment_added", random.randint(8, 14)))

    # Day 14–21 — hit the wall
    wall_day = random.randint(14, 21)
    e(make_event(user, "dashboard_limit_reached", wall_day))
    e(make_event(user, "upgrade_modal_opened", wall_day, {"trigger": "hit_limit"}))

    # Day 18–25 — late-stage pricing study + comparison
    e(make_event(user, "pricing_page_viewed", random.randint(18, 25), {
        "viewed_plan": "team",
        "time_on_page_seconds": random.randint(120, 400),  # studying now
    }))
    e(make_event(user, "comparison_page_viewed", random.randint(18, 25), {
        "compared_against": random.choice(["metabase", "looker", "mode"]),
    }))

    # Day 20–28 — case studies
    for _ in range(random.randint(1, 3)):
        e(make_event(user, "case_study_viewed", random.randint(20, 28), {
            "case_study_id": f"cs_{random.randint(1, 20):03d}",
            "industry_match": random.random() < 0.7,
        }))

    # Day 22–28 — billing
    e(make_event(user, "billing_page_viewed", random.randint(22, 28)))

    # Day 25–30 — final diligence
    e(make_event(user, "docs_viewed", random.randint(25, 30), {
        "doc_section": "integrations",
    }))

    return events


def events_tire_kicker(user: User) -> list[dict]:
    """Tire kicker — five sub-flavors handled here."""
    events = [group_identify(user)]
    e = events.append

    signup_props = {
        "signup_source": user.signup_source,
        "email_domain": user.email.split("@")[1],
        "email_type": user.email_type,
    }
    e(make_event(user, "user_signed_up", 0, signup_props, hour=14))
    e(make_event(user, "account_created", 0, hour=14))

    if user.subtype == "pure_bounce":
        # Nothing else. Ever.
        return events

    if user.subtype == "pricing_sniff_bounce":
        e(make_event(user, "pricing_page_viewed", 0, {
            "viewed_plan": "team",
            "time_on_page_seconds": random.randint(8, 25),  # quick look
        }, hour=14))
        e(make_event(user, "dashboard_viewed", 0, {"is_owner": False}, hour=14))
        return events

    if user.subtype == "window_shopper":
        for _ in range(random.randint(1, 2)):
            e(make_event(user, "dashboard_viewed", 0, {"is_owner": False}, hour=14))
        return events

    if user.subtype == "false_starter":
        e(make_event(user, "data_source_connected", 0, {"source_type": "csv"}, hour=15))
        e(make_event(user, "query_run", 0, {
            "query_type": "sql",
            "rows_returned": random.randint(10, 500),
            "execution_time_ms": random.randint(200, 2000),
        }, hour=15))
        e(make_event(user, "first_dashboard_created", 0, {
            "creation_method": "no_code",
        }, hour=16))
        # No publish, no return
        return events

    if user.subtype == "ghost":
        # Brief Day 0
        e(make_event(user, "dashboard_viewed", 0, {"is_owner": False}, hour=14))
        # Returns Day 14–28, no real depth
        revive_day = random.randint(14, 28)
        for _ in range(random.randint(1, 2)):
            e(make_event(user, "dashboard_viewed", revive_day, {"is_owner": False}))
        return events

    return events


def events_champion_small(user: User) -> list[dict]:
    """Champion at small company — high engagement, won't pay."""
    events = [group_identify(user)]
    e = events.append

    e(make_event(user, "user_signed_up", 0, {
        "signup_source": user.signup_source,
        "email_domain": user.email.split("@")[1],
        "email_type": user.email_type,
    }, hour=10))
    e(make_event(user, "account_created", 0, hour=10))
    e(make_event(user, "pricing_page_viewed", 0, {
        "viewed_plan": "team",
        "time_on_page_seconds": random.randint(20, 60),
    }, hour=10))
    e(make_event(user, "data_source_connected", 0, {
        "source_type": random.choice(["postgres", "supabase", "csv"]),
    }, hour=11))
    e(make_event(user, "first_query_run", random.choice([0, 1]), {
        "query_type": "sql",
        "rows_returned": random.randint(20, 2000),
        "execution_time_ms": random.randint(150, 2500),
    }))
    e(make_event(user, "first_dashboard_created", random.choice([1, 2]), {
        "creation_method": random.choice(["sql", "no_code"]),
    }))

    # Heavy individual usage — more than PQL
    for day in range(2, 12):
        for _ in range(random.randint(8, 15)):
            e(make_event(user, "query_run", day, {
                "query_type": "sql",
                "rows_returned": random.randint(20, 2000),
                "execution_time_ms": random.randint(150, 2500),
            }))
        for _ in range(random.randint(5, 12)):
            e(make_event(user, "dashboard_viewed", day, {"is_owner": True}))
        if random.random() < 0.3:
            e(make_event(user, "dashboard_edited", day))

    # 1–2 invites max
    invite_day = random.randint(5, 10)
    for i in range(random.randint(1, 2)):
        e(make_event(user, "teammate_invited", invite_day + i, {
            "invited_email_domain": user.company.domain,
            "invite_count_so_far": i + 1,
        }))

    # Hit the wall — same as PQL
    wall_day = random.randint(15, 22)
    e(make_event(user, "dashboard_limit_reached", wall_day))
    e(make_event(user, "upgrade_modal_opened", wall_day, {"trigger": "hit_limit"}))
    e(make_event(user, "pricing_page_viewed", wall_day, {
        "viewed_plan": "team",
        "time_on_page_seconds": random.randint(60, 180),
    }))

    # ...then comparison shopping for cheaper alternatives
    e(make_event(user, "comparison_page_viewed", wall_day + random.randint(1, 3), {
        "compared_against": "metabase",  # often open-source alternative
    }))

    # Dashboard deletion — the "won't pay" tell
    e(make_event(user, "dashboard_deleted", random.randint(wall_day + 3, wall_day + 8), {
        "dashboard_id": f"db_{uuid.uuid4().hex[:8]}",
        "lifetime_views": random.randint(20, 80),
    }))

    # Continued workaround usage — no billing_page_viewed, no case_study_viewed
    for day in range(wall_day + 1, min(wall_day + 15, 35)):
        for _ in range(random.randint(3, 8)):
            e(make_event(user, "query_run", day, {
                "query_type": "sql",
                "rows_returned": random.randint(20, 2000),
                "execution_time_ms": random.randint(150, 2500),
            }))

    # Docs-heavy throughout
    for _ in range(random.randint(3, 7)):
        e(make_event(user, "docs_viewed", random.randint(1, 25), {
            "doc_section": random.choice(["sql_reference", "troubleshooting", "integrations"]),
        }))

    return events


def events_enterprise_eval(user: User) -> list[dict]:
    """Enterprise evaluator — short, distinctive, SSO-focused."""
    events = [group_identify(user)]
    e = events.append

    # Pre-signup anonymous (still under same distinct_id for v1 simplicity)
    e(make_event(user, "case_study_viewed", 0, {
        "case_study_id": f"cs_{random.randint(1, 20):03d}",
        "industry_match": True,
    }, hour=10))
    e(make_event(user, "pricing_page_viewed", 0, {
        "viewed_plan": "business",  # Note: business, not team
        "time_on_page_seconds": random.randint(60, 180),
    }, hour=10))

    e(make_event(user, "user_signed_up", 0, {
        "signup_source": user.signup_source,
        "email_domain": user.email.split("@")[1],
        "email_type": user.email_type,
    }, hour=11))
    e(make_event(user, "account_created", 0, hour=11))

    # The override trigger
    e(make_event(user, "docs_viewed", 0, {"doc_section": "sso"}, hour=11))

    # Security/compliance pass
    for section in random.sample(["soc2", "audit_logs", "data_residency", "gdpr"], 3):
        e(make_event(user, "docs_viewed", random.randint(1, 3), {"doc_section": section}))

    # Token activation
    e(make_event(user, "data_source_connected", random.randint(1, 3), {
        "source_type": "csv",
    }))

    # Light dashboard activity
    for _ in range(random.randint(2, 3)):
        e(make_event(user, "dashboard_viewed", random.randint(2, 5), {"is_owner": True}))

    # Stack-fit check
    for integration in random.sample(["salesforce", "snowflake", "slack"], 2):
        e(make_event(user, "integration_page_viewed", random.randint(3, 7), {
            "integration_name": integration,
        }))

    # Conversion event
    conv_day = random.randint(5, 10)
    if random.random() < 0.5:
        e(make_event(user, "demo_requested", conv_day, {
            "requested_plan": "business",
            "note_provided": True,
            "company_size_at_request": user.company.size_band,
        }))
    else:
        e(make_event(user, "contact_sales_clicked", conv_day, {
            "surface": "pricing_page",
        }))

    return events


def events_noise(user: User) -> list[dict]:
    """Noise buffer — light, random, intentionally messy."""
    events = [group_identify(user)]
    e = events.append

    e(make_event(user, "user_signed_up", 0, {
        "signup_source": user.signup_source,
        "email_domain": user.email.split("@")[1],
        "email_type": user.email_type,
    }))
    e(make_event(user, "account_created", 0))

    # Random light activity — some activate, some don't
    if random.random() < 0.6:
        e(make_event(user, "data_source_connected", 0, {
            "source_type": random.choice(["postgres", "csv", "bigquery"]),
        }))
        for day in range(1, random.randint(3, 15)):
            if random.random() < 0.4:
                e(make_event(user, "dashboard_viewed", day, {"is_owner": True}))
            if random.random() < 0.3:
                e(make_event(user, "query_run", day, {
                    "query_type": "sql",
                    "rows_returned": random.randint(10, 1000),
                    "execution_time_ms": random.randint(100, 2000),
                }))

    return events


# ============================================================
# Dispatcher
# ============================================================
def events_for_user(user: User) -> list[dict]:
    # Person identify fires first — gives every user an email/name in PostHog
    identify = [person_identify(user)]
    if user.persona == "pql":
        return identify + events_pql(user)
    if user.persona == "tire_kicker":
        return identify + events_tire_kicker(user)
    if user.persona == "champion_small":
        return identify + events_champion_small(user)
    if user.persona == "enterprise_eval":
        return identify + events_enterprise_eval(user)
    return identify + events_noise(user)


# ============================================================
# PostHog sender
# ============================================================
def send_batch(events: list[dict], batch_num: int, total_batches: int) -> bool:
    """Send a batch of historical events to PostHog. Returns True on success."""
    if DRY_RUN:
        for ev in events[:3]:
            print(json.dumps(ev, indent=2))
        if len(events) > 3:
            print(f"... ({len(events) - 3} more events in this batch)")
        return True

    payload = {
        "api_key": POSTHOG_API_KEY,
        "historical_migration": True,
        "batch": events,
    }
    url = f"{POSTHOG_HOST.rstrip('/')}/batch/"

    max_retries = 4
    backoff = 2.0

    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 401:
                # Auth errors won't fix themselves — fail loudly and stop.
                print(f"\nAuth error: {r.text[:200]}")
                print("Set POSTHOG_API_KEY correctly and try again.")
                sys.exit(1)
            if r.status_code >= 300:
                print(f"  Batch {batch_num}/{total_batches}: HTTP {r.status_code} "
                      f"({r.text[:120]}). Retrying in {backoff:.0f}s...")
                time.sleep(backoff)
                backoff *= 2
                continue
            print(f"  Batch {batch_num}/{total_batches}: sent {len(events)} events")
            return True
        except (requests.ConnectionError, requests.Timeout) as e:
            err_brief = str(e)[:80]
            if attempt < max_retries - 1:
                print(f"  Batch {batch_num}/{total_batches}: network issue "
                      f"({err_brief}). Retry {attempt + 1}/{max_retries - 1} "
                      f"in {backoff:.0f}s...")
                time.sleep(backoff)
                backoff *= 2
            else:
                print(f"  Batch {batch_num}/{total_batches}: FAILED after "
                      f"{max_retries} attempts.")
                return False
    return False


# ============================================================
# Main
# ============================================================
def main():
    mode = "DRY RUN" if DRY_RUN else f"LIVE → {POSTHOG_HOST}"
    print(f"=== Plait synthetic event generator ({mode}) ===\n")

    cohort = generate_cohort()
    print(f"Generated cohort of {len(cohort)} users\n")

    # Persona breakdown
    from collections import Counter
    breakdown = Counter(u.persona for u in cohort)
    for persona, count in breakdown.items():
        print(f"  {persona}: {count}")
    print()

    # Generate all events
    all_events = []
    for user in cohort:
        all_events.extend(events_for_user(user))
    print(f"Generated {len(all_events)} total events\n")

    # Sort by timestamp so they arrive in PostHog in order
    all_events.sort(key=lambda e: e["timestamp"])

    # Send in batches
    total_batches = (len(all_events) + BATCH_SIZE - 1) // BATCH_SIZE
    failed = 0
    for i in range(0, len(all_events), BATCH_SIZE):
        batch = all_events[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        success = send_batch(batch, batch_num, total_batches)
        if not success:
            failed += 1
        if not DRY_RUN:
            time.sleep(0.2)  # gentle pacing — avoids overwhelming the API

    if DRY_RUN:
        print("\nDry run complete. Set POSTHOG_API_KEY to send to PostHog.")
    else:
        sent = total_batches - failed
        print(f"\nDone. {sent}/{total_batches} batches sent successfully.")
        if failed > 0:
            print(f"WARNING: {failed} batches failed. Safe to re-run — "
                  f"$insert_id prevents duplicates.")


if __name__ == "__main__":
    main()
