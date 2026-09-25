import os
import re
import csv
import json
import time
import shutil
import argparse
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

TG_HOST = os.getenv("PROD_TG_HOST")
TG_SECRET = os.getenv("PROD_TG_SECRET")
TG_GRAPH = os.getenv("PROD_TG_GRAPH", "FraudCaseGraph")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = "openai/gpt-oss-120b"

PIPELINE_VERSION = "v2-grounded"
REPORTS_FILE = "sar_reports.json"
ANSWERS_DIR = "answers"  # one answer file per case
EVIDENCE_RESPONSES_FILE = "evidence_responses.json"  # optional: mock replies to evidence requests, keyed by case_id

_TG_TOKEN_CACHE = {"token": None}


# ----------------------------------------------------------------------------
# TigerGraph access
# ----------------------------------------------------------------------------
def get_tg_token(force_refresh=False):
    if _TG_TOKEN_CACHE["token"] and not force_refresh:
        return _TG_TOKEN_CACHE["token"]
    url = f"{TG_HOST}/gsql/v1/tokens"
    payload = {"secret": TG_SECRET, "lifetime": 999999, "graph": TG_GRAPH}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print(f"⚠️ Token request failed [{resp.status_code}]: {resp.text[:300]}")
            resp.raise_for_status()
        token = resp.json().get("token")
        _TG_TOKEN_CACHE["token"] = token
        return token
    except Exception as e:
        print(f"⚠️ Failed to get TigerGraph token: {e}")
        return None


def tg_get(query_name, params, retries=2, timeout=30):
    """Call a TigerGraph REST query with a longer timeout and one retry on failure.
    A 401/403 is treated as an expired/invalid token: fetch a fresh one and retry
    immediately, without burning through the normal retry/backoff budget."""
    token = get_tg_token()
    if not token:
        return None
    url = f"{TG_HOST}/restpp/query/{TG_GRAPH}/{query_name}"
    for attempt in range(retries):
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code in (401, 403):
                print(f"⚠️ TigerGraph query {query_name} got {resp.status_code} "
                      f"(likely expired token) — refreshing token and retrying...")
                token = get_tg_token(force_refresh=True)
                if not token:
                    return None
                continue
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            print(f"⚠️ TigerGraph query {query_name} failed (attempt {attempt+1}/{retries}): {e}")
            time.sleep(2)
    return None


def get_fraud_ring_evidence(txn_id):
    return tg_get("DetectFraudRing", {"seed_txn": txn_id})


def get_similar_closed_cases(pattern_hint):
    if not pattern_hint:
        return []
    result = tg_get("FindSimilarClosedCases", {"pattern_hint": pattern_hint})
    return result if result else []


def write_case_to_graph(case_id, trigger_type, trigger_text, risk_score):
    """Returns {'ok': bool, 'error': str|None} so callers can surface *why* a write failed
    instead of just a bare True/False."""
    token = get_tg_token()
    if not token:
        msg = "No auth token (check PROD_TG_HOST / PROD_TG_SECRET / PROD_TG_GRAPH)"
        print(f"⚠️ Failed to write case {case_id} to graph: {msg}")
        return {"ok": False, "error": msg}
    url = f"{TG_HOST}/restpp/query/{TG_GRAPH}/WriteCaseRecord"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "case_id": case_id,
        "trigger_type": trigger_type,
        "trigger_text": trigger_text,
        "risk_score": risk_score if risk_score is not None else 0.0,
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code in (401, 403):
            print(f"⚠️ Graph write for {case_id} got {resp.status_code} "
                  f"(likely expired token) — refreshing token and retrying once...")
            token = get_tg_token(force_refresh=True)
            if not token:
                return {"ok": False, "error": "Token refresh failed after 401/403"}
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get(url, params=params, headers=headers, timeout=30)
        if not resp.ok:
            msg = f"HTTP {resp.status_code}: {resp.text[:500]}"
            print(f"⚠️ Failed to write case {case_id} to graph: {msg}")
            return {"ok": False, "error": msg}
        body = resp.json()
        if body.get("error"):
            msg = f"GSQL error: {body.get('message', body)}"
            print(f"⚠️ Failed to write case {case_id} to graph: {msg}")
            return {"ok": False, "error": msg}
        return {"ok": True, "error": None}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"⚠️ Failed to write case {case_id} to graph: {msg}")
        return {"ok": False, "error": msg}


