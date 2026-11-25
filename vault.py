"""EIP-7540 asynchronous ERC-4626 vault simulator in pure Python.

EIP-7540 adds an asynchronous request flow on top of ERC-4626: a user
first *requests* a deposit or a redemption, the vault operator later
*fulfils* the request at the current share price, and only then can the
user *claim* the shares (or assets).  The synchronous deposit/mint/
withdraw/redeem path of ERC-4626 stays available alongside it.

Modelled here, with exact integer math (all conversions round down):

* ``request_deposit(assets, user)`` -- locks ``assets`` in a pending
  deposit request for ``user`` (one outstanding request per user).
* ``fulfill_deposit(user, shares)`` -- the operator mints ``shares`` for
  the pending request; shares become claimable.
* ``claim_deposit(user)`` -- the user collects the fulfilled shares.
* ``request_redeem(shares, user)`` -- locks ``shares`` in a pending
  redeem request.
* ``fulfill_redeem(user, assets)`` -- the operator releases ``assets``
  for the pending request; assets become claimable.
* ``claim_redeem(user)`` -- the user collects the fulfilled assets.
* ``cancel_deposit_request(user)`` / ``cancel_redeem_request(user)`` --
  refund the locked amount while the request is still pending.
* ``max_deposit`` / ``max_mint`` / ``max_withdraw`` / ``max_redeem``
  report what the *synchronous* path can still do right now.
* ``convert_to_shares`` / ``convert_to_assets`` -- ERC-4626 conversion
  math (round down), used by both paths and by the demo.
* ``pending_deposit_request`` / ``claimable_deposit_request`` /
  ``pending_redeem_request`` / ``claimable_redeem_request`` -- the
  EIP-7540 view functions.

Every state change is appended to an audit log of events, so the whole
async lifecycle is inspectable after the fact.

Pure Python, zero dependencies.  Run the doctests with
``python -m doctest vault.py`` or ``python vault.py --test``;
``python vault.py --demo`` prints a full request -> fulfil -> claim
lifecycle.
"""

__all__ = [
    "AsyncVault",
    "VaultError",
    "demo",
]

ZERO = 0


class VaultError(Exception):
    """Raised when a vault operation is invalid (bad amount, bad state)."""


