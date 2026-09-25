"""
Shared utilities for the Fraud Investigation Agent.
- Loads all secrets from .env (never hardcode keys in source).
- Discovers a Groq chat model that this key actually has access to.
- Opens a TigerGraph connection using pyTigerGraph, which handles the
  secret -> REST++ token exchange correctly (this was the bug in the
  original app.py, which sent a random hardcoded bearer token that
  never matched the real secret).
"""

import os
import requests
from dotenv import load_dotenv
from groq import Groq
import pyTigerGraph as tg

load_dotenv()

def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Check that your .env file exists and is loaded from the right directory."
        )
    return value

GROQ_API_KEY = _require_env("GROQ_API_KEY")
TG_HOST = _require_env("PROD_TG_HOST")
TG_USERNAME = _require_env("PROD_TG_USERNAME")
TG_SECRET = _require_env("PROD_TG_SECRET")
TG_GRAPHNAME = os.environ.get("TG_GRAPHNAME", "FraudCaseGraph")

groq_client = Groq(api_key=GROQ_API_KEY)

# Ordered by preference. The code below picks the first one this key
# can actually see - it will NEVER silently fall back to an unrelated
# model (e.g. a safety/classifier model) like the original script did.
PREFERRED_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama3-70b-8192",
    "llama3-8b-8192",
]

_model_cache = {}

def get_working_groq_model():
    """Return a Groq chat model this API key can use, or raise a clear error."""
    if "model" in _model_cache:
        return _model_cache["model"]

    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    response = requests.get(url, headers=headers, timeout=10)

    if response.status_code == 401:
        raise RuntimeError(
            "Groq rejected this API key (401 Unauthorized). "
            "The key is invalid or was revoked - generate a new one at "
            "console.groq.com/keys and put it in .env as GROQ_API_KEY."
        )
    response.raise_for_status()

    available_ids = {m["id"] for m in response.json().get("data", [])}
    for candidate in PREFERRED_MODELS:
        if candidate in available_ids:
            _model_cache["model"] = candidate
            return candidate

    raise RuntimeError(
        f"None of {PREFERRED_MODELS} are available to this key. "
        f"Models this key CAN see: {sorted(available_ids)}. "
        "If that list is empty or missing common models like "
        "llama-3.1-8b-instant, the key itself is bad - rotate it."
    )

_tg_conn = None

def get_tg_connection():
    """Open (and cache) a pyTigerGraph connection, exchanging the secret
    for a REST++ token the way TigerGraph actually requires."""
    global _tg_conn
    if _tg_conn is None:
        resp = requests.post(
            f"{TG_HOST}/gsql/v1/tokens",
            json={"secret": TG_SECRET, "graph": TG_GRAPHNAME},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()["token"]

        conn = tg.TigerGraphConnection(
            host=TG_HOST,
            graphname=TG_GRAPHNAME,
            apiToken=token,
        )
        _tg_conn = conn
    return _tg_conn

def fetch_graph_evidence(txn_id, query_name="DetectFraudRing"):
    """Run the installed GSQL query against the real graph. Returns a
    dict with an 'error' key on failure instead of pretending to have
    live data (no more silent hardcoded mock evidence)."""
    try:
        conn = get_tg_connection()
        results = conn.runInstalledQuery(query_name, params={"seed_txn": (txn_id,)})
        if results:
            return results
        return {"error": "Query executed but returned empty results", "seed_txn": (txn_id,)}
    except Exception as e:
        return {"error": f"Live TigerGraph fetch failed: {e}", "seed_txn": (txn_id,)}
