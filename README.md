# DeFi Tome

DeFi Tome is a testnet-stage Django application for wallet operations, native
asset management, peer-to-peer trading, and chain exploration on EVRMore. The
project is focused on native EVR and EVRMore assets: no wrapped assets, bridges,
or off-chain custody are required for its settlement workflows.

> **Project status:** Active testnet alpha. The wallet, asset, market, and
> atomic-swap workflows are implemented and covered by automated tests, but the
> application has not completed an independent security audit and is not ready
> for production custody or mainnet settlement.

## Product Direction

DeFi Tome is evolving into an operational console for EVRMore rather than a
collection of disconnected protocol demos. The near-term priorities are:

1. Harden native raw-transaction workflows on testnet.
2. Make settlement, recovery, and chain reconciliation observable and safe.
3. Complete a security review and controlled testnet release.
4. Promote proven workflows to mainnet incrementally.
5. Expand lending, oracle, and liquidity features only after the trading and
   custody foundations are mature.

## Implemented Features

### Wallet and Authentication

- Django registration, login, email verification, and session management.
- EVRMore address authentication using signed, one-time challenges.
- HD wallet creation, derived address profiles, backup, QR receive, and
  transaction history views.
- Mainnet/testnet selection and public/local RPC endpoint routing.
- Stored balance snapshots so normal page rendering does not depend on a live
  node response.
- Optional SafeTrade credential storage and member-profile synchronization.

### Native EVRMore Transactions and Assets

- Manual raw transaction construction and signing for EVR transfers, asset
  transfers, issuance operations, and atomic settlement.
- `testmempoolaccept` preflight before transaction broadcast.
- EVR, asset, and authorization-token change returned to the source address.
- Mempool-aware UTXO selection and relay-fee-floor enforcement.
- Tracked main, sub, unique, messaging, qualifier, and restricted asset roles.
- Admin asset-creation workspace with dry-run/preflight support.
- IPFS-backed asset metadata and unique-asset mint requests.

### Atomic Swaps and Markets

- P2P atomic-swap offers containing exactly one native unique asset.
- Settlement in EVR or a tracked fungible main/sub-asset.
- Single raw atomic settlement transactions for asset-to-EVR and
  asset-to-asset swaps.
- Funding locks, expiry cleanup, cancellation, reconciliation state, and
  network isolation.
- Fungible market registry and order book with limit, market, and stop-loss
  order workflows.
- Canonical routes at `/markets/` and `/defi/p2p/available/`, with redirects
  retained for older listing URLs.

### Messaging Channels and Metadata Governance

- Canonical `ROOT~CHANNEL` assets for governed console workflows.
- Raw messaging-channel issuance and raw transfer-with-message lifecycle events.
- Schema-versioned IPFS metadata bound to the asset's on-chain CID.
- Chain metadata verification, policy revisions, deprecation, subscription,
  and scan/export administration.
- Atomic swaps fail closed when no active, verified channel covers the full
  lifecycle or when a required event cannot be published.

### Explorer, Media, API, and Interface

- Block, transaction, asset, output, and address-oriented explorer views.
- Kubo/IPFS upload, preview, edit, and deletion workflows.
- RIP-0010 address metadata tags and verification.
- API key management and a restricted RPC procedure catalog.
- API surfaces for chain reads, assets, contracts, and message-channel
  administration.
- Responsive operational dashboard and shared desktop/mobile visual system.

## Experimental Surfaces

The repository contains models, views, and tests for liquidity pools, price
feeds, lending positions, fixed-rate bonds, variable savings, fee distribution,
and contract records. These surfaces are research and testnet work in progress;
they are not production protocols and should not be treated as audited financial
infrastructure.

## Architecture

```text
DeFiTome/
├── Tome/
│   ├── Tome/       # Project settings, canonical URLs, routed RPC client
│   ├── User/       # Accounts, email verification, wallet authentication
│   ├── Wallet/     # HD wallets, UTXOs, raw transactions, asset operations
│   ├── Listings/   # Market registry, order book, listings, NFT records
│   ├── DeFi/       # Atomic swaps and experimental protocol modules
│   ├── Explorer/   # Chain explorer views and normalized chain data
│   ├── Media/      # IPFS files and RIP-0010 address metadata
│   ├── API/        # API keys, RPC catalog, messaging channel policies
│   ├── Settings/   # User, network, RPC, and appearance preferences
│   └── static/     # Shared operational interface theme
├── scripts/        # Local-node diagnostics and raw transaction probes
└── .github/
    ├── docs/       # Maintainer and protocol implementation references
    └── skills/     # EVRMore engineering workflow and validation guidance
```

### Technology

- Python and Django 6.0
- SQLite for local development through the Django ORM
- `evrmore-rpc` with routed public and local-node backends
- `coincurve`, `ecdsa`, `hdwallet`, and related cryptographic libraries
- Kubo/IPFS for metadata and media
- Server-rendered Django templates with vanilla JavaScript and shared CSS

## Local Development

### Prerequisites

- Python 3.12 or newer
- A Python virtual environment
- Optional: a local EVRMore node for signing, broadcasting, wallet-local RPC,
  and complete testnet validation
- Optional: a local Kubo daemon for IPFS uploads and metadata retrieval

The application can use the configured public RPC endpoints for supported read
operations. Settlement-critical and node-wallet operations require a local node.

