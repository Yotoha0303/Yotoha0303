#!/usr/bin/env python3
"""Unit tests for the offline cross-chain MEV protection simulation."""

import json
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import cross_chain_mev_protection_engine as engine


def swap(tx_id, chain="ethereum", actor="user", gas=10, block=1,
         token_in="USDC", token_out="ETH", pool="ETH-USDC"):
    return engine.Transaction(tx_id, chain, pool, actor, "swap", 100, gas,
                              block, token_in, token_out)


class TransactionTests(unittest.TestCase):
    def test_transaction_requires_positive_amount_and_valid_swap_tokens(self):
        with self.assertRaises(ValueError):
            swap("bad", token_in="ETH", token_out="ETH")
        with self.assertRaises(ValueError):
            engine.Transaction("bad", "eth", "pool", "actor", "swap", 0, 1, 1, "A", "B")

    def test_transaction_rejects_nonfinite_numeric_values(self):
        with self.assertRaises(ValueError):
            engine.Transaction("bad", "eth", "pool", "actor", "swap", float("nan"), 1, 1, "A", "B")
        with self.assertRaises(ValueError):
            engine.Transaction("bad", "eth", "pool", "actor", "swap", 1, float("inf"), 1, "A", "B")

    def test_transaction_rejects_unknown_action_and_invalid_block(self):
        with self.assertRaises(ValueError):
            engine.Transaction("bad", "eth", "pool", "actor", "borrow", 1, 1, 1)
        with self.assertRaises(ValueError):
            engine.Transaction("bad", "eth", "pool", "actor", "add_liquidity", 1, 1, -1)

    def test_duplicate_ids_are_rejected_before_detection(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            engine.detect_attacks([swap("same"), swap("same", chain="base")])


class DetectionTests(unittest.TestCase):
    def test_sandwich_attack_requires_front_and_back_legs(self):
        rows = [swap("front", actor="bot", gas=20), swap("victim"),
                swap("back", actor="bot", gas=10, token_in="ETH", token_out="USDC")]
        findings = engine.detect_attacks(rows)
        self.assertEqual([(item.kind, item.tx_ids) for item in findings],
                         [("sandwich", ("front", "victim", "back"))])

    def test_front_running_is_reported_without_matching_back_leg(self):
        findings = engine.detect_attacks([swap("front", actor="bot", gas=20), swap("victim")])
        self.assertEqual(findings[0].kind, "front_running")
        self.assertEqual(findings[0].tx_ids, ("front", "victim"))

    def test_front_running_requires_gas_premium(self):
        findings = engine.detect_attacks([swap("front", actor="bot", gas=11), swap("victim")])
        self.assertEqual(findings, [])

    def test_sandwich_does_not_cross_pools_or_chains(self):
        rows = [swap("front", actor="bot", gas=20), swap("victim", chain="base"),
                swap("back", actor="bot", token_in="ETH", token_out="USDC")]
        self.assertEqual(engine.detect_attacks(rows), [])

    def test_jit_liquidity_requires_add_before_and_remove_after_victim(self):
        rows = [engine.Transaction("add", "ethereum", "ETH-USDC", "lp", "add_liquidity", 1, 10, 1),
                swap("victim"),
                engine.Transaction("remove", "ethereum", "ETH-USDC", "lp", "remove_liquidity", 1, 10, 1)]
        findings = engine.detect_attacks(rows)
        self.assertEqual(findings[0].kind, "jit_liquidity")
        self.assertEqual(findings[0].tx_ids, ("add", "victim", "remove"))

    def test_jit_liquidity_is_not_reported_without_removal(self):
        rows = [engine.Transaction("add", "ethereum", "ETH-USDC", "lp", "add_liquidity", 1, 10, 1), swap("victim")]
        self.assertEqual(engine.detect_attacks(rows), [])

    def test_block_and_gap_boundaries_limit_detection(self):
        rows = [swap("front", actor="bot", gas=20), swap("x1", actor="bot", gas=20),
                swap("x2", actor="bot", gas=20), swap("x3", actor="bot", gas=20),
                swap("victim", block=2)]
        self.assertEqual(engine.detect_attacks(rows), [])

    def test_fixture_contains_all_expected_attack_families(self):
        kinds = {item.kind for item in engine.detect_attacks(engine.simulate_mempool())}
        self.assertEqual(kinds, {"sandwich", "jit_liquidity"})


class BundleTests(unittest.TestCase):
    def test_seal_bundle_encrypts_and_returns_threshold_shares(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")], threshold=2, share_count=3)
        self.assertEqual(len(shares), 3)
        self.assertEqual(len(salt), 32)
        self.assertNotIn(b"one", bundle.ciphertext)

    def test_threshold_reconstruction_accepts_any_quorum_subset(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")], threshold=2, share_count=3)
        decision = engine.ThresholdRouter().reveal_and_route(bundle, (shares[0], shares[2]), salt)
        self.assertEqual(decision.routes[0].tx_ids, ("one",))

    def test_insufficient_shares_are_rejected(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")])
        with self.assertRaisesRegex(ValueError, "insufficient"):
            engine.ThresholdRouter().reveal_and_route(bundle, shares[:1], salt)

    def test_duplicate_shares_are_rejected(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            engine.ThresholdRouter().reveal_and_route(bundle, (shares[0], shares[0]), salt)

    def test_ciphertext_tampering_fails_authentication(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")])
        tampered = replace(bundle, ciphertext=b"x" + bundle.ciphertext[1:])
        with self.assertRaisesRegex(ValueError, "authentication"):
            engine.ThresholdRouter().reveal_and_route(tampered, shares[:2], salt)

    def test_reveal_salt_tampering_fails_commitment(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")])
        with self.assertRaisesRegex(ValueError, "commitment"):
            engine.ThresholdRouter().reveal_and_route(bundle, shares[:2], b"z" * 32)

    def test_bundle_metadata_is_authenticated(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")])
        tampered = replace(bundle, bundle_id="different")
        with self.assertRaisesRegex(ValueError, "authentication"):
            engine.ThresholdRouter().reveal_and_route(tampered, shares[:2], salt)

    def test_invalid_threshold_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            engine.seal_bundle([swap("one")], threshold=1, share_count=3)
        with self.assertRaises(ValueError):
            engine.seal_bundle([swap("one")], threshold=4, share_count=3)

    def test_replay_of_bundle_is_rejected(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")])
        router = engine.ThresholdRouter()
        router.reveal_and_route(bundle, shares[:2], salt)
        with self.assertRaisesRegex(ValueError, "already routed"):
            router.reveal_and_route(bundle, shares[:2], salt)


class RoutingTests(unittest.TestCase):
    def test_flagged_chain_is_quarantined_and_clean_chain_uses_private_relay(self):
        bundle, shares, salt = engine.seal_bundle(engine.simulate_mempool())
        decision = engine.ThresholdRouter().reveal_and_route(bundle, shares[:2], salt)
        destinations = {route.chain_id: route.destination for route in decision.routes}
        self.assertEqual(destinations["ethereum"], "quarantine")
        self.assertEqual(destinations["arbitrum"], "quarantine")
        self.assertEqual(destinations["base"], "private_relay")

    def test_json_payload_round_trip_is_used_by_router(self):
        bundle, shares, salt = engine.seal_bundle([swap("one")])
        decision = engine.ThresholdRouter().reveal_and_route(bundle, shares[:2], salt)
        self.assertEqual(json.loads(json.dumps({"routes": [route.tx_ids for route in decision.routes]})),
                         {"routes": [["one"]]})

    def test_cli_emits_json(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("cross_chain_mev_protection_engine.py"))],
                                check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        self.assertEqual({route["destination"] for route in payload["routes"]},
                         {"quarantine", "private_relay"})


if __name__ == "__main__":
    unittest.main()
