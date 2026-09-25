"""
Case management: builds a fraud investigation case record, progresses it
through the phases the hackathon brief requires (pre-evidence -> evidence
gathered -> post-evidence decision), and writes it back to TigerGraph as
a Case vertex connected to the investigated Transaction.
"""

import datetime
from common import get_tg_connection

def new_case(txn_id, case_id=None):
    """Create a fresh case record. Call this at the start of an investigation."""
    return {
        "case_id": case_id or f"CASE-{txn_id}",
        "txn_id": txn_id,
        "status": "opened",
        "risk_score": None,
        "anomaly_type": None,
        "action_pre_evidence": None,
        "action_post_evidence": None,
        "approval_required": None,
        "sar_required": None,
        "forensic_summary": None,
        "evidence_log": [],
        "decision_log": [],
        "created_at": datetime.datetime.utcnow().isoformat(),
    }

def record_pre_evidence_action(case, action, approval_required):
    """Call this BEFORE requesting additional evidence, per the brief's
    requirement to record the next-best-action before and after evidence."""
    case["action_pre_evidence"] = action
    case["approval_required"] = approval_required
    case["status"] = "pending_evidence"
    case["decision_log"].append({
        "phase": "pre_evidence",
        "action": action,
        "approval_required": approval_required,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    })
    return case

def record_evidence(case, evidence):
    """Attach graph evidence gathered for this case."""
    case["evidence_log"].append({
        "evidence": evidence,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    })
    return case

def record_post_evidence_decision(case, risk_score, anomaly_type, action,
                                   approval_required, sar_required,
                                   forensic_summary):
    """Call this AFTER evidence is gathered and the agent has reasoned
    over it - this is the case's final recommendation for this cycle."""
    case["risk_score"] = risk_score
    case["anomaly_type"] = anomaly_type
    case["action_post_evidence"] = action
    case["approval_required"] = approval_required
    case["sar_required"] = sar_required
    case["forensic_summary"] = forensic_summary
    case["status"] = "action_recommended"
    case["decision_log"].append({
        "phase": "post_evidence",
        "action": action,
        "approval_required": approval_required,
        "sar_required": sar_required,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    })
    return case

def write_case_to_graph(case):
    """Upsert the case as a vertex and link it to its Transaction.
    Requires the Case vertex type and Case_Investigates_Transaction edge
    type to already exist in the schema (added via Design Schema)."""
    conn = get_tg_connection()

    conn.upsertVertex("Case", case["case_id"], {
        "txn_id": case["txn_id"],
        "status": case["status"],
        "risk_score": case["risk_score"],
        "anomaly_type": case["anomaly_type"],
        "action_pre_evidence": case["action_pre_evidence"],
        "action_post_evidence": case["action_post_evidence"],
        "approval_required": case["approval_required"],
        "sar_required": case["sar_required"],
        "forensic_summary": case["forensic_summary"],
        "created_at": case["created_at"],
    })

    conn.upsertEdge(
        "Case", case["case_id"],
        "Case_Investigates_Transaction",
        "Transaction", case["txn_id"],
    )

    return case