# ----------------------------------------------------------------------------
# LLM
# ----------------------------------------------------------------------------
def _parse_json(text):
    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def call_llm(prompt, max_retries=5):
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODEL_NAME, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
    for attempt in range(max_retries):
        try:
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", 8)), 30)
                print(f"   Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            if not content or not content.strip():
                print(f"   Attempt {attempt+1}: empty response body, retrying...")
                time.sleep(3)
                continue
            return _parse_json(content)
        except Exception as e:
            print(f"   LLM attempt {attempt+1} failed: {e}")
            time.sleep(3)
    return None


# ----------------------------------------------------------------------------
# Actions and approval policy
# ----------------------------------------------------------------------------
ACTIONS = [
    "allow", "block_transaction", "step_up_authentication", "request_customer_validation",
    "monitor_account", "block_account", "warn_customer", "file_sar",
    "escalate_to_analyst", "request_more_evidence",
]

# PLACEHOLDER RULES: replace APPROVAL_RULES with the rules from the dataset's fraud policy document.
POLICY_SOURCE = "internal approval matrix (action-tier: auto-execute / analyst approval / risk-committee approval)"
AUTO, ANALYST, COMMITTEE = "auto_execute", "analyst_approval", "risk_committee_approval"
APPROVAL_RULES = {
    "allow": AUTO,
    "monitor_account": AUTO,
    "step_up_authentication": AUTO,
    "request_customer_validation": AUTO,
    "request_more_evidence": AUTO,
    "warn_customer": AUTO,
    "escalate_to_analyst": ANALYST,
    "block_transaction": ANALYST,
    "file_sar": ANALYST,
    "block_account": COMMITTEE,
}
_ROUTE_RANK = {AUTO: 0, ANALYST: 1, COMMITTEE: 2}

_ACTION_KEYWORDS = [
    (("sar", "suspicious activity"), "file_sar"),
    (("freeze", "block account", "block_account", "suspend account", "close account"), "block_account"),
    (("block", "decline", "reject"), "block_transaction"),
    (("step-up", "step up", "authenticat", "2fa", "mfa", "otp"), "step_up_authentication"),
    (("validat", "confirm with", "verify with", "contact customer"), "request_customer_validation"),
    (("monitor", "watch"), "monitor_account"),
    (("warn", "notify", "alert customer"), "warn_customer"),
    (("more evidence", "additional evidence", "additional information"), "request_more_evidence"),
    (("hold", "escalat", "manual review", "analyst", "review"), "escalate_to_analyst"),
    (("allow", "clear", "approve", "release", "legitimate"), "allow"),
]


def normalize_action(text):
    """Map the model's free-text action onto the fixed action set (unknown -> escalate)."""
    t = str(text or "").strip().lower()
    if t in ACTIONS:
        return t
    for keywords, action in _ACTION_KEYWORDS:
        if any(k in t for k in keywords):
            return action
    return "escalate_to_analyst"


def route_for_action(action, risk_score=None, sar_required=False):
    """Approval route depends on the ACTION (and SAR need), not just the score."""
    route = APPROVAL_RULES.get(action, ANALYST)
    reason = f"'{action}' routes to {route} ({POLICY_SOURCE})."
    if sar_required and _ROUTE_RANK[route] < _ROUTE_RANK[ANALYST]:
        route = ANALYST
        reason += " SAR required, so analyst sign-off is needed."
    if risk_score is None and route == AUTO:
        route = ANALYST
        reason = "No risk score available; not auto-authorizing."
    return {"approval_route": route, "requires_human_approval": route != AUTO, "approval_reason": reason}


# ----------------------------------------------------------------------------
# Evidence summary + grounding checks
# ----------------------------------------------------------------------------
def summarize_evidence(evidence, txn_id):
    """Turn raw DetectFraudRing output into compact facts. Returns (facts, known_ids)."""
    block = evidence[0] if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict) else {}
    connected = block.get("ConnectedEntities", []) or []
    ring = block.get("FraudRing", []) or []
    cards = [str(v.get("v_id")) for v in connected if v.get("v_type") == "Card"]
    identities = [v for v in connected if v.get("v_type") == "Identity"]

    populated = set()
    for ident in identities:
        for key, val in (ident.get("attributes") or {}).items():
            if key != "TransactionID" and val not in ("", 0, None):
                populated.add(key)

    ring_txns = [str(v.get("v_id")) for v in ring if str(v.get("v_id")) != str(txn_id)]
    not_available = ["transaction amounts", "transaction timestamps", "IP addresses",
                     "shipping/billing addresses", "other customers' account identifiers"]
    if not ({"DeviceInfo", "DeviceType"} & populated):
        not_available.append("device identifiers")

    facts = {
        "flagged_transaction": str(txn_id),
        "cards_linked": cards,
        "other_transactions_on_same_card_or_identity": len(ring_txns),
        "sample_of_those_transaction_ids": ring_txns[:10],
        "identity_records_linked": len(identities),
        "identity_fields_with_values": sorted(populated),
        "not_available_in_evidence": not_available,
        "caveat": "The 'ring' is only other transactions sharing a card/identity; it is not proof of fraud.",
    }
    known_ids = {str(txn_id)} | set(cards) | set(ring_txns)
    for v in connected:
        known_ids.add(str(v.get("v_id")))
    return facts, known_ids


IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
DEV_RE = re.compile(r"\bDEV-\w+\b", re.I)
NUM_RE = re.compile(r"(?<![\w.])\d{5,}(?!\w|\.\d)")
ADDRESS_RE = re.compile(r"\b\d{1,5}\s+[A-Z]\w*(?:\s+[A-Z]\w*)*\s+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Drive|Dr)\b")
PLACEHOLDER_ACCT_RE = re.compile(r"(?i:accounts?)\s*\(?\s*[A-E](?:\s*,\s*[A-E])+\b")


def find_ungrounded(text, known_ids, allowed_extra=()):
    """Flag identifiers/details in the text that do not appear in the evidence."""
    issues = []
    allowed = set(known_ids) | set(allowed_extra)
    for ip in IP_RE.findall(text):
        if ip not in allowed:
            issues.append(f"IP address {ip}")
    for dev in DEV_RE.findall(text):
        if dev not in allowed:
            issues.append(f"device id {dev}")
    for num in NUM_RE.findall(text):
        if num not in allowed:
            issues.append(f"identifier {num}")
    if ADDRESS_RE.search(text):
        issues.append("street address")
    if PLACEHOLDER_ACCT_RE.search(text):
        issues.append("placeholder account labels (A, B, C...)")
    return issues


def _tokens_from(*values):
    out = set()
    for v in values:
        out.update(re.findall(r"[A-Za-z0-9\-]+", str(v or "")))
    return out


def fallback_summary(facts, trigger_text):
    return (
        f"Flagged transaction {facts['flagged_transaction']} (trigger: {trigger_text}). "
        f"Linked card(s): {', '.join(facts['cards_linked']) or 'none'}; "
        f"{facts['other_transactions_on_same_card_or_identity']} other transactions share the same card or identity. "
        f"The graph evidence does not include {', '.join(facts['not_available_in_evidence'])}, "
        f"so a fraud pattern cannot be confirmed from graph data alone; analyst review is recommended."
    )


