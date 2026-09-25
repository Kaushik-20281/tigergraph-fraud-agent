import streamlit as st
import json
import csv
import os
from agent import investigate_case, save_report, REPORTS_FILE

st.set_page_config(page_title="Future Makers | Agentic Fraud Investigator", layout="wide")
st.title("🕵️‍♂️ Future Makers: Agentic Fraud Investigation Agent")


@st.cache_data
def load_cases():
    with open("case_pack.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@st.cache_data
def load_cached_reports():
    if os.path.exists(REPORTS_FILE):
        with open(REPORTS_FILE, encoding="utf-8") as f:
            return {r["case_id"]: r for r in json.load(f)}
    return {}


def route_label(route):
    return {
        "auto_execute": "Auto-execute (no approval needed)",
        "analyst_approval": "Needs fraud analyst approval",
        "risk_committee_approval": "Needs risk committee approval",
    }.get(route, route or "n/a")


cases = load_cases()
case_lookup = {c["case_id"]: c for c in cases}
cached_reports = load_cached_reports()

st.sidebar.header("Case Queue (IEEE-CIS Benchmark)")
selected_case_id = st.sidebar.selectbox("Select Benchmark Case:", list(case_lookup.keys()))
selected = case_lookup[selected_case_id]

st.sidebar.markdown(f"**Trigger:** {selected['trigger_type']}")
st.sidebar.caption(selected["trigger_text"])

cached = cached_reports.get(selected_case_id)
if cached and cached.get("status") == "completed":
    st.sidebar.success("✅ Already investigated (saved record available)")
else:
    st.sidebar.warning("⏳ Not yet investigated — will run live")

use_saved = st.sidebar.checkbox("Load saved record when available", value=True)

if st.button("🚀 Run Agent Investigation"):
    if use_saved and cached and cached.get("status") == "completed":
        report = cached
        st.caption("✅ Loaded from saved investigation record")
    else:
        with st.spinner("Investigating: graph evidence → assessment → case memory / evidence request → final decision..."):
            report = investigate_case(
                case_id=selected["case_id"],
                txn_id=selected["flagged_txn_id"],
                trigger_type=selected.get("trigger_type", "fraud_signal"),
                trigger_text=selected.get("trigger_text", ""),
                customer_id=selected.get("customer_id", ""),
                card_id=selected.get("card_id", ""),
            )
        save_report(report)
        load_cached_reports.clear()

    if report.get("status") != "completed":
        st.warning(f"Case status: **{report.get('status', 'unknown')}** — treat as needing analyst review.")

    top1, top2, top3 = st.columns(3)
    top1.metric("Risk score", f"{report.get('risk_score')}/100" if report.get("risk_score") is not None else "N/A")
    top2.metric("Uncertainty before evidence", report.get("uncertainty_before") or "n/a")
    top3.metric("Uncertainty after evidence", report.get("uncertainty_after") or "n/a")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📋 Decision & Approval Routing")
        st.info(
            f"**BEFORE additional evidence:** `{report.get('action_before_evidence')}`\n\n"
            f"Approval route: {route_label(report.get('approval_route_before_evidence'))}"
        )
        req = report.get("evidence_request")
        if req:
            st.write(f"**Evidence requested:** {req.get('requested')} → status: `{req.get('status')}` "
                     f"(source: {req.get('source')})")
        else:
            st.caption("No additional evidence was requested.")
        st.success(
            f"**AFTER additional evidence:** `{report.get('action_after_evidence')}`\n\n"
            f"Approval route: {route_label(report.get('approval_route_after_evidence'))}"
        )
        st.caption(report.get("approval_reason", ""))

        if report.get("sar_required"):
            st.error("🚨 Suspicious Activity Report (SAR) required")
            st.download_button(
                "📥 Download SAR",
                json.dumps(report, indent=2),
                file_name=f"SAR_{report['case_id']}.json",
            )

    with col2:
        st.subheader("🔍 Explainability & Case Memory")
        st.write(report.get("forensic_summary"))
        st.write(f"**Case memory:** {report.get('case_memory_note', str(report.get('similar_past_cases_found', 0)) + ' similar past cases')}")
        st.caption(f"Grounding check: {report.get('grounding_status', 'n/a')} · "
                   f"Written to graph: {report.get('graph_write_ok', 'n/a')}")
        if report.get("graph_write_ok") is False:
            st.error(f"Graph write failed: {report.get('graph_write_error', 'unknown error')}")

    if report.get("evidence_facts"):
        with st.expander("Graph evidence used"):
            st.json(report["evidence_facts"])
    if report.get("decision_log"):
        with st.expander("Case timeline (decision log)"):
            for entry in report["decision_log"]:
                st.write(f"`{entry['time']}` **{entry['step']}** — {entry['detail']}")

    with st.expander("Full case report (JSON)"):
        st.json(report)