class AsyncVault:
    """An EIP-7540 asynchronous ERC-4626 vault.

    The vault holds one underlying asset.  Shares are minted/burned at
    the current share price: ``shares = assets * total_supply /
    total_assets`` (round down) and ``assets = shares * total_assets /
    total_supply`` (round down).  A fresh vault starts 1:1.

    >>> v = AsyncVault()
    >>> v.convert_to_shares(1000)
    1000
    >>> v.deposit(1000, "alice")
    1000
    >>> v.balance_of("alice")
    1000
    >>> v.total_assets
    1000
    >>> v.total_supply
    1000
    """

    def __init__(self, operator="operator"):
        self.operator = operator
        self.total_assets = 0
        self.total_supply = 0
        self._balances = {}          # user -> shares
        self._pending_deposit = {}   # user -> assets locked
        self._claimable_deposit = {} # user -> shares fulfilled
        self._pending_redeem = {}    # user -> shares locked
        self._claimable_redeem = {}  # user -> assets fulfilled
        self.events = []             # (name, user, amount) audit log

    # -- helpers -----------------------------------------------------

    def _emit(self, name, user, amount):
        self.events.append((name, user, amount))

    def _add_balance(self, user, shares):
        self._balances[user] = self._balances.get(user, 0) + shares

    # -- views -------------------------------------------------------

    def balance_of(self, user):
        """Shares held by ``user`` (claimable shares are not included)."""
        return self._balances.get(user, 0)

    def convert_to_shares(self, assets):
        """Shares minted for ``assets`` at the current price (round down)."""
        if assets < 0:
            raise VaultError("assets must be non-negative")
        if self.total_supply == 0:
            return assets
        return assets * self.total_supply // self.total_assets

    def convert_to_assets(self, shares):
        """Assets redeemable for ``shares`` at the current price (round down)."""
        if shares < 0:
            raise VaultError("shares must be non-negative")
        if self.total_supply == 0:
            return shares
        return shares * self.total_assets // self.total_supply

    def pending_deposit_request(self, user):
        """Assets locked in ``user``'s outstanding deposit request."""
        return self._pending_deposit.get(user, 0)

    def claimable_deposit_request(self, user):
        """Shares ``user`` can claim from a fulfilled deposit request."""
        return self._claimable_deposit.get(user, 0)

    def pending_redeem_request(self, user):
        """Shares locked in ``user``'s outstanding redeem request."""
        return self._pending_redeem.get(user, 0)

    def claimable_redeem_request(self, user):
        """Assets ``user`` can claim from a fulfilled redeem request."""
        return self._claimable_redeem.get(user, 0)

    # -- synchronous ERC-4626 path ------------------------------------

    def deposit(self, assets, user):
        """Sync deposit: mint shares immediately. Returns shares minted.

        >>> v = AsyncVault()
        >>> v.deposit(500, "alice")
        500
        >>> v.deposit(500, "bob")
        500
        >>> v.yield_(1000)  # yield doubles the share price
        >>> v.deposit(500, "carol")
        250
        >>> v.balance_of("carol")
        250
        """
        if assets <= 0:
            raise VaultError("deposit amount must be positive")
        shares = self.convert_to_shares(assets)
        if shares <= 0:
            raise VaultError("deposit too small to mint a share")
        self.total_assets += assets
        self.total_supply += shares
        self._add_balance(user, shares)
        self._emit("Deposit", user, assets)
        return shares

    def mint(self, shares, user):
        """Sync mint: deposit exactly enough assets for ``shares``.

        >>> v = AsyncVault()
        >>> v.deposit(1000, "alice")
        1000
        >>> v.yield_(1000)
        >>> v.mint(100, "bob")  # share price is now 2 assets per share
        200
        >>> v.balance_of("bob")
        100
        """
        if shares <= 0:
            raise VaultError("mint amount must be positive")
        assets = self.convert_to_assets(shares)
        # Round up so the minter always covers the full share price.
        if self.total_supply and assets * self.total_supply < shares * self.total_assets:
            assets += 1
        if assets <= 0:
            assets = shares
        self.total_assets += assets
        self.total_supply += shares
        self._add_balance(user, shares)
        self._emit("Deposit", user, assets)
        return assets

    def withdraw(self, assets, user):
        """Sync withdraw: burn shares for ``assets``. Returns shares burned.

        >>> v = AsyncVault()
        >>> v.deposit(1000, "alice")
        1000
        >>> v.withdraw(400, "alice")
        400
        >>> v.balance_of("alice")
        600
        >>> v.total_assets
        600
        """
        if assets <= 0:
            raise VaultError("withdraw amount must be positive")
        shares = self.convert_to_shares(assets)
        if shares <= 0:
            raise VaultError("withdraw too small to burn a share")
        if shares > self.balance_of(user):
            raise VaultError("insufficient shares")
        self.total_assets -= assets
        self.total_supply -= shares
        self._balances[user] -= shares
        self._emit("Withdraw", user, assets)
        return shares

    def redeem(self, shares, user):
        """Sync redeem: burn ``shares`` for assets. Returns assets paid.

        >>> v = AsyncVault()
        >>> v.deposit(1000, "alice")
        1000
        >>> v.yield_(1000)
        >>> v.redeem(500, "alice")  # share price is 2, so 500 shares = 1000
        1000
        >>> v.balance_of("alice")
        500
        """
        if shares <= 0:
            raise VaultError("redeem amount must be positive")
        if shares > self.balance_of(user):
            raise VaultError("insufficient shares")
        assets = self.convert_to_assets(shares)
        self.total_assets -= assets
        self.total_supply -= shares
        self._balances[user] -= shares
        self._emit("Withdraw", user, assets)
        return assets

    # -- async EIP-7540 path: deposits --------------------------------

    def request_deposit(self, assets, user):
        """Open a deposit request, locking ``assets`` until fulfilled.

        >>> v = AsyncVault()
        >>> v.request_deposit(800, "alice")
        800
        >>> v.pending_deposit_request("alice")
        800
        >>> v.balance_of("alice")  # nothing minted yet -- async!
        0
        >>> v.request_deposit(100, "alice")
        Traceback (most recent call last):
        ...
        VaultError: deposit request already pending
        """
        if assets <= 0:
            raise VaultError("deposit amount must be positive")
        if user in self._pending_deposit:
            raise VaultError("deposit request already pending")
        if self.claimable_deposit_request(user):
            raise VaultError("unclaimed fulfilled deposit exists")
        self._pending_deposit[user] = assets
        self._emit("DepositRequest", user, assets)
        return assets

    def fulfill_deposit(self, user, shares, caller=None):
        """Operator fulfils a pending deposit request with ``shares``.

        The locked assets enter the vault; the shares become claimable.

        >>> v = AsyncVault()
        >>> v.request_deposit(1000, "alice")
        1000
        >>> v.fulfill_deposit("alice", 1000)
        1000
        >>> v.claimable_deposit_request("alice")
        1000
        >>> v.pending_deposit_request("alice")
        0
        >>> v.total_assets
        1000
        """
        if caller is not None and caller != self.operator:
            raise VaultError("only the operator can fulfil requests")
        if user not in self._pending_deposit:
            raise VaultError("no pending deposit request")
        if shares <= 0:
            raise VaultError("fulfilled shares must be positive")
        assets = self._pending_deposit.pop(user)
        self.total_assets += assets
        self.total_supply += shares
        self._claimable_deposit[user] = (
            self._claimable_deposit.get(user, 0) + shares
        )
        self._emit("DepositFulfilled", user, shares)
        return shares

    def claim_deposit(self, user):
        """Claim shares from a fulfilled deposit request.

        >>> v = AsyncVault()
        >>> v.request_deposit(1000, "alice")
        1000
        >>> v.fulfill_deposit("alice", 900)  # operator prices the request
        900
        >>> v.claim_deposit("alice")
        900
        >>> v.balance_of("alice")
        900
        >>> v.claimable_deposit_request("alice")
        0
        """
        shares = self._claimable_deposit.pop(user, 0)
        if shares <= 0:
            raise VaultError("nothing claimable")
        self._add_balance(user, shares)
        self._emit("DepositClaimed", user, shares)
        return shares

    def cancel_deposit_request(self, user):
        """Cancel a still-pending deposit request; assets are refunded.

        >>> v = AsyncVault()
        >>> v.request_deposit(700, "alice")
        700
        >>> v.cancel_deposit_request("alice")
        700
        >>> v.pending_deposit_request("alice")
        0
        >>> v.cancel_deposit_request("alice")
        Traceback (most recent call last):
        ...
        VaultError: no pending deposit request
        """
        assets = self._pending_deposit.pop(user, None)
        if assets is None:
            raise VaultError("no pending deposit request")
        self._emit("DepositRequestCancelled", user, assets)
        return assets

    # -- async EIP-7540 path: redemptions ------------------------------

    def request_redeem(self, shares, user):
        """Open a redeem request, locking ``shares`` until fulfilled.

        >>> v = AsyncVault()
        >>> v.deposit(1000, "alice")
        1000
        >>> v.request_redeem(400, "alice")
        400
        >>> v.pending_redeem_request("alice")
        400
        >>> v.balance_of("alice")  # locked shares leave the balance
        600
        >>> v.request_redeem(100, "alice")
        Traceback (most recent call last):
        ...
        VaultError: redeem request already pending
        """
        if shares <= 0:
            raise VaultError("redeem amount must be positive")
        if user in self._pending_redeem:
            raise VaultError("redeem request already pending")
        if self.claimable_redeem_request(user):
            raise VaultError("unclaimed fulfilled redeem exists")
        if shares > self.balance_of(user):
            raise VaultError("insufficient shares")
        self._balances[user] -= shares
        self._pending_redeem[user] = shares
        self._emit("RedeemRequest", user, shares)
        return shares

    def fulfill_redeem(self, user, assets, caller=None):
        """Operator fulfils a pending redeem request with ``assets``.

        The locked shares are burned; the assets become claimable.

        >>> v = AsyncVault()
        >>> v.deposit(1000, "alice")
        1000
        >>> v.request_redeem(400, "alice")
        400
        >>> v.fulfill_redeem("alice", 400)
        400
        >>> v.claimable_redeem_request("alice")
        400
        >>> v.pending_redeem_request("alice")
        0
        >>> v.total_supply
        600
        >>> v.total_assets
        600
        """
        if caller is not None and caller != self.operator:
            raise VaultError("only the operator can fulfil requests")
        if user not in self._pending_redeem:
            raise VaultError("no pending redeem request")
        if assets <= 0:
            raise VaultError("fulfilled assets must be positive")
        if assets > self.total_assets:
            raise VaultError("vault cannot pay that much")
        shares = self._pending_redeem.pop(user)
        self.total_assets -= assets
        self.total_supply -= shares
        self._claimable_redeem[user] = (
            self._claimable_redeem.get(user, 0) + assets
        )
        self._emit("RedeemFulfilled", user, assets)
        return assets

    def claim_redeem(self, user):
        """Claim assets from a fulfilled redeem request.

        >>> v = AsyncVault()
        >>> v.deposit(1000, "alice")
        1000
        >>> v.request_redeem(400, "alice")
        400
        >>> v.fulfill_redeem("alice", 380)  # operator applies a haircut
        380
        >>> v.claim_redeem("alice")
        380
        >>> v.claimable_redeem_request("alice")
        0
        >>> v.balance_of("alice")
        600
        """
        assets = self._claimable_redeem.pop(user, 0)
        if assets <= 0:
            raise VaultError("nothing claimable")
        self._emit("RedeemClaimed", user, assets)
        return assets

    def cancel_redeem_request(self, user):
        """Cancel a still-pending redeem request; shares are returned.

        >>> v = AsyncVault()
        >>> v.deposit(1000, "alice")
        1000
        >>> v.request_redeem(400, "alice")
        400
        >>> v.cancel_redeem_request("alice")
        400
        >>> v.balance_of("alice")
        1000
        >>> v.pending_redeem_request("alice")
        0
        """
        shares = self._pending_redeem.pop(user, None)
        if shares is None:
            raise VaultError("no pending redeem request")
        self._add_balance(user, shares)
        self._emit("RedeemRequestCancelled", user, shares)
        return shares

    # -- limits for the synchronous path -------------------------------

    def max_deposit(self, user):
        """Max assets the sync path can still deposit for ``user``.

        Deposit requests lock no vault capacity here, so the sync path is
        always open in this model; it returns a large practical bound.

        >>> v = AsyncVault()
        >>> v.max_deposit("alice") > 10 ** 12
        True
        """
        return 10 ** 18

    def max_mint(self, user):
        """Max shares the sync path can still mint for ``user``."""
        return self.convert_to_shares(self.max_deposit(user))

    def max_withdraw(self, user):
        """Max assets ``user`` can synchronously withdraw (their balance)."""
        return self.convert_to_assets(self.balance_of(user))

    def max_redeem(self, user):
        """Max shares ``user`` can synchronously redeem (their balance)."""
        return self.balance_of(user)

    # -- misc ----------------------------------------------------------

    def yield_(self, amount):
        """Simulate yield: assets arrive without new shares (price up).

        >>> v = AsyncVault()
        >>> v.deposit(1000, "alice")
        1000
        >>> v.yield_(500)
        >>> v.convert_to_assets(1000)
        1500
        """
        if amount < 0:
            raise VaultError("yield must be non-negative")
        self.total_assets += amount
        self._emit("Yield", "vault", amount)

    def share_price(self):
        """Assets per share as a float (1.0 for an empty vault).

        >>> v = AsyncVault()
        >>> v.share_price()
        1.0
        >>> v.deposit(1000, "alice")
        1000
        >>> v.yield_(1000)
        >>> v.share_price()
        2.0
        """
        if self.total_supply == 0:
            return 1.0
        return self.total_assets / self.total_supply

    def pending_requests(self):
        """All outstanding requests as ``(kind, user, amount)`` triples.

        >>> v = AsyncVault()
        >>> v.request_deposit(300, "alice")
        300
        >>> v.deposit(1000, "bob")
        1000
        >>> v.request_redeem(200, "bob")
        200
        >>> v.pending_requests()
        [('deposit', 'alice', 300), ('redeem', 'bob', 200)]
        """
        out = [("deposit", u, a) for u, a in self._pending_deposit.items()]
        out += [("redeem", u, s) for u, s in self._pending_redeem.items()]
        return out