# ----------------------------------------------------------------------------
# Controlled evidence request (MOCK)
# ----------------------------------------------------------------------------
def request_additional_evidence(case_id, evidence_type):
    """MOCK controlled action (e.g. customer validation / step-up auth).
    If evidence_responses.json has an entry for the case, it is used as the reply;
    otherwise the simulated party does not respond."""
    responses = {}
    if os.path.exists(EVIDENCE_RESPONSES_FILE):
        with open(EVIDENCE_RESPONSES_FILE, encoding="utf-8") as f:
            responses = json.load(f)
    if case_id in responses:
        return {"requested": evidence_type, "status": "received", "response": responses[case_id],
                "source": f"{EVIDENCE_RESPONSES_FILE} (mock)"}
    return {"requested": evidence_type, "status": "no_response", "response": None,
            "source": "mock_customer_validation_api"}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _to_int(value, default=None):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _failed_report(base, reason, log):
    route = route_for_action("escalate_to_analyst", None)
    log.append({"step": "failed", "time": _now(), "detail": reason})
    report = dict(base)
    report.update({
        "status": "failed", "risk_score": None, "anomaly_type": "investigation_incomplete",
        "uncertainty_before": "High", "uncertainty_after": "High",
        "action_before_evidence": "escalate_to_analyst", "action_after_evidence": "escalate_to_analyst",
        "approval_route_before_evidence": "analyst_approval", "approval_route_after_evidence": "analyst_approval",
        "requires_human_approval": True, "approval_reason": route["approval_reason"],
        "sar_required": False, "similar_past_cases_found": 0,
        "forensic_summary": f"Investigation incomplete: {reason}. Manual review required.",
        "decision_log": log,
    })
    # risk_score -1 marks 'not assessed' in the graph (0 would look like low risk)
    graph_write = write_case_to_graph(base["case_id"], base["trigger_type"], base["trigger_text"], -1)
    report["graph_write_ok"] = graph_write["ok"]
    report["graph_write_error"] = graph_write["error"]
    return report


