from agent import investigate_case, load_benchmark_cases
import json
import os

# Pick 2-3 case IDs you want for the demo — mix a normal one and a high-risk one
DEMO_CASE_IDS = ["HHG-001", "HHG-002", "HHG-003"]

cases = load_benchmark_cases()
case_lookup = {c["case_id"]: c for c in cases}

# Load existing cache if any, so we don't lose previously completed cases
existing = {}
if os.path.exists("sar_reports.json"):
    with open("sar_reports.json") as f:
        existing = {r["case_id"]: r for r in json.load(f)}

for cid in DEMO_CASE_IDS:
    if cid in existing:
        print(f"⏭️ {cid} already cached, skipping.")
        continue
    row = case_lookup[cid]
    report = investigate_case(
        case_id=row["case_id"],
        txn_id=row["flagged_txn_id"],
        trigger_type=row.get("trigger_type", "fraud_signal"),
        trigger_text=row.get("trigger_text", ""),
        customer_id=row.get("customer_id", ""),
        card_id=row.get("card_id", "")
    )
    existing[cid] = report
    print(f"✅ {cid} done.")

with open("sar_reports.json", "w") as f:
    json.dump(list(existing.values()), f, indent=4)

print("🎉 Demo cases ready.")