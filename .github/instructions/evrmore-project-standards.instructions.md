---
applyTo: "**/*.{py,html,md}"
description: "Use when working on DeFiTome EVRMore wallet, RPC, asset, template, or message-channel code. Enforces consistent project styling, approved RPC usage, HDWallet-based EVR address handling, full EVR package usage, and blockchain message-channel logging."
---

# DeFiTome EVRMore Project Standards

## Scope
Apply these rules to EVRMore wallet logic, RPC integrations, asset flows, template work, and any DeFi, Listing, API, or Wallet changes that touch blockchain behavior or user-facing views.

## Template and UI Consistency
- Keep styling consistent across all project templates.
- Reuse the established Django template structure, class names, spacing, color palette, and visual patterns already used in the project before introducing new markup or CSS patterns.
- Prefer existing project conventions over ad hoc template styling.
- Do not add one-off UI patterns that differ from the repository's current design language.

## Approved RPC Usage
- Only use whitelisted Evrmore RPC commands that are already exposed through the project wrappers in [Tome/Wallet/rpc.py](Tome/Wallet/rpc.py) and [Tome/Tome/rpc_client.py](Tome/Tome/rpc_client.py).
- Do not call raw RPC methods directly from ad hoc code paths unless the command is explicitly approved and added to the project wrapper first.
- Keep RPC access centralized through the established wrapper interface instead of introducing new one-off clients or duplicate command helpers.
- For every blockchain operation, prefer the repository's canonical RPC abstraction and validation flow over direct low-level calls.
- Runtime routing supports only `public` and `local` endpoint modes; do not add a third endpoint mode or force a single backend from application logic.
- For live test validation, instantiate the canonical `PublicRpcClient` against the selected public `/rpc` endpoint and execute the validation commands directly, so the evidence cannot come from a local fallback.

## EVR Address and Key Management
- Always use `HDWallet` from the `hdwallet` package for EVR address generation, derivation, and key management in Python.
- Do not hand-roll address derivation, private key generation, or key recovery logic when the project already has a supported `HDWallet` flow.
- Use the wallet helper and derivation conventions already present in the project rather than introducing alternative EVR address code paths.
- For EVR wallet work, stay aligned with the existing `HDWallet`-based implementation used by the repository.

## EVR Package Usage
- Use the EVR-related packages already present in this codebase and project dependency stack instead of reinventing equivalent behavior.
- Favor the existing stack for crypto, wallet, and EVR-specific functionality, including `hdwallet`, `evrmore-rpc`, `evrmore_authentication`, and the repo’s wallet/auth utilities.
- Keep EVR integration logic in the same package patterns and conventions already used by the project.

## Message Channel Logging
- Always use the message channel system to record operational and transactional information on the EVR blockchain when the event is relevant to a channel or workflow.
- Prefer the project helpers in [Tome/API/message_channel_lib.py](Tome/API/message_channel_lib.py) and the DeFi message-channel integration patterns over informal ad hoc logging.
- Use the channel payload validation and event helper functions when emitting status updates, swap stages, order events, and workflow logs.
- Log persisted blockchain-facing state through the canonical message channel payload format rather than plain unstructured status-only messages.

## Default Implementation Guidance
- Reuse current project patterns before introducing new abstractions.
- Prefer small, explicit changes that match the repository's established EVR, wallet, and API styles.
- When adding a new EVR RPC or wallet feature, first confirm the existing wrapper pattern and extend that pattern instead of creating parallel code paths.
- Keep changes auditable, traceable, and consistent with the DeFiTome EVRMore architecture.
