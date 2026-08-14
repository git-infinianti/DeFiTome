# DEC Poker DEX Valuation And Audit Authority

## RTP Evidence

DEC Poker accepts EVR wagers and pays a separate reward asset. A percentage RTP is published only when a direct, active `REWARD_ASSET/EVR` market has recent `OrderExecution` records with canonical 64-hex settlement transaction IDs that the selected public `/rpc` endpoint can independently resolve with at least one confirmation.

The valuation service uses the most recent 24 hours of eligible executions and stores an immutable volume-weighted average price:

$$
P_{\mathrm{VWAP}} = \frac{\sum_i p_i q_i}{\sum_i q_i}
$$

The published policy stores the execution IDs, transaction IDs, confirmation/block evidence, quantities, prices, time range, VWAP, expected EVR return, and exact payout-policy hash. Its disclosed percentage is:

$$
\operatorname{RTP\%} = \frac{E[\text{reward asset per hand}] \times P_{\mathrm{VWAP}}}{\text{wager EVR}} \times 100
$$

An open bid, `TradingPair.last_price`, a cached balance, or an external price feed never establishes RTP.

## Posting Valuation Liquidity

The configured active DEC audit-authority account must also be the game manager. That manager account funds valuation bids. The authority account itself or a Django superuser acting as an audited delegated operator can submit the Admin action; ordinary staff users cannot. The immutable bid and valuation evidence records the operator, authority, manager, funding account, and whether superuser delegation was used.

The bid workflow:

1. Verifies the authority’s restricted asset, verifier string, qualifier tag, and minimum balance directly against the selected public `/rpc` endpoint.
2. Creates the direct `REWARD_ASSET/EVR` pair when one does not already exist.
3. Rejects any price at or above the best open ask, so creation cannot take liquidity immediately.
4. Verifies live EVR availability and creates the normal `BalanceLock` reservation.
5. Creates a normal DEX buy `LimitOrder` without calling the matching function.

A later seller can match the bid through the ordinary DEX path. Only the resulting settlement execution can contribute to a valuation snapshot.

No live bid should be placed until an operator chooses a maximum price and quantity. The local database currently has no direct valuation market or provisioned restricted authority credential.

## Restricted Audit Authority

The authority configuration is network-scoped and records:

- authority account and wallet-owned address;
- restricted asset name, expected verifier string, and required qualifier tag;
- minimum restricted-asset balance;
- last direct-public-RPC verification evidence;
- an optional flag that requires verification for each new payout-ledger entry.

DEX valuation bids and DEX-valued payout-policy publications always require a currently authorized active authority. When settlement enforcement is enabled, every new ledger event records the authority evidence that permitted it.

## Unified Workflow Channel

Swap, market, and DEC activity use one shared policy: `tome0808_swapflow`, version 5 or later. A usable policy must be active, chain-metadata verified, and declare every lifecycle stage in the immutable metadata:

- `offer_created`
- `market_created`
- `order_created`
- `settlement_lock_created`
- `settlement_build_failed`
- `settlement_pending_reconciliation`
- `settlement_broadcasted`
- `swap_cancelled`
- `swap_expired`
- `game_instance_created`
- `game_spend_recorded`
- `game_reward_distributed`
- `payout_policy_published`

The default v5 testnet channel is `TOME0808~SWAPFLOWV5`, issued under `TOME0808!`. Its strict rules require canonical checksummed event envelopes and fail-closed channel-asset lineage reconciliation. A revision uses a newly named one-unit channel asset with new immutable metadata; issued channel metadata is never changed in place.

The staff messaging console is a cross-user reconciliation surface. It aggregates persisted swap, market, and DEC activity for every user on the selected network, including failed DEC broadcasts. A missing broadcast can therefore be compared with the associated local financial record without treating the channel as a per-user event stream.

A channel asset has one spendable unit, so immediately consecutive lifecycle broadcasts can conflict in the mempool. Failed or missing broadcasts remain visible when the application persisted the associated event, but the system intentionally does not automatically retry or rebroadcast them.

## Database Boundary

Migration `0022_decpoker_market_valuation_and_audit_authority` installs SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers for payout policies, market valuations, valuation bids, and payout-ledger entries. These defend against ORM bulk updates and ordinary SQL mutation after publication. Migration `0023_alter_decpokergameinstance_hand_cooldown_seconds` aligns the model-level default hand buffer with the 30-second service and UI default.

Consequently, a failed DEC instance with a published policy is retained for recovery and audit. It cannot be deleted and recreated under the same reward asset name.

This is not a substitute for database access control. A user with database-file or host-administrator access can remove triggers or alter the authority configuration. The restricted asset is an application authorization credential verified on chain; it cannot independently grant or deny SQLite privileges. Production deployment must additionally restrict database file access, application credentials, host access, and migration privileges.

## Deployment

Apply the migration from `Tome/`:

```bash
/Users/chiefton/Documents/GitHub/DeFiTome/.venv/bin/python manage.py migrate
```

Then configure the authority as a superuser in DEC Poker Admin, verify and activate it against public testnet, post non-crossing liquidity with an explicit limit, wait for eligible settlement execution, and publish the new policy version.