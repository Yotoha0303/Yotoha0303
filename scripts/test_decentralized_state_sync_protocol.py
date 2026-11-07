#!/usr/bin/env python3
"""Comprehensive unit test suite for decentralized_state_sync_protocol.py."""

import unittest
from decentralized_state_sync_protocol import (
    PNCounter,
    LWWElementSet,
    Timestamp,
    KnowledgeState,
    MerkleDAG,
    MerkleNode,
    DecentralizedStateSyncProtocol,
    canonical_json,
    content_hash,
)


class TestTimestampAndHelpers(unittest.TestCase):
    def test_canonical_json_and_content_hash(self):
        data = {"b": 2, "a": 1}
        self.assertEqual(canonical_json(data), '{"a":1,"b":2}')
        h = content_hash(data)
        self.assertEqual(len(h), 64)
        with self.assertRaises(ValueError):
            canonical_json(float("nan"))

    def test_timestamp_ordering_and_validation(self):
        t1 = Timestamp(1, "peer-a")
        t2 = Timestamp(2, "peer-a")
        t3 = Timestamp(1, "peer-b")
        self.assertTrue(t1 < t2)
        self.assertTrue(t1 < t3)  # tie breaker by replica_id
        self.assertEqual(Timestamp.from_value(5, "peer-x"), Timestamp(5, "peer-x"))
        self.assertEqual(Timestamp.from_value([10, "peer-y"], "peer-x"), Timestamp(10, "peer-y"))
        with self.assertRaises(ValueError):
            Timestamp(-1, "peer-a")
        with self.assertRaises(ValueError):
            Timestamp(1, "")


class TestPNCounter(unittest.TestCase):
    def test_pn_counter_basic_operations(self):
        c = PNCounter("peer-1")
        self.assertEqual(c.value, 0)
        c.increment(5)
        self.assertEqual(c.value, 5)
        c.decrement(2)
        self.assertEqual(c.value, 3)
        with self.assertRaises(ValueError):
            c.increment(-1)
        with self.assertRaises(ValueError):
            c.decrement(-1)

    def test_pn_counter_merge_properties(self):
        c1 = PNCounter("peer-1")
        c2 = PNCounter("peer-2")
        c1.increment(10)
        c2.increment(15)
        c1.decrement(2)
        c2.decrement(3)

        # Merge c2 into c1
        merged = c1.merge(c2)
        self.assertEqual(merged.value, 20)

        # Idempotence: merging again changes nothing
        merged_again = merged.merge(c2)
        self.assertEqual(merged_again.value, 20)

    def test_pn_counter_serialization(self):
        c = PNCounter("peer-1")
        c.increment(7)
        c.decrement(2)
        d = c.to_dict()
        restored = PNCounter.from_dict(d, "peer-1")
        self.assertEqual(restored.value, 5)
        self.assertEqual(c, restored)


class TestLWWElementSet(unittest.TestCase):
    def test_lww_add_and_read(self):
        s = LWWElementSet("peer-1")
        s.add("artifact-alpha")
        self.assertIn("artifact-alpha", s.elements())
        self.assertIn("artifact-alpha", s)
        self.assertEqual(len(s), 1)

    def test_lww_remove_and_clock_resolution(self):
        s = LWWElementSet("peer-1")
        s.add("target-doc")
        self.assertIn("target-doc", s.elements())
        s.remove("target-doc")
        self.assertNotIn("target-doc", s.elements())

        # Re-adding with a strictly higher timestamp restores the item
        s.add("target-doc")
        self.assertIn("target-doc", s.elements())

    def test_lww_merge_conflict_resolution(self):
        s1 = LWWElementSet("peer-1")
        s2 = LWWElementSet("peer-2")
        s1.add("shared-item")
        s2.add("shared-item")
        s2.remove("shared-item")

        merged = s1.merge(s2)
        self.assertNotIn("shared-item", merged.elements())

    def test_lww_serialization(self):
        s = LWWElementSet("peer-1")
        s.add("item-1")
        s.add("item-2")
        d = s.to_dict()
        restored = LWWElementSet.from_dict(d, "peer-1")
        self.assertEqual(restored.elements(), {"item-1", "item-2"})


