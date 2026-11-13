"""Render a Graph to Mermaid or DOT. Output is deterministic."""

from __future__ import annotations

import re

from kew.graph.domain import Graph, NodeStatus

_MERMAID_CLASSDEF = {
    NodeStatus.DONE: "classDef done fill:#c6f6d5,stroke:#22543d;",
    NodeStatus.READY: "classDef ready fill:#bee3f8,stroke:#2a4365;",
    NodeStatus.IN_PROGRESS: "classDef in_progress fill:#faf089,stroke:#744210;",
    NodeStatus.IN_REVIEW: "classDef in_review fill:#e9d8fd,stroke:#44337a;",
    NodeStatus.BLOCKED: "classDef blocked fill:#fed7d7,stroke:#742a2a;",
    NodeStatus.FAILED: "classDef failed fill:#feb2b2,stroke:#742a2a;",
    NodeStatus.TODO: "classDef todo fill:#edf2f7,stroke:#2d3748;",
    NodeStatus.QUEUED: "classDef queued fill:#feebc8,stroke:#7b341e;",
}


def _mermaid_id(node_id: str) -> str:
    return "n" + re.sub(r"[^0-9A-Za-z_]", "_", node_id)


def _mermaid_id_map(graph: Graph) -> dict[str, str]:
    """Assign every id a UNIQUE Mermaid-safe id.

    Sanitizing each id independently can collide (e.g. ``ENG-1`` and ``ENG_1``
    both sanitize to ``nENG_1``), which would merge distinct nodes/edges in the
    diagram. Build one stable per-graph mapping instead, disambiguating
    collisions deterministically. Covers edge endpoints too (a dangling edge may
    reference an id that is not a node).
    """
    endpoints = {e.src for e in graph.edges} | {e.dst for e in graph.edges}
    ordered = sorted(graph.nodes) + sorted(endpoints - set(graph.nodes))
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for nid in ordered:
        candidate = base = _mermaid_id(nid)
        i = 1
        while candidate in used:
            candidate = f"{base}__{i}"
            i += 1
        used.add(candidate)
        mapping[nid] = candidate
    return mapping


def _mermaid_label(text: str) -> str:
    """Escape characters significant in a Mermaid ``["..."]`` node label."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _mermaid(graph: Graph) -> str:
    mid = _mermaid_id_map(graph)
    lines = ["graph TD"]
    for nid in sorted(graph.nodes):
        node = graph.nodes[nid]
        label = _mermaid_label(node.title)
        lines.append(f'    {mid[nid]}["#{nid} {label}"]:::{node.status.value}')
    for e in sorted(graph.edges, key=lambda x: (x.src, x.dst)):
        lines.append(f"    {mid[e.src]} --> {mid[e.dst]}")
    for classdef in _MERMAID_CLASSDEF.values():
        lines.append(f"    {classdef}")
    return "\n".join(lines)


def _dot(graph: Graph) -> str:
    lines = ["digraph epic {"]
    for nid in sorted(graph.nodes):
        node = graph.nodes[nid]
        label = node.title.replace('"', "'")
        lines.append(f'    "{nid}" [label="#{nid} {label}\\n{node.status.value}"];')
    for e in sorted(graph.edges, key=lambda x: (x.src, x.dst)):
        lines.append(f'    "{e.src}" -> "{e.dst}";')
    lines.append("}")
    return "\n".join(lines)


def render(graph: Graph, fmt: str = "mermaid") -> str:
    if fmt == "mermaid":
        return _mermaid(graph)
    if fmt == "dot":
        return _dot(graph)
    raise ValueError(f"unknown format: {fmt!r} (expected 'mermaid' or 'dot')")
