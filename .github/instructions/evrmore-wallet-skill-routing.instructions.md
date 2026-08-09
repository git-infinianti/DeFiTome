---
description: "Use the evrmore-engineer skill whenever work involves EVRMore engineering, including wallet interactions, RPC usage, address and UTXO flows, asset logic, trading analysis, and on-chain DEX architecture decisions."
---

# EVRMore And Wallet Skill Routing

## Trigger Conditions
Use the evrmore-engineer skill whenever the request or edited files involve any of the following:
- EVRMore network behavior, chain data, RPC methods, or endpoint health
- Wallet operations such as addresses, balances, UTXOs, signatures, or transfers
- EVRMore asset logic, metadata, indexing, or contract-facing integration decisions
- Trading or market-research workflows for EVRMore assets
- On-chain-only DEX design, settlement logic, or risk controls

## Required Workflow
1. Load the skill at .github/skills/evrmore-engineer/SKILL.md.
2. Follow the skill procedure, decision points, and completion criteria.
3. Prefer existing project RPC wrappers before introducing new RPC plumbing.
4. For asset-changing operations related to issuance, reissuance, transfers, settlement, or channel creation, prefer manual raw transaction construction and signing helpers over node-side convenience RPC methods when a raw path exists.
5. Treat this raw-transaction preference as the default design choice for future EVRMore RPC work in this workspace unless the user explicitly requests otherwise.
6. Use the skill assets and script when applicable:
- .github/skills/evrmore-engineer/scripts/rpc_health_check.py
- .github/skills/evrmore-engineer/scripts/local_rpc_backup_probe.py
- .github/skills/evrmore-engineer/assets/trading-session-report-template.md
- .github/skills/evrmore-engineer/assets/dex-architecture-template.md
- .github/skills/evrmore-engineer/assets/dex-threat-model-template.md
- .github/skills/evrmore-engineer/assets/dex-rollout-checklist.md
- .github/skills/evrmore-engineer/references/project-rpc-implementation-backup.md

## Quality Bar
- Outputs should be evidence-backed with reproducible commands or references.
- Risk and failure modes must be explicit for trading and DEX changes.
- If public RPC access is restricted, document constraints and use local node RPC for full checks.
- For RPC implementation incidents, follow the backup runbook and local probe before introducing new RPC plumbing.