# ----------------------------------------------------------------------------
# Investigation
# ----------------------------------------------------------------------------
def investigate_case(case_id, txn_id, trigger_type="fraud_signal", trigger_text="", customer_id="", card_id=""):
    print(f"🔍 Investigating Case #{case_id} (txn {txn_id}, trigger: {trigger_type})...")
    log = []

    def step(name, detail):
        log.append({"step": name, "time": _now(), "detail": detail})

    base = {
        "case_id": case_id, "flagged_txn_id": txn_id, "trigger_type": trigger_type,
        "trigger_text": trigger_text, "customer_id": customer_id, "card_id": card_id,
        "pipeline_version": PIPELINE_VERSION, "policy_source": POLICY_SOURCE,
    }
    step("trigger", f"{trigger_type}: {trigger_text}")

    evidence = get_fraud_ring_evidence(txn_id)
    if evidence is None:
        return _failed_report(base, f"TigerGraph query failed for txn {txn_id}", log)

    facts, known_ids = summarize_evidence(evidence, txn_id)
    step("graph_evidence", {k: facts[k] for k in ("cards_linked", "other_transactions_on_same_card_or_identity",
                                                   "identity_fields_with_values")})
    allowed_extra = _tokens_from(trigger_text, customer_id, card_id, case_id)
    facts_json = json.dumps(facts)

    # --- PHASE 1: assessment from graph evidence only ---
    phase1_prompt = f"""
You are a fraud investigation agent reviewing a newly triggered case.
Trigger type: {trigger_type}
Trigger detail: {trigger_text}
Customer: {customer_id}   Card: {card_id}
Graph evidence facts (this is ALL the evidence you have): {facts_json}

STRICT RULES:
- Use only the facts above. Do NOT invent devices, IPs, addresses, account names, amounts or timestamps.
- If something is listed under not_available_in_evidence, treat it as unknown and reflect that in your uncertainty.
- Choose action_before_evidence from exactly this list: {ACTIONS}

Return ONLY JSON with:
- "risk_score": integer 0-100
- "anomaly_type": short pattern name supported by the facts
- "uncertainty": "High", "Medium", or "Low"
- "action_before_evidence": one item from the list
- "needs_more_evidence": true/false
- "evidence_to_request": what additional evidence would resolve the uncertainty (or "")
"""
    phase1 = call_llm(phase1_prompt)
    if phase1 is None:
        return _failed_report(base, "LLM phase 1 failed", log)

    action_before = normalize_action(phase1.get("action_before_evidence"))
    risk_before = _to_int(phase1.get("risk_score"))
    route_before = route_for_action(action_before, risk_before)
    step("phase1_assessment", {"risk_score": risk_before, "anomaly_type": phase1.get("anomaly_type"),
                               "uncertainty": phase1.get("uncertainty"), "action": action_before,
                               "approval_route": route_before["approval_route"]})

    # --- Gather more evidence: case memory + controlled evidence request ---
    similar_cases = get_similar_closed_cases(phase1.get("anomaly_type"))
    step("case_memory", f"{len(similar_cases)} similar closed case(s) retrieved")

    evidence_request = None
    if _truthy(phase1.get("needs_more_evidence")):
        evidence_request = request_additional_evidence(case_id, phase1.get("evidence_to_request") or "customer_validation")
        step("evidence_request", evidence_request)

    # --- PHASE 2: final decision with all evidence ---
    phase2_prompt = f"""
You previously assessed this case: {json.dumps(phase1)}
Graph evidence facts: {facts_json}
Case memory: {len(similar_cases)} similar closed case(s) retrieved. Details: {json.dumps(similar_cases)[:3000]}
Additional evidence request and outcome: {json.dumps(evidence_request)}

STRICT RULES:
- Cite only entities and IDs present in the graph evidence facts, the trigger, or the case memory.
- Do NOT invent devices, IPs, addresses, account names, amounts or timestamps. Say "not available" when unknown.
- Do not state how many similar cases were found (that is recorded separately).
- If the additional evidence request got no response, say the uncertainty remains.
- Choose action_after_evidence from exactly this list: {ACTIONS}

Return ONLY JSON with:
- "risk_score": integer 0-100
- "anomaly_type": final pattern name
- "uncertainty": "High", "Medium", or "Low"
- "action_after_evidence": one item from the list
- "sar_required": true/false
- "forensic_summary": at most 120 words explaining the evidence used, what is unknown, and why the action fits
"""
    phase2 = call_llm(phase2_prompt)
    degraded = phase2 is None
    if degraded:
        phase2 = {"risk_score": risk_before, "anomaly_type": phase1.get("anomaly_type"),
                  "uncertainty": phase1.get("uncertainty"), "action_after_evidence": action_before,
                  "sar_required": False, "forensic_summary": ""}

    # --- Grounding check on the explanation ---
    summary = str(phase2.get("forensic_summary") or "")
    grounding = "passed"
    issues = find_ungrounded(summary, known_ids, allowed_extra)
    if issues or not summary.strip():
        step("grounding_check_failed", issues or ["empty summary"])
        retry = call_llm(
            f"Rewrite this fraud case summary. It contains details that are NOT in the evidence: {issues}.\n"
            f"Evidence facts: {facts_json}\nTrigger: {trigger_text}\nOriginal summary: {summary}\n"
            f'Use only the evidence and trigger; say "not available" for unknowns. '
            f'Return ONLY JSON: {{"forensic_summary": "..."}} (max 120 words).'
        ) if summary.strip() else None
        new_summary = str((retry or {}).get("forensic_summary") or "")
        if new_summary.strip() and not find_ungrounded(new_summary, known_ids, allowed_extra):
            summary, grounding = new_summary, "regenerated"
        else:
            summary, grounding = fallback_summary(facts, trigger_text), "template_fallback"
        step("grounding_resolution", grounding)

    risk = _to_int(phase2.get("risk_score"), risk_before)
    action_after = normalize_action(phase2.get("action_after_evidence"))
    sar_required = _truthy(phase2.get("sar_required"))
    route_after = route_for_action(action_after, risk, sar_required)
    step("phase2_decision", {"risk_score": risk, "uncertainty": phase2.get("uncertainty"), "action": action_after,
                             "sar_required": sar_required, "approval_route": route_after["approval_route"]})

    report = dict(base)
    report.update({
        "status": "degraded" if degraded else "completed",
        "risk_score": risk,
        "anomaly_type": phase2.get("anomaly_type"),
        "uncertainty_before": phase1.get("uncertainty"),
        "uncertainty_after": phase2.get("uncertainty"),
        "action_before_evidence": action_before,
        "approval_route_before_evidence": route_before["approval_route"],
        "needs_more_evidence": _truthy(phase1.get("needs_more_evidence")),
        "evidence_request": evidence_request,
        "action_after_evidence": action_after,
        "approval_route_after_evidence": route_after["approval_route"],
        "requires_human_approval": route_after["requires_human_approval"],
        "approval_reason": route_after["approval_reason"],
        "sar_required": sar_required,
        "similar_past_cases_found": len(similar_cases),
        "case_memory_note": f"{len(similar_cases)} similar closed case(s) retrieved from the graph.",
        "forensic_summary": summary,
        "grounding_status": grounding,
        "evidence_facts": facts,
        "decision_log": log,
    })
    graph_write = write_case_to_graph(case_id, trigger_type, trigger_text, risk or 0)
    report["graph_write_ok"] = graph_write["ok"]
    report["graph_write_error"] = graph_write["error"]
    if not graph_write["ok"]:
        step("graph_write_failed", graph_write["error"])
    print(f"✅ Case #{case_id}: before='{action_before}' → after='{action_after}' "
          f"(route: {route_after['approval_route']}, grounding: {grounding})")
    return report