def demo():
    """Run a full async lifecycle and print it."""
    v = AsyncVault()
    print("== EIP-7540 async vault demo ==")
    print(f"alice requests a deposit of 1000 assets")
    v.request_deposit(1000, "alice")
    print(f"  pending deposit: {v.pending_deposit_request('alice')}")
    print(f"  alice balance (not yet minted): {v.balance_of('alice')}")
    print("operator fulfils at the 1:1 price (1000 shares)")
    v.fulfill_deposit("alice", 1000)
    print(f"  claimable shares: {v.claimable_deposit_request('alice')}")
    v.claim_deposit("alice")
    print(f"  alice balance after claim: {v.balance_of('alice')}")
    print("yield arrives: +1000 assets (share price now 2.0)")
    v.yield_(1000)
    print(f"  share price: {v.share_price()}")
    print("alice requests a redeem of 500 shares")
    v.request_redeem(500, "alice")
    print(f"  pending redeem: {v.pending_redeem_request('alice')}")
    print("operator fulfils with 1000 assets (500 shares x 2.0)")
    v.fulfill_redeem("alice", 1000)
    print(f"  claimable assets: {v.claimable_redeem_request('alice')}")
    print(f"alice claims {v.claim_redeem('alice')} assets")
    print(f"  alice balance: {v.balance_of('alice')}")
    print(f"  vault: {v.total_assets} assets / {v.total_supply} shares")
    print(f"  events: {len(v.events)} logged")


if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        import doctest

        failures, tests = doctest.testmod(verbose=False)
        print(f"{tests} doctests, {failures} failures")
        sys.exit(1 if failures else 0)
    demo()
