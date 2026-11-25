# eip7540-async-vault-sim

An **EIP-7540 asynchronous ERC-4626 vault** simulator in pure Python — zero dependencies.

EIP-7540 adds an asynchronous request flow on top of ERC-4626: a user first
*requests* a deposit or a redemption, the vault operator later *fulfils* the
request at the current share price, and only then can the user *claim* the
shares (or assets). The synchronous `deposit` / `mint` / `withdraw` /
`redeem` path of ERC-4626 stays available alongside it.

## What it models

- **Async deposits** — `request_deposit(assets, user)` locks assets in a
  pending request (one outstanding request per user); `fulfill_deposit(user,
  shares)` by the operator makes shares claimable; `claim_deposit(user)`
  collects them. `cancel_deposit_request(user)` refunds a still-pending
  request.
- **Async redemptions** — `request_redeem(shares, user)` locks shares;
  `fulfill_redeem(user, assets)` burns them and makes assets claimable;
  `claim_redeem(user)` collects them. `cancel_redeem_request(user)`
  returns the locked shares.
- **EIP-7540 views** — `pending_deposit_request`, `claimable_deposit_request`,
  `pending_redeem_request`, `claimable_redeem_request`, plus
  `pending_requests()` for the whole queue.
- **ERC-4626 math** — `convert_to_shares` / `convert_to_assets` with exact
  integer math (round down), `max_deposit` / `max_mint` / `max_withdraw` /
  `max_redeem`, and `yield_()` to simulate yield raising the share price.
- **Audit log** — every state change is appended to `vault.events`.

## Usage

```python
from vault import AsyncVault

v = AsyncVault()
v.request_deposit(1000, "alice")   # pending, nothing minted yet
v.fulfill_deposit("alice", 1000)   # operator fulfils at 1:1
v.claim_deposit("alice")           # alice now holds 1000 shares

v.yield_(1000)                     # share price doubles to 2.0
v.request_redeem(500, "alice")     # 500 shares locked
v.fulfill_redeem("alice", 1000)    # operator pays 1000 assets
v.claim_redeem("alice")            # alice collects 1000 assets
```

## Run it

```bash
python vault.py --demo   # full request -> fulfil -> claim lifecycle
python vault.py --test   # 92 doctests
```

## Notes

- Operator-only fulfilment: pass `caller=` to `fulfill_deposit` /
  `fulfill_redeem`; anyone other than the vault operator is rejected.
- A request cannot be opened while another one of the same kind is pending
  or while a fulfilled amount is still unclaimed — claim or cancel first.
- This is a teaching simulator, not production vault code: no access
  control beyond the operator check, no token transfers, no reentrancy
  surface modelled.