# ----------------------------------------------------------------------------
# Persistence + batch run
# ----------------------------------------------------------------------------
def load_benchmark_cases(path="case_pack.csv"):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_reports():
    if os.path.exists(REPORTS_FILE):
        with open(REPORTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_report(report):
    """Write the per-case answer file and update the combined reports file."""
    os.makedirs(ANSWERS_DIR, exist_ok=True)
    with open(os.path.join(ANSWERS_DIR, f"{report['case_id']}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    reports = [r for r in load_reports() if r.get("case_id") != report["case_id"]]
    reports.append(report)
    with open(REPORTS_FILE, "w", encoding="utf-8") as f:
        json.dump(reports, f, indent=4)


def analyze_fraud_cases(only=None, force=False):
    print("🚀 Fraud Investigation Agent Online!")
    cases = load_benchmark_cases()
    print(f"📋 Loaded {len(cases)} official benchmark cases from case_pack.csv")

    existing = load_reports()
    if existing and any(r.get("pipeline_version") != PIPELINE_VERSION for r in existing):
        backup = "sar_reports_v1_backup.json"
        if not os.path.exists(backup):
            shutil.copy(REPORTS_FILE, backup)
        print(f"⚠️ Older-format reports found (backed up to {backup}); they will be regenerated.")

    done_ids = set() if force else {
        r["case_id"] for r in existing
        if r.get("pipeline_version") == PIPELINE_VERSION and r.get("status") == "completed"
    }
    if done_ids:
        print(f"⏭️ Skipping {len(done_ids)} completed case(s): {sorted(done_ids)}")

    for row in cases:
        if only and row["case_id"] not in only:
            continue
        if row["case_id"] in done_ids:
            continue
        report = investigate_case(
            case_id=row["case_id"],
            txn_id=row["flagged_txn_id"],
            trigger_type=row.get("trigger_type", "fraud_signal"),
            trigger_text=row.get("trigger_text", ""),
            customer_id=row.get("customer_id", ""),
            card_id=row.get("card_id", ""),
        )
        save_report(report)
        time.sleep(8)  # stay clear of Groq's per-minute limit

    reports = load_reports()
    print(f"\n🎉 {len(reports)} case report(s) on file; "
          f"{sum(1 for r in reports if r.get('status') != 'completed')} not fully completed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="Run only these case ids, e.g. --only HHG-005")
    parser.add_argument("--force", action="store_true", help="Re-run even completed cases")
    args = parser.parse_args()
    analyze_fraud_cases(only=set(args.only) if args.only else None, force=args.force)