class TestMerkleDAGAndProtocol(unittest.TestCase):
    def test_merkle_dag_genesis_and_nodes(self):
        dag = MerkleDAG("peer-0")
        self.assertIsNotNone(dag.genesis)
        self.assertEqual(len(dag.heads), 1)
        node = dag.increment(5)
        self.assertIsInstance(node, MerkleNode)
        self.assertEqual(dag.state.counter.value, 5)
        self.assertIn(node.node_id, dag.heads)

    def test_merkle_dag_decrement_and_knowledge(self):
        dag = MerkleDAG("peer-0")
        dag.increment(10)
        dag.decrement(4)
        self.assertEqual(dag.state.counter.value, 6)
        dag.add_knowledge("k1")
        self.assertIn("k1", dag.state.knowledge.elements())
        dag.remove_knowledge("k1")
        self.assertNotIn("k1", dag.state.knowledge.elements())

    def test_merkle_dag_sync_between_peers(self):
        dag_a = MerkleDAG("peer-a")
        dag_b = MerkleDAG("peer-b")

        dag_a.increment(10)
        dag_a.add_knowledge("insight-A")

        dag_b.increment(20)
        dag_b.add_knowledge("insight-B")

        # Sync B into A
        imported = dag_a.sync(dag_b)
        self.assertGreater(imported, 0)
        self.assertEqual(dag_a.state.counter.value, 30)
        self.assertEqual(dag_a.state.knowledge.elements(), {"insight-A", "insight-B"})

        # Sync A into B
        dag_b.sync(dag_a)
        self.assertEqual(dag_b.state.counter.value, 30)
        self.assertEqual(dag_b.state.knowledge.elements(), {"insight-A", "insight-B"})

    def test_merkle_dag_serialization_roundtrip(self):
        dag = MerkleDAG("peer-x")
        dag.increment(42)
        dag.add_knowledge("item-x")
        exported = dag.to_dict()
        restored = MerkleDAG.from_dict(exported, "peer-x")
        self.assertEqual(restored.state.counter.value, 42)
        self.assertEqual(restored.state.knowledge.elements(), {"item-x"})

    def test_merkle_dag_missing_hashes(self):
        dag_a = MerkleDAG("peer-a")
        dag_b = MerkleDAG("peer-b")
        node = dag_a.increment(1)
        missing = dag_a.missing_hashes(dag_b)
        self.assertIn(node.node_id, missing)

    def test_protocol_facade_and_offline_multi_peer_replication(self):
        alice = DecentralizedStateSyncProtocol("agent-alice")
        bob = DecentralizedStateSyncProtocol("agent-bob")
        charlie = DecentralizedStateSyncProtocol("agent-charlie")

        # Offline local changes
        alice.increment(100)
        alice.add_knowledge({"rule": "no-telemetry", "priority": 1})

        bob.decrement(30)
        bob.add_knowledge({"rule": "always-verify", "priority": 2})

        charlie.increment(50)
        charlie.add_knowledge({"rule": "air-gap-audit", "priority": 0})

        # P2P exchange: Alice <-> Bob
        msg_alice = alice.export_sync_message()
        bob.receive_sync_message(msg_alice)
        msg_bob = bob.export_sync_message()
        alice.receive_sync_message(msg_bob)

        self.assertEqual(alice.counter.value, 70)
        self.assertEqual(bob.counter.value, 70)

        # Charlie syncs with Bob
        charlie.sync(bob)
        self.assertEqual(charlie.counter.value, 120)  # 100 - 30 + 50
        knowledge_items = charlie.knowledge.elements()
        self.assertEqual(len(knowledge_items), 3)

        # Final complete convergence across all 3 agents
        alice.sync(charlie)
        bob.sync(charlie)
        self.assertEqual(alice.counter.value, 120)
        self.assertEqual(bob.counter.value, 120)
        self.assertEqual(alice.knowledge.elements(), bob.knowledge.elements())
        self.assertEqual(bob.knowledge.elements(), charlie.knowledge.elements())

    def test_protocol_serialization_roundtrip(self):
        p = DecentralizedStateSyncProtocol("agent-p")
        p.increment(99)
        d = p.to_dict()
        restored = DecentralizedStateSyncProtocol.from_dict(d, "agent-p")
        self.assertEqual(restored.counter.value, 99)


if __name__ == "__main__":
    unittest.main()