### Setup

```bash
git clone <repository-url>
cd DeFiTome

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd Tome
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`.

No Node.js build step is required. Configuration is loaded from environment
variables or a local `.env` file at the repository root. Useful settings include:

```ini
DJANGO_SECRET_KEY=replace-this-for-any-shared-environment
DJANGO_DEBUG=True
DEFAULT_EVRMORE_NETWORK=testnet
DEFAULT_EVRMORE_RPC_ENDPOINT_MODE=public

# Local testnet node, required for full transaction operations
RPC_TESTNET_HOST=127.0.0.1
RPC_TESTNET_PORT=18819
RPC_TESTNET_USER=your-rpc-user
RPC_TESTNET_PASSWORD=your-rpc-password

# Optional local IPFS
IPFS_STORAGE_API_URL=http://localhost:5001/api/v0/
IPFS_GATEWAY_API_URL=http://localhost:8080/ipfs/

# Use a separate secret in shared or production-like environments
EVRMORE_AUTH_JWT_SECRET=replace-with-a-random-secret-of-at-least-32-bytes
EVRMORE_AUTH_CHALLENGE_EXPIRY_MINUTES=10
```

Network-specific RPC URL, scheme, path, datadir, and timeout settings are also
available in `Tome/Tome/settings.py`.

## Validation

Run the full Django suite from the repository root:

```bash
.venv/bin/python Tome/manage.py test
.venv/bin/python Tome/manage.py check
python scripts/verify_rpc_cheatsheet.py
```

Local-node transaction probes live in `scripts/`. They can sign or broadcast
transactions; inspect each script and its configured network before running it.

## Roadmap and Milestones

Roadmap status reflects repository implementation, not production readiness.

### Milestone 1: Application Foundation - Reached

- [x] Multi-app Django architecture and database migrations
- [x] Account, email, and EVRMore wallet authentication
- [x] Wallet creation, address management, balances, and transaction history
- [x] Mainnet/testnet and public/local RPC routing
- [x] Responsive dashboard, canonical URLs, and shared navigation system

### Milestone 2: Native Asset Infrastructure - Reached

- [x] Raw EVR and native asset transfer builders
- [x] Source-preserving EVR, asset, and authorization change
- [x] Mempool preflight and fee-floor validation
- [x] Asset issuance workspace and tracked asset inventory
- [x] IPFS media, asset metadata, and RIP-0010 address tags
- [x] Verified messaging-channel policy lifecycle

### Milestone 3: Testnet Trading Alpha - Reached, Hardening Continues

- [x] Unique-asset atomic offers with EVR settlement
- [x] Unique-asset atomic offers with fungible asset settlement
- [x] Funding locks, expiry, cancellation, and reconciliation records
- [x] Fungible market registry and order-book workflows
- [x] Limit, market, and stop-loss order paths
- [x] Messaging-channel publication gates for governed swap operations
- [ ] Complete repeatable end-to-end local-node testnet scenarios
- [ ] Add broader failure-injection and recovery coverage
- [ ] Publish operator runbooks and testnet release criteria

### Milestone 4: Security and Controlled Mainnet - Planned

- [ ] Threat model and security review of key custody and settlement
- [ ] Independent audit of transaction and authorization paths
- [ ] Production secrets, database, backup, monitoring, and alerting plan
- [ ] Limited-value mainnet pilot with emergency pause procedures
- [ ] General mainnet availability after pilot acceptance criteria are met

### Milestone 5: Protocol Expansion - Future

- [ ] Harden liquidity and fee-distribution workflows
- [ ] Validate decentralized price-feed and oracle assumptions
- [ ] Harden lending, liquidation, and rate-market workflows
- [ ] Stabilize versioned external API and developer SDK
- [ ] Add governance only after operational and security foundations mature

## Documentation

Maintainer references are collected in [`.github/docs/`](.github/docs/README.md):

- [EVRMore command cheatsheet](.github/docs/commands-cheatsheet.md)
- [Asset type reference](.github/docs/EVRMORE_ASSET_TYPES.md)
- [Asset integration summary](.github/docs/ASSET_INTEGRATION_SUMMARY.md)
- [Asset security summary](.github/docs/SECURITY_SUMMARY.md)
- [NFT implementation notes](.github/docs/NFT_IMPLEMENTATION_COMPLETE.md)
- [RIP-0010 address metadata tags](.github/docs/RIP-0010_ADDRESS_METADATA_TAGS.md)

The application also serves its current API catalog at `/api/docs/`.

## Security

- Treat the default Django secret, `DEBUG=True`, SQLite, and permissive hosts as
  local-development defaults only.
- Never commit RPC credentials, wallet secrets, passphrases, API secrets, or
  production `.env` files.
- Use a local node for signing, broadcast, UTXO-critical decisions, and methods
  unavailable through restricted public RPC endpoints.
- Preserve raw-transaction mempool preflight and source-change invariants when
  extending transaction code.
- Do not use this alpha with funds you cannot afford to lose.

## Contributing

Keep changes focused, add tests for behavioral changes, and run the validation
commands above. EVRMore transaction changes must use the existing RPC wrappers,
raw transaction helpers, local-node validation, and `testmempoolaccept` gate.
Internal implementation notes belong under [`.github/docs/`](.github/docs/README.md).
