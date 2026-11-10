from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from ..errors import MdlSqlRelationshipError
from ..models import MdlRelationship, MdlSnapshot, RelationshipSubgraph


def build_relationship_subgraph(
    snapshot: MdlSnapshot,
    root_model: str,
    required_models: tuple[str, ...],
    required_relationship_ids: tuple[str, ...],
    max_hops: int,
) -> RelationshipSubgraph:
    root = str(root_model or "")
    targets = tuple(dict.fromkeys(str(item) for item in required_models if item))
    required_ids = tuple(
        dict.fromkeys(str(item) for item in required_relationship_ids if item)
    )
    if root not in snapshot.models:
        _raise_relationship(
            "unknown_model",
            root_model=root,
            required_model="",
        )
    unknown_models = sorted(set(targets) - set(snapshot.models))
    if unknown_models:
        _raise_relationship(
            "unknown_model",
            root_model=root,
            required_model=unknown_models[0],
        )
    missing_ids = sorted(
        set(required_ids) - set(snapshot.relationships)
    )
    if missing_ids:
        _raise_relationship(
            "required_relationship_not_found",
            root_model=root,
            required_model="",
            candidate_relationship_ids=missing_ids,
        )

    fixed_relationships = {
        relationship_id: snapshot.relationships[relationship_id]
        for relationship_id in required_ids
    }
    parent = {model: model for model in snapshot.models}

    def find(model: str) -> str:
        current = model
        while parent[current] != current:
            parent[current] = parent[parent[current]]
            current = parent[current]
        return current

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for relationship in fixed_relationships.values():
        union(*relationship.models)

    component_edges: dict[
        str,
        list[tuple[str, MdlRelationship]],
    ] = defaultdict(list)
    for relationship in snapshot.relationships.values():
        if relationship.relationship_id in fixed_relationships:
            continue
        left = find(relationship.models[0])
        right = find(relationship.models[1])
        if left == right:
            continue
        component_edges[left].append((right, relationship))
        component_edges[right].append((left, relationship))
    for edges in component_edges.values():
        edges.sort(key=lambda item: item[1].relationship_id)

    selected = dict(fixed_relationships)
    root_component = find(root)
    for target in targets:
        target_component = find(target)
        if target_component == root_component:
            continue
        candidates = _component_paths(
            component_edges,
            start=root_component,
            target=target_component,
            max_hops=max_hops,
        )
        if not candidates:
            _raise_relationship(
                "unreachable_within_max_hops",
                root_model=root,
                required_model=target,
                max_hops=max_hops,
            )
        if len(candidates) > 1:
            _raise_relationship(
                "multiple_paths",
                root_model=root,
                required_model=target,
                candidate_relationship_ids=[
                    sorted(relationship_id for relationship_id, _ in path)
                    for path in candidates
                ],
            )
        for relationship_id, relationship in candidates[0]:
            selected[relationship_id] = relationship

    selected_relationships = tuple(
        selected[key] for key in sorted(selected)
    )
    for target in targets:
        hops = _shortest_hop_count(
            root,
            target,
            selected_relationships,
        )
        if hops is None or hops > max_hops:
            _raise_relationship(
                "unreachable_within_max_hops",
                root_model=root,
                required_model=target,
                max_hops=max_hops,
                observed_hops=hops,
            )

    selected_models = {root, *targets}
    for relationship in selected_relationships:
        selected_models.update(relationship.models)
    return RelationshipSubgraph(
        root_model=root,
        models=tuple(sorted(selected_models)),
        relationships=selected_relationships,
    )


def _component_paths(
    adjacency: dict[str, list[tuple[str, MdlRelationship]]],
    *,
    start: str,
    target: str,
    max_hops: int,
) -> list[tuple[tuple[str, MdlRelationship], ...]]:
    if max_hops < 0:
        return []
    results: dict[
        frozenset[str],
        tuple[tuple[str, MdlRelationship], ...],
    ] = {}

    def visit(
        current: str,
        visited: set[str],
        path: tuple[tuple[str, MdlRelationship], ...],
    ) -> None:
        if len(path) > max_hops:
            return
        if current == target:
            key = frozenset(
                relationship.relationship_id
                for _, relationship in path
            )
            results.setdefault(key, path)
            return
        for next_component, relationship in adjacency.get(current, ()):
            if next_component in visited:
                continue
            visit(
                next_component,
                {*visited, next_component},
                (*path, (relationship.relationship_id, relationship)),
            )

    visit(start, {start}, ())
    return list(results.values())


def _shortest_hop_count(
    root: str,
    target: str,
    relationships: Iterable[MdlRelationship],
) -> int | None:
    if root == target:
        return 0
    adjacency: dict[str, list[str]] = defaultdict(list)
    for relationship in relationships:
        left, right = relationship.models
        adjacency[left].append(right)
        adjacency[right].append(left)

    queue = deque([(root, 0)])
    visited = {root}
    while queue:
        model, hops = queue.popleft()
        for neighbor in adjacency.get(model, ()):
            if neighbor == target:
                return hops + 1
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append((neighbor, hops + 1))
    return None


def _raise_relationship(
    reason: str,
    **evidence: object,
) -> None:
    raise MdlSqlRelationshipError(
        "relationship_path_ambiguous",
        {
            "reason": reason,
            **evidence,
        },
    )
