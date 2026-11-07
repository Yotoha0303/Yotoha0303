#!/usr/bin/env python3
"""Offline cross-chain MEV screening and private order routing simulation.

Run: python3 scripts/cross_chain_mev_protection_engine.py
Requires: cryptography (AES-256-GCM). Findings are heuristics, not proof of
intent. The threshold scheme splits an ephemeral symmetric key; it does not
provide distributed key generation, verifiable shares, relay delivery, or
atomic execution across chains.
"""

import hashlib
import hmac
import json
import math
import secrets
from collections import defaultdict
from dataclasses import asdict, dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


COMMIT_DOMAIN = b"cross-chain-mev-bundle-v1\0"
MAX_GAP = 3
MIN_GAS_MULTIPLIER = 1.2
ACTIONS = frozenset({"swap", "add_liquidity", "remove_liquidity"})


@dataclass(frozen=True)
class Transaction:
    tx_id: str
    chain_id: str
    pool: str
    actor: str
    action: str
    amount: float
    gas_price: float
    block_number: int
    token_in: str = ""
    token_out: str = ""

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in
               (self.tx_id, self.chain_id, self.pool, self.actor)):
            raise ValueError("transaction identity, chain, pool, and actor are required")
        if self.action not in ACTIONS:
            raise ValueError("unsupported transaction action")
        for name in ("amount", "gas_price"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < (0 if name == "gas_price" else 0)):
                raise ValueError("invalid " + name)
        if self.amount == 0:
            raise ValueError("amount must be positive")
        if isinstance(self.block_number, bool) or not isinstance(self.block_number, int) or self.block_number < 0:
            raise ValueError("invalid block number")
        if self.action == "swap" and (not self.token_in or not self.token_out
                                       or self.token_in == self.token_out):
            raise ValueError("swap needs distinct input and output tokens")
        if not isinstance(self.token_in, str) or not isinstance(self.token_out, str):
            raise ValueError("invalid token")


@dataclass(frozen=True)
class Finding:
    kind: str
    chain_id: str
    pool: str
    tx_ids: tuple[str, ...]


@dataclass(frozen=True)
class PrivateBundle:
    bundle_id: str
    threshold: int
    share_count: int
    nonce: bytes
    ciphertext: bytes
    commitment: str


@dataclass(frozen=True)
class KeyShare:
    index: int
    value: bytes


@dataclass(frozen=True)
class ChainRoute:
    chain_id: str
    destination: str
    tx_ids: tuple[str, ...]


@dataclass(frozen=True)
class RoutingDecision:
    bundle_id: str
    routes: tuple[ChainRoute, ...]
    findings: tuple[Finding, ...]


def _validate_transactions(transactions):
    transactions = tuple(transactions)
    if not transactions or any(not isinstance(tx, Transaction) for tx in transactions):
        raise ValueError("bundle needs transactions")
    ids = [tx.tx_id for tx in transactions]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate transaction id")
    return transactions


