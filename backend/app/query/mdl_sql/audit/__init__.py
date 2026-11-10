import json

from ...capability_registry.models import MdlCompilationPolicy
from ..models import (
    MdlQueryContract,
    MdlSnapshot,
    MdlSqlCandidate,
    RelationshipSubgraph,
    SqlAuditReport,
)
from .ast import parse_mdl_sql
from .contract_audit import audit_contract
from .failures import sql_issue
from .relationship_audit import (
    audit_relationships,
    relationship_audit_details,
)
from .safety_audit import audit_safety


def audit_mdl_sql(
    candidate: MdlSqlCandidate,
    contract: MdlQueryContract,
    subgraph: RelationshipSubgraph,
    snapshot: MdlSnapshot,
    policy: MdlCompilationPolicy,
) -> SqlAuditReport:
    kind = (
        "postgres"
        if str(snapshot.source_dir).lower().find("postgres") >= 0
        else "mysql"
    )
    parsed = parse_mdl_sql(candidate.mdl_sql, kind)
    contract_issues = audit_contract(parsed, contract)
    relationship_issues, used_relationships = relationship_audit_details(
        parsed,
        contract,
        subgraph,
    )
    safety_issues = audit_safety(
        parsed,
        contract,
        snapshot,
        policy,
    )
    issues = _deduplicate(
        (
            *contract_issues,
            *relationship_issues,
            *safety_issues,
        )
    )
    return SqlAuditReport(
        accepted=bool(parsed.valid) and not issues,
        issues=issues,
        root_model=contract.root_model if parsed.valid else "",
        used_models=tuple(
            sorted(
                {
                    table
                    for table in parsed.table_aliases.values()
                    if table in contract.allowed_models
                }
            )
        ),
        used_fields=parsed.qualified_columns,
        used_relationships=used_relationships,
        normalized_structure={
            "root_query_type": parsed.root_query_type,
            "projections": list(parsed.projections),
            "group_by": list(parsed.group_expressions),
            "order_by": list(parsed.order_expressions),
            "limit": parsed.limit,
        },
    )


def _deduplicate(issues):
    seen = set()
    result = []
    for issue in issues:
        key = (
            issue.code,
            issue.path,
            json.dumps(
                issue.evidence,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return tuple(result)


__all__ = [
    "audit_contract",
    "audit_mdl_sql",
    "audit_relationships",
    "audit_safety",
    "parse_mdl_sql",
    "sql_issue",
]
