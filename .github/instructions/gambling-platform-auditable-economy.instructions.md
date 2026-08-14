---
description: "Use when implementing or documenting casino, gambling, adult-only gaming, betting, wagering, odds, payouts, RTP, house edge, real-money or cash-equivalent chips, deposits, withdrawals, RNG, KYC, AML, responsible gambling, or economy audit trails in DeFiTome."
applyTo:
  - "Tome/**/*.{py,html,md}"
  - "scripts/**/*.py"
  - ".github/docs/**/*.md"
---

# Gambling Platform And Auditable Economy

## Scope And Product Language

- Treat casino features as age-restricted, regulated gambling surfaces. Use accurate terms such as `bet`, `wager`, `stake`, `odds`, `payout`, `RTP`, and `house edge` in game logic, player-facing records, and policy documentation.
- Keep adult-only presentation age-gated and non-explicit. Do not design for or market to minors.
- Do not describe testnet, demo, or non-redeemable assets as real-money or cash-redeemable currency.
- This file supplements, and does not replace, existing EVRMore, raw-transaction, security, and Django project standards.

## Currency And Economy

- Give each wagering currency a clear legal and product classification: fungibility, transferability, purchase path, redemption path, withdrawal eligibility, and applicable restrictions.
- For any production cash-equivalent currency, document deposits, winning payouts, bonuses, promotions, and jackpots as sources; document wagers, entry fees, rake or tips, and non-odds-affecting feature purchases as sinks.
- Keep cosmetics and feature unlocks separate from odds, payouts, and player eligibility.
- Enforce configurable deposit, wager, loss, balance, and withdrawal limits per player and per rolling time window.
- Treat all balances, odds, and payout calculations as server-authoritative. Never trust client-supplied balances, odds, outcomes, or payout amounts.
- Use fixed-precision `Decimal` or integer minor units for monetary values. Never use binary floating point for stakes, odds, fees, or payouts.

## Wager Resolution And Payouts

- Define a versioned, published payout table and odds/RTP disclosure for every game, side bet, bonus round, and jackpot.
- Persist the applied rule version, odds, RTP/house-edge version, stake, payout cap, outcome, and balance delta with each wager.
- Resolve a wager exactly once using an idempotency key and a database or ledger-level concurrency guard before chain or balance mutations.
- Reject a wager before settlement when eligibility, limits, funds, jurisdiction, cooldown, responsible-gambling state, or game availability checks fail.
- Keep odds, payout tables, and house-edge changes versioned and auditable. Never silently alter an active wager's terms.
- Use explicit player-facing results that show the stake, applied odds, resolved outcome, payout, and resulting balance change.

## RNG And Fairness

- Use an approved, production-certified RNG for real-money deployments. Do not claim certification for local, testnet, deterministic, or unreviewed implementations.
- For provably fair games, persist the committed server-seed hash, revealed seed, player seed, nonce, rule version, and deterministic outcome inputs needed for independent verification.
- Disclose the relative effect of chance and skill for every game variant.
- Tie handling, dealer rules, side bets, progressive-jackpot eligibility, and payout caps must be visible in the published game rules before a wager is accepted.
- Preserve historical rule versions so a settled wager remains verifiable after a game implementation changes.

## Immutable Audit Trail

- Append a signed, tamper-evident event for every deposit, wager acceptance, wager rejection, resolution, payout, reversal, adjustment, withdrawal, and balance change.
- Each event must include an internal player identifier, timestamp, event type, correlation or idempotency key, stake, odds, rule version, RNG evidence, payout, balance delta, and external transaction identifiers when applicable.
- Do not overwrite settled financial events. Corrections must be compensating events linked to the original record with an auditable reason.
- For EVRMore asset or EVR settlement, use the established raw-transaction workflow, mempool preflight, and message-channel evidence. Persist broadcast status and reconciliation data independently from the financial settlement.
- Use hash chaining and signatures, or an equivalent independently verifiable mechanism, for audit-log integrity. Record verification failures and reconciliation actions explicitly.

## Player Protection And Eligibility

- Require age verification before gambling activity. Use the jurisdiction-specific legal age configured for the player and market; do not assume one global age threshold.
- Require KYC/AML, sanctions screening, source-of-funds review, and enhanced due diligence at the policy-defined thresholds before production deposits, withdrawals, or high-risk activity.
- Provide player-set and platform-enforced deposit, wager, loss, and session-duration limits; reality checks; cool-off periods; self-exclusion; and a clear route to responsible-gambling resources.
- Enforce self-exclusion and limit changes server-side, including across sessions and devices. Apply any mandated cooling-off period before loosening a player-set limit.
- Detect and investigate multi-accounting, self-referrals, bonus abuse, collusion, automation, arbitrage, replay attempts, abnormal wager patterns, and suspicious withdrawal flows.
- Enforce geo-restrictions and jurisdiction eligibility before accepting a wager or processing a deposit or withdrawal.

## Compliance And Launch Gates

- Keep jurisdiction analysis, licensing status, tax treatment, advertising restrictions, approved KYC/AML providers, responsible-gambling policy, Terms of Service, privacy policy, currency policy, and withdrawal policy under `.github/docs/` until approved for publication.
- Do not enable production purchase, redemption, withdrawal, or cash-equivalent transfer flows until legal and compliance owners approve the target jurisdiction, licensing, age gate, KYC/AML controls, tax handling, and player-protection controls.
- Treat production RNG certification, independent audit evidence, incident response ownership, and regulatory reporting paths as launch blockers, not optional follow-up work.
- Separate testnet and production configuration, credentials, ledgers, assets, endpoints, and user disclosures. Production activation must be explicit and reviewable.

## Testing And Operations

- Add focused tests for stake-to-odds-to-payout-to-balance flows, idempotency, concurrent wager attempts, payout caps, limit enforcement, cooldowns, reconciliation, reversals, and append-only audit events.
- Add integration coverage for deposit-to-wager-to-withdrawal lifecycle states using provider and chain boundaries appropriate to the environment.
- Add abuse, fuzz, and stress tests for malformed odds, negative or overflow values, duplicate requests, race conditions, RNG prediction attempts, bonus abuse, and replay attacks.
- Define severity, customer communication, evidence preservation, reconciliation, and compensating-event procedures for balance defects, RNG defects, chain settlement failures, and payout disputes.
- Log operationally useful context without storing private keys, seed phrases, raw KYC documents, payment credentials, or unnecessary personal data in application logs or chain messages.

## Implementation Checklist

- Before implementing a gambling feature, identify its jurisdiction, eligibility, currency classification, game-rule version, odds/RTP disclosure, payout cap, audit event schema, and failure/reconciliation behavior.
- Before merging, validate all affected limits, balance changes, audit events, fairness proof paths, and player-facing wager records with focused tests.
- Before production launch, require documented legal/compliance approval and evidence that all launch gates above are active.