def detect_attacks(transactions):
    """Screen execution order within each chain, pool, and block."""
    transactions = _validate_transactions(transactions)
    groups = defaultdict(list)
    positions = {}
    for position, tx in enumerate(transactions):
        groups[(tx.chain_id, tx.pool, tx.block_number)].append(tx)
        positions[tx.tx_id] = position

    findings = []
    sandwich_fronts = set()
    for (chain, pool, _), group in groups.items():
        for victim_index, victim in enumerate(group):
            if victim.action != "swap":
                continue
            before = group[max(0, victim_index - MAX_GAP):victim_index]
            after = group[victim_index + 1:victim_index + MAX_GAP + 1]
            for front in before:
                if (front.action != "swap" or front.actor == victim.actor
                        or (front.token_in, front.token_out) != (victim.token_in, victim.token_out)
                        or front.gas_price < victim.gas_price * MIN_GAS_MULTIPLIER):
                    continue
                back = next((tx for tx in after if tx.action == "swap" and tx.actor == front.actor
                             and (tx.token_in, tx.token_out) == (victim.token_out, victim.token_in)), None)
                if back is not None:
                    findings.append(Finding("sandwich", chain, pool,
                                            (front.tx_id, victim.tx_id, back.tx_id)))
                    sandwich_fronts.add((front.tx_id, victim.tx_id))
                elif (front.tx_id, victim.tx_id) not in sandwich_fronts:
                    findings.append(Finding("front_running", chain, pool,
                                            (front.tx_id, victim.tx_id)))

            for add in before:
                if add.action != "add_liquidity" or add.actor == victim.actor:
                    continue
                remove = next((tx for tx in after if tx.action == "remove_liquidity"
                               and tx.actor == add.actor), None)
                if remove is not None:
                    findings.append(Finding("jit_liquidity", chain, pool,
                                            (add.tx_id, victim.tx_id, remove.tx_id)))

    # A front leg may be seen before its matching back leg in the same pass.
    findings = [finding for finding in findings if finding.kind != "front_running"
                or tuple(finding.tx_ids) not in sandwich_fronts]
    return sorted(findings, key=lambda item: (positions[item.tx_ids[0]], item.kind, item.tx_ids))


def _gf_multiply(left, right):
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        left = ((left << 1) ^ (0x11B if left & 0x80 else 0)) & 0xFF
        right >>= 1
    return result


def _gf_power(value, exponent):
    result = 1
    while exponent:
        if exponent & 1:
            result = _gf_multiply(result, value)
        value = _gf_multiply(value, value)
        exponent >>= 1
    return result


def _split_key(key, threshold, count):
    shares = [bytearray(32) for _ in range(count)]
    for offset, constant in enumerate(key):
        coefficients = [constant, *secrets.token_bytes(threshold - 1)]
        for index, value in enumerate(shares, 1):
            point = 0
            for coefficient in reversed(coefficients):
                point = _gf_multiply(point, index) ^ coefficient
            value[offset] = point
    return tuple(KeyShare(index, bytes(value)) for index, value in enumerate(shares, 1))


def _combine_key(shares, threshold, count):
    shares = tuple(shares)
    if len(shares) < threshold:
        raise ValueError("insufficient decryption shares")
    if (any(not isinstance(share, KeyShare) or isinstance(share.index, bool)
            or not isinstance(share.index, int) or not 1 <= share.index <= count
            or not isinstance(share.value, bytes) or len(share.value) != 32 for share in shares)
            or len({share.index for share in shares}) != len(shares)):
        raise ValueError("invalid or duplicate decryption share")
    selected = sorted(shares, key=lambda share: share.index)[:threshold]
    key = bytearray(32)
    for share in selected:
        basis = 1
        for other in selected:
            if other.index != share.index:
                basis = _gf_multiply(basis, _gf_multiply(
                    other.index, _gf_power(other.index ^ share.index, 254)))
        for offset, value in enumerate(share.value):
            key[offset] ^= _gf_multiply(value, basis)
    return bytes(key)


def _aad(bundle):
    return json.dumps([bundle.bundle_id, bundle.threshold, bundle.share_count],
                      separators=(",", ":")).encode("ascii")


