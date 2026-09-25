"""
Standalone diagnostic for the TigerGraph case write.

Run this from the SAME directory (and same virtualenv) that Streamlit runs
`app.py` from, so it sees the same .env file and environment:

    python diagnose_graph_write.py

It does two things, step by step, and prints the raw response at each step
so we can see exactly where it breaks (bad secret, wrong graph name, query
not installed, param type mismatch, etc.) instead of just "Failed".
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

TG_HOST = os.getenv("PROD_TG_HOST")
TG_SECRET = os.getenv("PROD_TG_SECRET")
TG_GRAPH = os.getenv("PROD_TG_GRAPH", "FraudCaseGraph")

print("=" * 70)
print("STEP 0: Environment")
print("=" * 70)
print("PROD_TG_HOST   :", TG_HOST or "❌ NOT SET")
print("PROD_TG_GRAPH  :", TG_GRAPH)
print("PROD_TG_SECRET :", "set (hidden)" if TG_SECRET else "❌ NOT SET")

if not TG_HOST or not TG_SECRET:
    raise SystemExit(
        "\nStop here — one or more env vars are missing. "
        "Confirm a .env file exists in this directory and defines "
        "PROD_TG_HOST and PROD_TG_SECRET (and optionally PROD_TG_GRAPH)."
    )

print("\n" + "=" * 70)
print("STEP 1: Request an auth token")
print("=" * 70)
try:
    tok_resp = requests.post(
        f"{TG_HOST}/gsql/v1/tokens",
        json={"secret": TG_SECRET, "lifetime": 999999, "graph": TG_GRAPH},
        timeout=15,
    )
except Exception as e:
    raise SystemExit(f"❌ Could not reach {TG_HOST}: {type(e).__name__}: {e}")

print("Status code:", tok_resp.status_code)
print("Response body:", tok_resp.text[:800])

if not tok_resp.ok:
    raise SystemExit(
        "\nStop here — the token request itself failed. Common causes:\n"
        "  - PROD_TG_SECRET is wrong, expired, or revoked (Savanna free-tier "
        "secrets can expire)\n"
        "  - PROD_TG_GRAPH doesn't match the graph the secret was created for\n"
        "  - PROD_TG_HOST is wrong or the instance is stopped (check Savanna "
        "auto-stop/auto-start)"
    )

token = tok_resp.json().get("token")
if not token:
    raise SystemExit(f"❌ Token request returned 200 but no token in body: {tok_resp.text[:500]}")

print("✅ Got a token.")

print("\n" + "=" * 70)
print("STEP 2: Call WriteCaseRecord with a throwaway test case")
print("=" * 70)
try:
    q_resp = requests.get(
        f"{TG_HOST}/restpp/query/{TG_GRAPH}/WriteCaseRecord",
        params={
            "case_id": "DIAG-TEST",
            "trigger_type": "diagnostic",
            "trigger_text": "diagnose_graph_write.py test write",
            "risk_score": 1,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
except Exception as e:
    raise SystemExit(f"❌ Request to WriteCaseRecord failed: {type(e).__name__}: {e}")

print("Status code:", q_resp.status_code)
print("Response body:", q_resp.text[:1500])

if not q_resp.ok:
    print(
        "\n⚠️ Non-2xx response. Common causes:\n"
        "  - WriteCaseRecord is not installed/published on this graph\n"
        "  - The query exists but expects different parameter names or types\n"
        "    (GSQL is strict: an INT parameter fed a float like 1.0 can be "
        "rejected)\n"
        "  - REST++ endpoint disabled or a permissions issue on this token"
    )
else:
    body = q_resp.json()
    if body.get("error"):
        print(f"\n⚠️ Query ran but returned a GSQL-level error: {body.get('message', body)}")
    else:
        print("\n✅ Write appears to have succeeded. Verify in GSQL shell with:")
        print(f'   USE GRAPH {TG_GRAPH}')
        print('   SELECT c FROM CaseRecord:c WHERE c.case_id == "DIAG-TEST";')

print("\n" + "=" * 70)
print("Done.")
print("=" * 70)