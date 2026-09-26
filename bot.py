"""
magicpin AI Challenge — Vera bot
---------------------------------
Deterministic compose() + a FastAPI server exposing the 5 required endpoints:
  POST /v1/context
  POST /v1/tick
  POST /v1/reply
  GET  /v1/healthz
  GET  /v1/metadata

Run locally:
    pip install fastapi uvicorn
    uvicorn bot:app --host 0.0.0.0 --port 8080

Everything is in-memory (fine per the testing brief — no restarts during the test).
"""

import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
START = time.time()

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}

# conversation_id -> {"turns": [...], "merchant_id":..., "customer_id":...,
#                      "sent_bodies": set(), "unanswered_nudges": int}
conversations: dict[str, dict] = {}

# suppression_key -> last_sent_at (ISO string). Prevents re-sending the same
# nudge for the same reason every tick.
suppressed: dict[str, str] = {}

TEAM_METADATA = {
    "team_name": "Solo Builder",
    "team_members": ["Your Name"],
    "model": "deterministic-templates (no external LLM call required)",
    "approach": "Template-based composer keyed by trigger.kind, grounded in "
                "category voice + merchant facts. No LLM dependency, fully deterministic.",
    "contact_email": "you@example.com",
    "version": "1.0.0",
    "submitted_at": datetime.now(timezone.utc).isoformat(),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_ctx(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def make_suppression_key(*parts: str) -> str:
    """Deterministic suppression key. If the trigger already carries one, prefer
    that (it's the source of truth from the judge). Otherwise derive one."""
    raw = ":".join(str(p) for p in parts if p)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def first_name(merchant: dict) -> str:
    identity = merchant.get("identity", {})
    return identity.get("owner_first_name") or identity.get("name", "there")


def uses_hindi_mix(merchant: dict) -> bool:
    langs = merchant.get("identity", {}).get("languages", [])
    return "hi" in langs


def salutation(merchant: dict) -> str:
    """Category-aware salutation (dentists get 'Dr. X', others get first name)."""
    identity = merchant.get("identity", {})
    slug = merchant.get("category_slug", "")
    name = identity.get("owner_first_name") or identity.get("name", "")
    if slug == "dentists":
        return f"Dr. {name}" if name else "Doctor"
    return name or "there"


def pick_digest_item(category: dict, trigger: dict) -> Optional[dict]:
    """Resolve the digest item referenced by a research_digest / regulation_change
    trigger. Never invents one — if it's not in the category context, return None."""
    top_item_id = trigger.get("payload", {}).get("top_item_id")
    digest = category.get("digest", [])
    if top_item_id:
        for item in digest:
            if item.get("id") == top_item_id:
                return item
    return digest[0] if digest else None


# ---------------------------------------------------------------------------
# compose() — the deterministic message engine
# ---------------------------------------------------------------------------
# Each handler returns (body, cta) and never fabricates a fact: every number,
# date or price is pulled directly from category/merchant/trigger/customer.
# If a needed field is missing, the handler falls back to a safer, more
# generic (but still true) phrasing rather than inventing the gap.


def _h_research_digest(category, merchant, trigger, customer):
    item = pick_digest_item(category, trigger)
    if not item:
        return (
            f"{salutation(merchant)}, there's a new item in this week's "
            f"{category.get('display_name', category.get('slug'))} research digest. "
            f"Want me to pull the details relevant to your patient mix?",
            "open_ended",
        )
    segment = item.get("patient_segment", "").replace("_", " ")
    n = item.get("trial_n")
    title = item.get("title", "")
    source = item.get("source", "")
    if segment and not segment.endswith(("patients", "adults", "users", "customers")):
        segment = f"{segment} patients"
    seg_clause = f" relevant to your {segment}" if segment else ""
    n_clause = f"{n:,}-patient trial — " if isinstance(n, int) else ""
    body = (
        f"{salutation(merchant)}, this week's digest landed. One item{seg_clause} — "
        f"{n_clause}{title[0].lower() + title[1:] if title else 'worth a look'}. "
        f"Want me to pull it + draft a patient-ed WhatsApp you can share?"
        + (f"  — {source}" if source else "")
    )
    return body, "open_ended"


def _h_regulation_change(category, merchant, trigger, customer):
    item = pick_digest_item(category, trigger)
    deadline = trigger.get("payload", {}).get("deadline_iso", "")
    deadline_clause = f" Effective {deadline[:10]}." if deadline else ""
    title = item.get("title", "a regulation change") if item else "a regulation change"
    body = (
        f"{salutation(merchant)}, heads up — {title[0].lower() + title[1:] if title else title}."
        f"{deadline_clause} Want me to send the compliance checklist so you're covered?"
    )
    return body, "open_ended"


def _h_recall_due(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    service = payload.get("service_due", "your recall").replace("_", " ")
    slots = payload.get("available_slots", [])
    active_offer = next((o for o in merchant.get("offers", []) if o.get("status") == "active"), None)
    name = customer.get("identity", {}).get("name", "there") if customer else "there"
    merchant_name = merchant.get("identity", {}).get("name", "the clinic")

    slot_txt = ""
    if len(slots) >= 2:
        slot_txt = f"{slots[0].get('label')} ya {slots[1].get('label')}. " if uses_hindi_mix(merchant) else \
                    f"{slots[0].get('label')} or {slots[1].get('label')}. "
    elif len(slots) == 1:
        slot_txt = f"{slots[0].get('label')}. "

    offer_txt = f"{active_offer.get('title')}" if active_offer else "your next visit"

    if uses_hindi_mix(merchant):
        body = (
            f"Hi {name}, {merchant_name} here 🦷 Aapka {service} due hai. "
            f"Apke liye slots ready hain: {slot_txt}{offer_txt}. "
            f"Reply 1 for the first slot, 2 for the second, ya batayein koi aur time chalega."
        )
    else:
        body = (
            f"Hi {name}, {merchant_name} here. Your {service} is due. "
            f"Available slots: {slot_txt}{offer_txt}. "
            f"Reply 1 for the first, 2 for the second, or tell us a time that works."
        )
    return body, "open_ended"


def _h_perf_dip(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    metric = payload.get("metric", "performance")
    delta = payload.get("delta_pct")
    window = payload.get("window", "recent")
    delta_txt = f"{abs(delta) * 100:.0f}%" if isinstance(delta, (int, float)) else "noticeably"
    body = (
        f"{salutation(merchant)}, your {metric} dropped {delta_txt} over the last {window} — "
        f"worth a look before it compounds. I can check if it's a listing issue or a demand dip. "
        f"Want me to dig in?"
    )
    return body, "open_ended"


def _h_perf_spike(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    metric = payload.get("metric", "views")
    delta = payload.get("delta_pct")
    delta_txt = f"+{delta * 100:.0f}%" if isinstance(delta, (int, float)) else "up sharply"
    body = (
        f"{salutation(merchant)}, your {metric} are {delta_txt} right now — good moment to make sure "
        f"your active offers and photos are current so you convert the extra traffic. Want me to check?"
    )
    return body, "open_ended"


def _h_renewal_due(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    days = payload.get("days_remaining")
    plan = payload.get("plan", merchant.get("subscription", {}).get("plan", "your plan"))
    amount = payload.get("renewal_amount")
    days_txt = f"{days} days" if days is not None else "soon"
    amt_txt = f" (₹{amount:,})" if isinstance(amount, (int, float)) else ""
    body = (
        f"{salutation(merchant)}, your {plan} plan renews in {days_txt}{amt_txt}. "
        f"Want me to lock in the renewal now so there's no gap in your listing visibility?"
    )
    return body, "yes_no"


def _h_festival(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    festival = payload.get("festival_name", payload.get("name", "the festival"))
    days = payload.get("days_until")
    days_txt = f"in {days} days" if days is not None else "coming up"
    active_offer = next((o for o in merchant.get("offers", []) if o.get("status") == "active"), None)
    offer_txt = f" Your {active_offer.get('title')} is already active — want me to push it as the festival special?" \
        if active_offer else " Want me to draft a festival post using one of your catalog offers?"
    body = f"{salutation(merchant)}, {festival} is {days_txt}.{offer_txt}"
    return body, "open_ended"


def _h_dormant(category, merchant, trigger, customer):
    body = (
        f"{salutation(merchant)}, haven't heard from you in a bit — no pressure. "
        f"Quick one when you have 2 min: what's the one thing you'd want me to fix on your listing first?"
    )
    return body, "open_ended"


def _h_review_theme(category, merchant, trigger, customer):
    themes = merchant.get("review_themes", [])
    neg = next((t for t in themes if t.get("sentiment") == "neg"), None)
    if not neg:
        return (
            f"{salutation(merchant)}, a theme is coming up in your recent reviews — want me to summarize it?",
            "open_ended",
        )
    theme = neg.get("theme", "an issue").replace("_", " ")
    n = neg.get("occurrences_30d")
    n_txt = f"{n} reviews this month" if n else "a few recent reviews"
    body = (
        f"{salutation(merchant)}, {n_txt} mention {theme}. Small fix now could stop it becoming a pattern — "
        f"want me to draft a response template + a fix checklist?"
    )
    return body, "open_ended"


def _h_curious_ask(category, merchant, trigger, customer):
    body = (
        f"Hi {salutation(merchant)}! Quick check — what service has been most asked-for this week? "
        f"I'll turn the answer into a Google post + a short WhatsApp reply you can reuse. Takes 5 min."
    )
    return body, "open_ended"


def _h_trend_movement(category, merchant, trigger, customer):
    payload = trigger.get("payload", {})
    query = payload.get("query", "a relevant search term")
    delta = payload.get("delta_yoy")
    delta_txt = f"+{delta * 100:.0f}% YoY" if isinstance(delta, (int, float)) else "trending up"
    body = (
        f"{salutation(merchant)}, \"{query}\" searches are {delta_txt} in your city. "
        f"Worth checking if your listing/offers cover this — want me to check the gap?"
    )
    return body, "open_ended"


def _h_default(category, merchant, trigger, customer):
    kind = trigger.get("kind", "an update")
    body = (
        f"{salutation(merchant)}, quick note on {kind.replace('_', ' ')} — "
        f"want me to walk you through what changed and what (if anything) to do about it?"
    )
    return body, "open_ended"


HANDLERS = {
    "research_digest": _h_research_digest,
    "category_research_digest_release": _h_research_digest,
    "regulation_change": _h_regulation_change,
    "recall_due": _h_recall_due,
    "customer_lapsed_soft": _h_recall_due,
    "perf_dip": _h_perf_dip,
    "perf_spike": _h_perf_spike,
    "renewal_due": _h_renewal_due,
    "festival_upcoming": _h_festival,
    "dormant_with_vera": _h_dormant,
    "review_theme_emerged": _h_review_theme,
    "scheduled_recurring": _h_curious_ask,
    "category_trend_movement": _h_trend_movement,
}


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Deterministic compose(). Same inputs -> same output, always."""
    kind = trigger.get("kind", "")
    handler = HANDLERS.get(kind, _h_default)
    body, cta = handler(category, merchant, trigger, customer)

    send_as = "merchant_on_behalf" if customer is not None else "vera"
    suppression_key = trigger.get("suppression_key") or make_suppression_key(
        merchant.get("merchant_id", ""), kind, category.get("slug", "")
    )
    rationale = (
        f"kind={kind}; category={category.get('slug')}; "
        f"merchant_signals={merchant.get('signals', [])}; "
        f"customer_state={(customer or {}).get('state')}"
    )

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# /v1/reply — simple deterministic intent detection
# ---------------------------------------------------------------------------

ACCEPT_WORDS = {"yes", "ok", "okay", "sure", "go ahead", "haan", "theek hai", "done",
                "let's do it", "lets do it", "please", "1", "2", "send", "yes please"}
DECLINE_WORDS = {"no", "not interested", "stop", "nahi", "no thanks", "not now", "later"}
AUTO_REPLY_MARKERS = {"thank you for contacting", "we will get back", "team tak pahuncha",
                       "automated assistant", "this is an automated"}


def detect_intent(conv: dict, message: str) -> str:
    msg_lower = message.strip().lower()

    # auto-reply: same verbatim message seen 3+ times in this conversation, OR
    # it matches a known canned-reply phrase.
    same_count = sum(1 for t in conv["turns"] if t["from"] == "merchant" and t["msg"].strip().lower() == msg_lower)
    if same_count >= 2:  # this is the 3rd occurrence including the current one
        return "auto_reply"
    if any(marker in msg_lower for marker in AUTO_REPLY_MARKERS):
        return "auto_reply"

    if any(word in msg_lower for word in DECLINE_WORDS):
        return "decline"
    if "?" in message:
        return "question"
    if any(word in msg_lower for word in ACCEPT_WORDS):
        return "accept"
    return "unclear"


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        if scope in counts:
            counts[scope] += 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return TEAM_METADATA


@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": now_iso(),
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers[:20]:  # respect the 20-actions/tick cap
        trigger = get_ctx("trigger", trg_id)
        if not trigger:
            continue

        # suppression check — don't resend the same nudge for the same reason
        supp_key = trigger.get("suppression_key")
        if supp_key and supp_key in suppressed:
            continue

        merchant_id = trigger.get("merchant_id")
        merchant = get_ctx("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue

        category_slug = merchant.get("category_slug")
        category = get_ctx("category", category_slug) if category_slug else None
        if not category:
            continue

        customer = None
        customer_id = trigger.get("customer_id")
        if customer_id:
            customer = get_ctx("customer", customer_id)

        composed = compose(category, merchant, trigger, customer)

        conversation_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:6]}"
        conversations[conversation_id] = {
            "turns": [],
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "sent_bodies": {composed["body"]},
            "unanswered_nudges": 1,
        }

        if supp_key:
            suppressed[supp_key] = body.now

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [salutation(merchant)],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = conversations.setdefault(body.conversation_id, {
        "turns": [], "merchant_id": body.merchant_id, "customer_id": body.customer_id,
        "sent_bodies": set(), "unanswered_nudges": 0,
    })
    conv["turns"].append({"from": body.from_role, "msg": body.message})

    intent = detect_intent(conv, body.message)

    if intent == "auto_reply":
        result = {
            "action": "end",
            "rationale": "Detected canned/auto-reply pattern (same message repeated or "
                         "matched a known auto-reply phrase); exiting to avoid wasting turns.",
        }
    elif intent == "decline":
        result = {
            "action": "end",
            "rationale": "Merchant signaled not interested; gracefully exiting.",
        }
    elif intent == "accept":
        next_body = "Great — sending that over now. Anything specific you'd like me to prioritize first?"
        if next_body in conv["sent_bodies"]:
            next_body = "On it. I'll follow up here once it's ready."
        conv["sent_bodies"].add(next_body)
        result = {
            "action": "send",
            "body": next_body,
            "cta": "open_ended",
            "rationale": "Honoring the merchant's accept; advancing with a low-friction next step.",
        }
    elif intent == "question":
        next_body = "Good question — happy to clarify. Could you tell me a bit more about what you'd like to know?"
        result = {
            "action": "send",
            "body": next_body,
            "cta": "open_ended",
            "rationale": "Merchant asked a question; staying on-mission and inviting specifics rather than guessing.",
        }
    else:
        conv["unanswered_nudges"] = conv.get("unanswered_nudges", 0) + 1
        if conv["unanswered_nudges"] >= 3:
            result = {
                "action": "end",
                "rationale": "3 unanswered/unclear nudges; gracefully exiting per stop-condition.",
            }
        else:
            result = {
                "action": "wait",
                "wait_seconds": 1800,
                "rationale": "Reply unclear; backing off 30 min instead of pushing again immediately.",
            }

    return result