def seal_bundle(transactions, *, threshold=2, share_count=3):
    """Return public ciphertext, private key shares, and a private reveal salt."""
    transactions = _validate_transactions(transactions)
    if (isinstance(threshold, bool) or isinstance(share_count, bool)
            or not isinstance(threshold, int) or not isinstance(share_count, int)
            or not 2 <= threshold <= share_count <= 255):
        raise ValueError("invalid threshold or share count")
    payload = json.dumps([asdict(tx) for tx in transactions], sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    salt = secrets.token_bytes(32)
    commitment = hashlib.sha256(COMMIT_DOMAIN + salt + payload).hexdigest()
    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    bundle_id = secrets.token_hex(16)
    draft = PrivateBundle(bundle_id, threshold, share_count, nonce, b"", commitment)
    ciphertext = AESGCM(key).encrypt(nonce, payload, _aad(draft))
    return (PrivateBundle(bundle_id, threshold, share_count, nonce, ciphertext, commitment),
            _split_key(key, threshold, share_count), salt)


class ThresholdRouter:
    """Decrypt after quorum, verify reveal, then isolate flagged chains."""

    def __init__(self):
        self._routed_ids = set()

    def reveal_and_route(self, bundle, shares, salt):
        if not isinstance(bundle, PrivateBundle):
            raise ValueError("invalid bundle")
        if bundle.bundle_id in self._routed_ids:
            raise ValueError("bundle already routed")
        if (not isinstance(salt, bytes) or len(salt) != 32
                or not isinstance(bundle.nonce, bytes) or len(bundle.nonce) != 12
                or not isinstance(bundle.ciphertext, bytes) or len(bundle.ciphertext) < 16
                or isinstance(bundle.threshold, bool) or isinstance(bundle.share_count, bool)
                or not isinstance(bundle.threshold, int) or not isinstance(bundle.share_count, int)
                or not 2 <= bundle.threshold <= bundle.share_count <= 255):
            raise ValueError("invalid bundle metadata or reveal")
        key = _combine_key(shares, bundle.threshold, bundle.share_count)
        try:
            payload = AESGCM(key).decrypt(bundle.nonce, bundle.ciphertext, _aad(bundle))
        except (InvalidTag, ValueError) as error:
            raise ValueError("bundle authentication failed") from error
        expected = hashlib.sha256(COMMIT_DOMAIN + salt + payload).hexdigest()
        if not isinstance(bundle.commitment, str) or not hmac.compare_digest(expected, bundle.commitment):
            raise ValueError("commitment mismatch")
        try:
            records = json.loads(payload)
            if not isinstance(records, list):
                raise ValueError("invalid transaction payload")
            transactions = _validate_transactions(Transaction(**record) for record in records)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid transaction payload") from error

        findings = tuple(detect_attacks(transactions))
        flagged = {finding.chain_id for finding in findings}
        by_chain = defaultdict(list)
        for tx in transactions:
            by_chain[tx.chain_id].append(tx.tx_id)
        routes = tuple(ChainRoute(chain, "quarantine" if chain in flagged else "private_relay",
                                  tuple(ids)) for chain, ids in by_chain.items())
        self._routed_ids.add(bundle.bundle_id)
        return RoutingDecision(bundle.bundle_id, routes, findings)


def simulate_mempool():
    """Deterministic fixture with a sandwich, JIT liquidity, and a clean chain."""
    def swap(tx_id, chain, actor, direction, gas, block=1):
        token_in, token_out = direction
        return Transaction(tx_id, chain, "ETH-USDC", actor, "swap", 100, gas,
                           block, token_in, token_out)

    return (
        swap("eth-front", "ethereum", "searcher", ("USDC", "ETH"), 15),
        swap("eth-victim", "ethereum", "user", ("USDC", "ETH"), 10),
        swap("eth-back", "ethereum", "searcher", ("ETH", "USDC"), 10),
        Transaction("arb-add", "arbitrum", "ETH-USDC", "liquidity-bot", "add_liquidity", 1000, 10, 1),
        swap("arb-victim", "arbitrum", "user", ("USDC", "ETH"), 10),
        Transaction("arb-remove", "arbitrum", "ETH-USDC", "liquidity-bot", "remove_liquidity", 1000, 10, 1),
        swap("base-user", "base", "user", ("USDC", "ETH"), 10),
    )


def main():
    bundle, shares, salt = seal_bundle(simulate_mempool())
    decision = ThresholdRouter().reveal_and_route(bundle, shares[:bundle.threshold], salt)
    print(json.dumps(asdict(decision), indent=2))


if __name__ == "__main__":
    main()
