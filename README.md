# Future Makers - Agentic Fraud Investigation Agent

An AI agent for the TigerGraph Agentic Fraud Investigation hackathon. It investigates fraud cases end-to-end: gathers evidence from a TigerGraph knowledge graph, assesses risk and uncertainty, requests additional evidence when needed, recommends next-best actions, and writes findings back to the graph as case memory.

## Architecture

- **Intelligence Engine**: Groq API (`openai/gpt-oss-120b`) - used for reasoning, evidence synthesis, and generating explanations
- **Agent Framework**: Custom Python implementation (no external agent framework) - direct API calls and orchestration logic in `agent.py`
- **Graph Database**: TigerGraph Cloud (Savanna) - stores transactions, accounts, devices, identities, and case records; used for graph traversal and relationship analysis via GSQL
- **Interface**: Streamlit dashboard (`app.py`) - Case Queue, risk score, uncertainty before/after evidence, decision and approval routing, and explainability panel
- **Output**: Case records and Suspicious Activity Reports (SARs) in JSON, written to both local files and the graph

## How it works

1. **Trigger** - investigation starts from a risk score, customer report, or analyst request
2. **Investigate** - agent opens or creates a case and gathers evidence from the graph (transaction history, connected accounts, device signals, prior cases)
3. **Assess** - determines the fraud pattern, risk level, and confidence
4. **Request more evidence** if uncertainty is too high (e.g. step-up authentication, customer validation)
5. **Recommend next action** - allow, block, monitor, or escalate, routed for approval per policy
6. **Explain** - documents the evidence used and reasoning behind the decision
7. **Update case memory** - writes the case back to the graph so future investigations can learn from it

## Project structure

```
app.py                      # Streamlit UI
agent.py                    # Core agent logic
case_manager.py             # Case creation and progression
common.py                   # Shared utilities
diagnose_graph_write.py     # Graph write diagnostics
case_pack.csv               # Benchmark case triggers
identity.csv                # Device / identity signals
closed_cases_history.csv    # Prior closed cases (case memory source)
transactions_pruned.csv     # Transaction dataset
answers/                    # One JSON answer file per benchmark case
    case_1.json ... case_20.json
```

Each file in `answers/` contains, per case: the internal investigation record (evidence, findings, decisions, actions), a Suspicious Activity Report when required by policy, and the next-best-action with required approval route, recorded both before and after any additional evidence was requested.

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root with:

```
PROD_TG_HOST=your_tigergraph_host
PROD_TG_SECRET=your_tigergraph_secret
GROQ_API_KEY=your_groq_api_key
```

`.env` is excluded from version control via `.gitignore` - never commit real credentials.

## Run

```bash
streamlit run app.py
```

Open the app at `http://localhost:8501` and select a benchmark case from the Case Queue to view its investigation, evidence, risk assessment, decision routing, and explainability.

## Demo video

[Add link to your demo video here]

## Dataset

Built on the IEEE-CIS Fraud Detection dataset (Vesta Corporation), extended with the bank's fraud policy, known fraud patterns, and 20 benchmark cases used for evaluation.

## Team

Future Makers - Kaushik S, Nishan M, Muthu Karthigai Selvam S - VSB Engineering College
