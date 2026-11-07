#!/usr/bin/env python3
"""Offline peer-to-peer state replication using CRDTs and a Merkle DAG.

The module is deliberately self contained so it can be used by an agent that
has no coordinator available.  A peer records a content addressed snapshot of
its state in a Merkle DAG.  Peers can exchange those snapshots whenever a
connection becomes available; the CRDT join operation makes the resulting
state independent of message order, duplication, or the number of times a
peer reconnects.

The public building blocks are :class:`PNCounter`,
:class:`LWWElementSet`, :class:`KnowledgeState`, :class:`MerkleDAG`, and
:class:`DecentralizedStateSyncProtocol`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


HASH_LENGTH = 64


def canonical_json(value: Any) -> str:
    """Return the deterministic JSON representation used for content hashes."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value must be JSON serializable without NaN or infinity") from error


def content_hash(value: Any) -> str:
    """Hash a JSON value with SHA-256 and return a lower-case hex digest."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _replica_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("replica_id must be a non-empty string")
    return value


def _counter_amount(value: Any, name: str = "amount") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, order=True)
class Timestamp:
    """Lamport timestamp with a deterministic replica tie breaker."""

    counter: int
    replica_id: str

    def __post_init__(self) -> None:
        if isinstance(self.counter, bool) or not isinstance(self.counter, int) or self.counter < 0:
            raise ValueError("timestamp counter must be a non-negative integer")
        _replica_id(self.replica_id)

    def to_list(self) -> list[Any]:
        return [self.counter, self.replica_id]

    @classmethod
    def from_value(cls, value: Any, default_replica: str) -> "Timestamp":
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            raise ValueError("timestamp must be an integer or [counter, replica_id]")
        if isinstance(value, int):
            return cls(value, default_replica)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            counter, replica = value
            if isinstance(counter, bool) or not isinstance(counter, int):
                raise ValueError("timestamp counter must be an integer")
            return cls(counter, replica)
        raise ValueError("timestamp must be an integer or [counter, replica_id]")


class PNCounter:
    """A positive-negative counter CRDT.

    Each replica owns one entry in the positive and negative maps.  Merging
    takes a component-wise maximum, which makes the operation associative,
    commutative, and idempotent while still allowing decrements offline.
    """

    def __init__(
        self,
        replica_id: str,
        positive: Mapping[str, int] | None = None,
        negative: Mapping[str, int] | None = None,
    ) -> None:
        self.replica_id = _replica_id(replica_id)
        self.positive = self._validate_map(positive or {}, "positive")
        self.negative = self._validate_map(negative or {}, "negative")

    @staticmethod
    def _validate_map(values: Mapping[str, int], name: str) -> dict[str, int]:
        if not isinstance(values, Mapping):
            raise ValueError(f"{name} must be a mapping")
        result: dict[str, int] = {}
        for replica, amount in values.items():
            _replica_id(replica)
            result[replica] = _counter_amount(amount, f"{name}[{replica!r}]")
        return result

    @property
    def value(self) -> int:
        return sum(self.positive.values()) - sum(self.negative.values())

    def increment(self, amount: int = 1) -> "PNCounter":
        amount = _counter_amount(amount)
        if amount:
            self.positive[self.replica_id] = self.positive.get(self.replica_id, 0) + amount
        return self

    def decrement(self, amount: int = 1) -> "PNCounter":
        amount = _counter_amount(amount)
        if amount:
            self.negative[self.replica_id] = self.negative.get(self.replica_id, 0) + amount
        return self

    def copy(self) -> "PNCounter":
        return PNCounter(self.replica_id, self.positive, self.negative)

    def merge(self, other: "PNCounter") -> "PNCounter":
        if not isinstance(other, PNCounter):
            raise TypeError("can only merge a PNCounter")
        for target, source in ((self.positive, other.positive), (self.negative, other.negative)):
            for replica, amount in source.items():
                target[replica] = max(target.get(replica, 0), amount)
        return self

    def joined(self, other: "PNCounter") -> "PNCounter":
        return self.copy().merge(other)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "pn-counter",
            "positive": {key: self.positive[key] for key in sorted(self.positive)},
            "negative": {key: self.negative[key] for key in sorted(self.negative)},
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], replica_id: str) -> "PNCounter":
        if not isinstance(value, Mapping) or value.get("type") != "pn-counter":
            raise ValueError("invalid PN-counter state")
        return cls(replica_id, value.get("positive", {}), value.get("negative", {}))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PNCounter) and self.positive == other.positive and self.negative == other.negative

    def __repr__(self) -> str:
        return f"PNCounter(replica_id={self.replica_id!r}, value={self.value})"


class LWWElementSet:
    """Last-write-wins element set with add and remove tombstones.

    A timestamp is ``(Lamport counter, replica id)``.  Replica id breaks ties
    consistently across peers.  A remove wins an exact timestamp tie, which
    avoids resurrecting an element when an add and remove are delivered in a
    different order.
    """

    def __init__(self, replica_id: str, *, clock: int = 0) -> None:
        self.replica_id = _replica_id(replica_id)
        if isinstance(clock, bool) or not isinstance(clock, int) or clock < 0:
            raise ValueError("clock must be a non-negative integer")
        self.clock = clock
        self.adds: dict[str, tuple[Any, Timestamp]] = {}
        self.removes: dict[str, tuple[Any, Timestamp]] = {}

    @staticmethod
    def _key(element: Any) -> str:
        return canonical_json(element)

    def _next_timestamp(self) -> Timestamp:
        self.clock += 1
        return Timestamp(self.clock, self.replica_id)

    def _timestamp(self, value: Any | None) -> Timestamp:
        timestamp = self._next_timestamp() if value is None else Timestamp.from_value(value, self.replica_id)
        self.clock = max(self.clock, timestamp.counter)
        return timestamp

    def _record(self, target: MutableMapping[str, tuple[Any, Timestamp]], element: Any, timestamp: Timestamp) -> Timestamp:
        key = self._key(element)
        current = target.get(key)
        if current is None or timestamp > current[1]:
            target[key] = (copy.deepcopy(element), timestamp)
        return timestamp

    def add(self, element: Any, timestamp: Any | None = None) -> Timestamp:
        return self._record(self.adds, element, self._timestamp(timestamp))

    def remove(self, element: Any, timestamp: Any | None = None) -> Timestamp:
        return self._record(self.removes, element, self._timestamp(timestamp))

    def contains(self, element: Any) -> bool:
        key = self._key(element)
        added = self.adds.get(key)
        if added is None:
            return False
        removed = self.removes.get(key)
        return removed is None or added[1] > removed[1]

    def elements(self) -> set[Any] | list[Any]:
        values = [entry[0] for key, entry in self.adds.items() if self.contains(entry[0])]
        try:
            return set(values)
        except TypeError:
            # Knowledge records may be JSON objects, which are not hashable.
            return [copy.deepcopy(value) for value in sorted(values, key=self._key)]

    def values(self) -> list[Any]:
        result = [entry[0] for key, entry in self.adds.items() if self.contains(entry[0])]
        return [copy.deepcopy(value) for value in sorted(result, key=self._key)]

    def __contains__(self, element: Any) -> bool:
        return self.contains(element)

    def __len__(self) -> int:
        return sum(1 for key, entry in self.adds.items() if self.contains(entry[0]))

    def copy(self) -> "LWWElementSet":
        result = LWWElementSet(self.replica_id, clock=self.clock)
        result.adds = copy.deepcopy(self.adds)
        result.removes = copy.deepcopy(self.removes)
        return result

    def merge(self, other: "LWWElementSet") -> "LWWElementSet":
        if not isinstance(other, LWWElementSet):
            raise TypeError("can only merge an LWWElementSet")
        self.clock = max(self.clock, other.clock)
        for target, source in ((self.adds, other.adds), (self.removes, other.removes)):
            for key, entry in source.items():
                if key not in target or entry[1] > target[key][1]:
                    target[key] = copy.deepcopy(entry)
                self.clock = max(self.clock, entry[1].counter)
        return self

    def joined(self, other: "LWWElementSet") -> "LWWElementSet":
        return self.copy().merge(other)

    @staticmethod
    def _serialize_entries(entries: Mapping[str, tuple[Any, Timestamp]]) -> dict[str, Any]:
        return {
            key: {"element": copy.deepcopy(entries[key][0]), "timestamp": entries[key][1].to_list()}
            for key in sorted(entries)
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "lww-element-set",
            "clock": self.clock,
            "adds": self._serialize_entries(self.adds),
            "removes": self._serialize_entries(self.removes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], replica_id: str) -> "LWWElementSet":
        if not isinstance(value, Mapping) or value.get("type") != "lww-element-set":
            raise ValueError("invalid LWW-element-set state")
        result = cls(replica_id, clock=value.get("clock", 0))
        for field, target in (("adds", result.adds), ("removes", result.removes)):
            entries = value.get(field, {})
            if not isinstance(entries, Mapping):
                raise ValueError(f"{field} must be a mapping")
            for key, entry in entries.items():
                if not isinstance(key, str) or not isinstance(entry, Mapping):
                    raise ValueError("invalid LWW entry")
                if "element" not in entry or "timestamp" not in entry:
                    raise ValueError("invalid LWW entry")
                element = entry["element"]
                if result._key(element) != key:
                    raise ValueError("LWW entry key does not match its element")
                timestamp = Timestamp.from_value(entry["timestamp"], replica_id)
                target[key] = (copy.deepcopy(element), timestamp)
                result.clock = max(result.clock, timestamp.counter)
        return result

    def __eq__(self, other: object) -> bool:
        return isinstance(other, LWWElementSet) and self.adds == other.adds and self.removes == other.removes


class KnowledgeState:
    """The CRDT state replicated by an agent."""

    def __init__(
        self,
        replica_id: str,
        counter: PNCounter | None = None,
        knowledge: LWWElementSet | None = None,
    ) -> None:
        self.replica_id = _replica_id(replica_id)
        self.counter = counter.copy() if counter is not None else PNCounter(self.replica_id)
        self.knowledge = knowledge.copy() if knowledge is not None else LWWElementSet(self.replica_id)

    @property
    def pn_counter(self) -> PNCounter:
        return self.counter

    @property
    def knowledge_set(self) -> LWWElementSet:
        return self.knowledge

    def increment(self, amount: int = 1) -> "KnowledgeState":
        self.counter.increment(amount)
        return self

    def decrement(self, amount: int = 1) -> "KnowledgeState":
        self.counter.decrement(amount)
        return self

    def add_knowledge(self, element: Any, timestamp: Any | None = None) -> Timestamp:
        return self.knowledge.add(element, timestamp)

    def remove_knowledge(self, element: Any, timestamp: Any | None = None) -> Timestamp:
        return self.knowledge.remove(element, timestamp)

    def merge(self, other: "KnowledgeState") -> "KnowledgeState":
        if not isinstance(other, KnowledgeState):
            raise TypeError("can only merge a KnowledgeState")
        self.counter.merge(other.counter)
        self.knowledge.merge(other.knowledge)
        return self

    def joined(self, other: "KnowledgeState") -> "KnowledgeState":
        return self.copy().merge(other)

    def copy(self) -> "KnowledgeState":
        return KnowledgeState(self.replica_id, self.counter, self.knowledge)

    def to_dict(self) -> dict[str, Any]:
        return {"counter": self.counter.to_dict(), "knowledge": self.knowledge.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], replica_id: str) -> "KnowledgeState":
        if not isinstance(value, Mapping):
            raise ValueError("state must be a mapping")
        return cls(
            replica_id,
            PNCounter.from_dict(value.get("counter", {}), replica_id),
            LWWElementSet.from_dict(value.get("knowledge", {}), replica_id),
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, KnowledgeState) and self.counter == other.counter and self.knowledge == other.knowledge


@dataclass(frozen=True)
class MerkleNode:
    """A content-addressed state snapshot and its parent links."""

    node_id: str
    parents: tuple[str, ...]
    state: dict[str, Any]
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        if len(self.node_id) != HASH_LENGTH or any(character not in "0123456789abcdef" for character in self.node_id):
            raise ValueError("node_id must be a SHA-256 hex digest")
        if tuple(sorted(set(self.parents))) != self.parents:
            raise ValueError("parents must be sorted and unique")
        if any(not isinstance(parent, str) or len(parent) != HASH_LENGTH for parent in self.parents):
            raise ValueError("parents must be SHA-256 hex digests")
        if not isinstance(self.state, dict) or not isinstance(self.metadata, dict):
            raise ValueError("node state and metadata must be objects")
        expected = self.calculate_id(self.parents, self.state, self.metadata)
        if self.node_id != expected:
            raise ValueError("node_id does not match node contents")

    @staticmethod
    def calculate_id(parents: Iterable[str], state: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
        return content_hash({
            "parents": sorted(set(parents)),
            "state": state,
            "metadata": metadata,
        })

    @classmethod
    def create(
        cls,
        parents: Iterable[str],
        state: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> "MerkleNode":
        normalized_parents = tuple(sorted(set(parents)))
        normalized_state = copy.deepcopy(dict(state))
        normalized_metadata = copy.deepcopy(dict(metadata or {}))
        node_id = cls.calculate_id(normalized_parents, normalized_state, normalized_metadata)
        return cls(node_id, normalized_parents, normalized_state, normalized_metadata)

    @property
    def hash(self) -> str:
        return self.node_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "parents": list(self.parents),
            "state": copy.deepcopy(self.state),
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MerkleNode":
        if not isinstance(value, Mapping):
            raise ValueError("Merkle node must be an object")
        node_id = value.get("id", value.get("hash"))
        parents = value.get("parents", [])
        if not isinstance(node_id, str) or not isinstance(parents, (list, tuple)):
            raise ValueError("invalid Merkle node")
        state = value.get("state")
        metadata = value.get("metadata", {})
        if not isinstance(state, dict) or not isinstance(metadata, dict):
            raise ValueError("invalid Merkle node state or metadata")
        return cls(node_id, tuple(parents), state, metadata)


def _empty_state(replica_id: str = "genesis") -> KnowledgeState:
    return KnowledgeState(replica_id)


class MerkleDAG:
    """Content-addressed DAG of CRDT snapshots for one offline peer."""

    def __init__(self, replica_id: str) -> None:
        self.replica_id = _replica_id(replica_id)
        self.nodes: dict[str, MerkleNode] = {}
        self._heads: set[str] = set()
        genesis = MerkleNode.create((), _empty_state().to_dict(), {"kind": "genesis"})
        self._insert(genesis)
        self.genesis = genesis.node_id

    def _insert(self, node: MerkleNode) -> bool:
        existing = self.nodes.get(node.node_id)
        if existing is not None:
            if existing != node:
                raise ValueError("conflicting contents for a Merkle node id")
            return False
        self.nodes[node.node_id] = node
        self._heads.add(node.node_id)
        self._heads.difference_update(node.parents)
        return True

    @property
    def heads(self) -> set[str]:
        return set(self._heads)

    @property
    def head_hashes(self) -> set[str]:
        return self.heads

    @property
    def state(self) -> KnowledgeState:
        result = KnowledgeState(self.replica_id)
        for node_id in sorted(self._heads):
            result.merge(KnowledgeState.from_dict(self.nodes[node_id].state, self.replica_id))
        return result

    @property
    def current_state(self) -> KnowledgeState:
        return self.state

    @property
    def state_digest(self) -> str:
        return content_hash(self.state.to_dict())

    def commit(
        self,
        state: KnowledgeState | Mapping[str, Any] | None = None,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> MerkleNode:
        if state is None:
            snapshot = self.state
        elif isinstance(state, KnowledgeState):
            snapshot = state
        elif isinstance(state, Mapping):
            snapshot = KnowledgeState.from_dict(state, self.replica_id)
        else:
            raise TypeError("state must be a KnowledgeState, mapping, or None")
        node = MerkleNode.create(self._heads, snapshot.to_dict(), metadata)
        self._insert(node)
        return node

    def _update(self, operation: str, mutate: Any, **metadata: Any) -> MerkleNode:
        state = self.state
        mutate(state)
        return self.commit(state, metadata={"operation": operation, **metadata})

    def increment(self, amount: int = 1) -> MerkleNode:
        return self._update("increment", lambda state: state.increment(amount), amount=amount)

    def decrement(self, amount: int = 1) -> MerkleNode:
        return self._update("decrement", lambda state: state.decrement(amount), amount=amount)

    def add_knowledge(self, element: Any, timestamp: Any | None = None) -> MerkleNode:
        return self._update(
            "add-knowledge",
            lambda state: state.add_knowledge(element, timestamp),
            element=copy.deepcopy(element),
        )

    def remove_knowledge(self, element: Any, timestamp: Any | None = None) -> MerkleNode:
        return self._update(
            "remove-knowledge",
            lambda state: state.remove_knowledge(element, timestamp),
            element=copy.deepcopy(element),
        )

    def add_node(self, node: MerkleNode | Mapping[str, Any]) -> bool:
        parsed = node if isinstance(node, MerkleNode) else MerkleNode.from_dict(node)
        return self._insert(parsed)

    def merge(self, other: "MerkleDAG | Iterable[MerkleNode | Mapping[str, Any]]") -> int:
        if isinstance(other, MerkleDAG):
            nodes: Iterable[MerkleNode | Mapping[str, Any]] = other.nodes.values()
        else:
            nodes = other
        pending = list(nodes)
        added = 0
        # Repeated passes make the resulting head set correct when nodes are
        # supplied in child-before-parent order.
        for node in pending:
            if self.add_node(node):
                added += 1
        self._recompute_heads()
        return added

    def _recompute_heads(self) -> None:
        referenced = {parent for node in self.nodes.values() for parent in node.parents}
        self._heads = set(self.nodes) - referenced

    def sync(self, peer: "MerkleDAG") -> int:
        """Exchange all missing nodes with ``peer`` in both directions."""

        if not isinstance(peer, MerkleDAG):
            raise TypeError("peer must be a MerkleDAG")
        before = len(self.nodes) + len(peer.nodes)
        self.merge(peer)
        peer.merge(self)
        # A node received by the first peer is now also available to the
        # second; the second merge above is sufficient because self is unioned.
        return len(self.nodes) + len(peer.nodes) - before

    synchronize = sync

    def missing_hashes(self, known: "MerkleDAG | Iterable[str]") -> set[str]:
        known_ids = set(known.nodes) if isinstance(known, MerkleDAG) else set(known)
        return set(self.nodes) - known_ids

    def export_nodes(self, *, include_genesis: bool = True) -> list[dict[str, Any]]:
        selected = set(self.nodes)
        if not include_genesis:
            selected.discard(self.genesis)
        return [self.nodes[node_id].to_dict() for node_id in self._topological_ids(selected)]

    def _topological_ids(self, selected: set[str]) -> list[str]:
        result: list[str] = []
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited or node_id not in selected:
                return
            visited.add(node_id)
            for parent in self.nodes[node_id].parents:
                visit(parent)
            result.append(node_id)

        for node_id in sorted(selected):
            visit(node_id)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "genesis": self.genesis,
            "heads": sorted(self._heads),
            "nodes": self.export_nodes(),
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], replica_id: str | None = None) -> "MerkleDAG":
        if not isinstance(value, Mapping) or not isinstance(value.get("nodes"), list):
            raise ValueError("invalid Merkle DAG payload")
        peer = cls(replica_id or value.get("replica_id", "imported-peer"))
        peer.nodes = {}
        peer._heads = set()
        for raw_node in value["nodes"]:
            peer.add_node(raw_node)
        peer._recompute_heads()
        if not peer.nodes:
            raise ValueError("Merkle DAG must contain at least one node")
        supplied_genesis = value.get("genesis")
        if supplied_genesis is not None and supplied_genesis not in peer.nodes:
            raise ValueError("unknown genesis node")
        peer.genesis = supplied_genesis or min(peer.nodes)
        supplied_heads = value.get("heads")
        if supplied_heads is not None and set(supplied_heads) != peer._heads:
            raise ValueError("head list does not match DAG contents")
        return peer

    @classmethod
    def from_json(cls, payload: str, replica_id: str | None = None) -> "MerkleDAG":
        try:
            value = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid Merkle DAG JSON") from error
        return cls.from_dict(value, replica_id)


class DecentralizedStateSyncProtocol:
    """Convenience facade for an agent's local CRDT and peer synchronization."""

    def __init__(self, replica_id: str) -> None:
        self.replica_id = _replica_id(replica_id)
        self.dag = MerkleDAG(self.replica_id)

    @property
    def state(self) -> KnowledgeState:
        return self.dag.state

    @property
    def counter(self) -> PNCounter:
        return self.state.counter

    @property
    def knowledge(self) -> LWWElementSet:
        return self.state.knowledge

    @property
    def heads(self) -> set[str]:
        return self.dag.heads

    def increment(self, amount: int = 1) -> MerkleNode:
        return self.dag.increment(amount)

    def decrement(self, amount: int = 1) -> MerkleNode:
        return self.dag.decrement(amount)

    def add_knowledge(self, element: Any, timestamp: Any | None = None) -> MerkleNode:
        return self.dag.add_knowledge(element, timestamp)

    def remove_knowledge(self, element: Any, timestamp: Any | None = None) -> MerkleNode:
        return self.dag.remove_knowledge(element, timestamp)

    def sync(self, peer: "DecentralizedStateSyncProtocol") -> int:
        if not isinstance(peer, DecentralizedStateSyncProtocol):
            raise TypeError("peer must be a DecentralizedStateSyncProtocol")
        return self.dag.sync(peer.dag)

    synchronize = sync

    def export_sync_message(self) -> dict[str, Any]:
        return self.dag.to_dict()

    def receive_sync_message(self, message: Mapping[str, Any] | str) -> int:
        incoming = MerkleDAG.from_json(message, self.replica_id) if isinstance(message, str) else MerkleDAG.from_dict(message, self.replica_id)
        return self.dag.merge(incoming)

    def to_dict(self) -> dict[str, Any]:
        return self.dag.to_dict()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], replica_id: str | None = None) -> "DecentralizedStateSyncProtocol":
        protocol = cls(replica_id or value.get("replica_id", "imported-peer"))
        protocol.dag = MerkleDAG.from_dict(value, protocol.replica_id)
        return protocol


# Friendly aliases for callers that use the shorter names in design docs.
AgentKnowledgeState = KnowledgeState
PeerStateSync = DecentralizedStateSyncProtocol
StateSyncProtocol = DecentralizedStateSyncProtocol


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("replica_id", nargs="?", default="local-agent")
    args = parser.parse_args(argv)
    protocol = DecentralizedStateSyncProtocol(args.replica_id)
    print(json.dumps(protocol.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
