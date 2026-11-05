from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ..config import settings
from .contracts import QuerySpecError, UserDecision, plan_scope_hash
from .templates import CandidateSet
from .values import resolve_value


class ConfirmationError(ValueError):
    pass


@dataclass(frozen=True)
class ConfirmationTicket:
    token: str
    plan_scope_hash: str
    schema_snapshot: str
    candidate_ids: dict[str, tuple[str, ...]]
    template_versions: dict[str, int]
    expires_at: float


@dataclass(frozen=True)
class _TicketRecord:
    ticket: ConfirmationTicket
    candidate_set: CandidateSet
    created_at: float


_TICKETS: dict[str, _TicketRecord] = {}


def _ttl_seconds() -> float:
    return float(settings.capability_token_ttl_minutes or 0) * 60


def _purge_expired(now: float) -> None:
    for token, record in list(_TICKETS.items()):
        if now - record.created_at > _ttl_seconds():
            _TICKETS.pop(token, None)


def issue_confirmation(
    plan: Any,
    candidate_set: CandidateSet,
    schema_snapshot: str,
) -> ConfirmationTicket:
    now = time.time()
    _purge_expired(now)
    token = uuid.uuid4().hex
    candidate_ids: dict[str, list[str]] = {}
    versions: dict[str, int] = {}
    for candidate in candidate_set.candidates:
        candidate_ids.setdefault(candidate.node_id, []).append(candidate.candidate_id)
        versions[candidate.node_id] = candidate.contract.version
    ticket = ConfirmationTicket(
        token=token,
        plan_scope_hash=plan_scope_hash(plan),
        schema_snapshot=str(schema_snapshot),
        candidate_ids={key: tuple(value) for key, value in candidate_ids.items()},
        template_versions=versions,
        expires_at=now + _ttl_seconds(),
    )
    _TICKETS[token] = _TicketRecord(
        ticket=ticket,
        candidate_set=candidate_set,
        created_at=now,
    )
    return ticket


def consume_confirmation(
    token: str,
    choices: Any,
    overrides: dict[str, dict[str, Any]],
    value_resolver: Callable[..., Any] | None = None,
    scope_hash: str | None = None,
) -> UserDecision:
    record = _TICKETS.pop(str(token or ""), None)
    if record is None:
        raise ConfirmationError("confirmation token missing or already used")
    if time.time() - record.created_at > _ttl_seconds():
        raise ConfirmationError("confirmation token expired")
    ticket = record.ticket
    if scope_hash is not None and scope_hash != ticket.plan_scope_hash:
        raise ConfirmationError("scope hash changed after confirmation was issued")
    choice_map = dict(choices or {}) if not isinstance(choices, dict) else dict(choices)
    unknown_nodes = set(choice_map) - set(ticket.candidate_ids)
    if unknown_nodes:
        raise ConfirmationError(
            "confirmation choices contain unknown nodes: "
            + "、".join(sorted(unknown_nodes))
        )
    for node_id, candidate_ids in ticket.candidate_ids.items():
        picked = str(choice_map.get(node_id) or "")
        if picked not in candidate_ids:
            raise ConfirmationError(f"candidate confirmation missing for node {node_id}")
    resolver = value_resolver or resolve_value
    for node_id, values in (overrides or {}).items():
        if not isinstance(values, dict):
            raise ConfirmationError(f"overrides for node {node_id} must be an object")
        node = next((item for item in record.candidate_set.candidates if item.node_id == node_id), None)
        if node is None:
            raise ConfirmationError(f"override node not present in candidate set: {node_id}")
        kind = node.contract.kind
        for field_name, raw_value in values.items():
            try:
                resolver(field_name, "=", raw_value, "", kind)
            except QuerySpecError as exc:
                raise ConfirmationError(f"override rejected for {field_name}: {exc}") from exc
    return UserDecision(
        token=str(token),
        choices=choice_map,
        overrides={key: dict(value) for key, value in (overrides or {}).items()},
    )
