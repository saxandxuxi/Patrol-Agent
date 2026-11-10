from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .models import CompilerFailure


_FAILURE_MESSAGES = {
    "contract_build_failed": "查询契约构建失败",
    "context_budget_exceeded": "查询所需上下文超过限制",
    "relationship_path_ambiguous": "关联路径不唯一，无法安全执行",
    "compiler_not_covered": "确定性编译器暂不支持该查询",
    "compiler_exception": "SQL 编译出现内部错误",
    "root_subject_mismatch": "查询主体与根模型不一致",
    "unexpected_model": "SQL 使用了未授权模型",
    "unexpected_field": "SQL 使用了未授权字段",
    "missing_filter": "SQL 缺少查询契约要求的条件",
    "extra_filter": "SQL 增加了查询契约之外的条件",
    "filter_value_mismatch": "SQL 的过滤值与查询契约不一致",
    "illegal_relationship": "SQL 使用了未登记关联",
    "cardinality_risk": "关联基数可能导致结果重复或放大",
    "invalid_view_shape": "查询结果结构与请求视图不一致",
    "missing_output": "SQL 缺少查询契约要求的输出",
    "order_required": "排名或极值查询缺少排序",
    "invalid_limit": "返回条数不符合查询契约",
    "physical_table_reference": "SQL 直接引用了物理表",
    "soft_delete_missing": "SQL 缺少有效数据过滤条件",
    "sql_parse_failed": "SQL 无法解析",
    "multiple_statements": "只允许执行一条 SQL",
    "forbidden_sql_type": "SQL 包含已禁止的语句类型",
    "dangerous_function": "SQL 包含已禁止的危险函数",
    "star_projection": "明细查询不允许无条件返回全部列",
    "dry_plan_failed": "Wren 语义 SQL 编译失败",
    "physical_sql_invalid": "Wren 生成的物理 SQL 未通过检查",
    "llm_sql_invalid": "模型返回的 SQL 格式无效",
    "llm_declared_scope_mismatch": "模型声明的使用范围与 SQL 不一致",
    "llm_fallback_failed": "受控兜底 SQL 生成失败",
    "llm_rate_limited": "模型请求受限，请稍后重试",
    "llm_quota_exhausted": "模型额度不足，无法生成查询",
    "llm_transport_error": "模型服务连接失败",
    "llm_provider_unavailable": "模型供应商当前不可用",
}

FAILURE_CODES: Mapping[str, str] = MappingProxyType(_FAILURE_MESSAGES)
_UNKNOWN_FAILURE_MESSAGE = "查询失败，原因未知，请查看结构化证据"


def failure_message(failure: CompilerFailure) -> str:
    return FAILURE_CODES.get(failure.code, _UNKNOWN_FAILURE_MESSAGE)


class MdlSqlError(RuntimeError):
    def __init__(
        self,
        code: str,
        evidence: Mapping[str, Any] | None = None,
        *,
        message: str = "",
    ):
        self.code = code
        self.evidence = dict(evidence or {})
        resolved = message or failure_message(
            CompilerFailure(code=code, stage="", evidence=self.evidence)
        )
        super().__init__(resolved)


class MdlSqlConfigurationError(MdlSqlError):
    pass


class MdlSqlRelationshipError(MdlSqlError):
    pass


class MdlSqlContractError(MdlSqlError):
    pass


class MdlSqlContextError(MdlSqlError):
    pass


class MdlSqlFallbackError(MdlSqlError):
    pass


class MdlSqlPipelineError(MdlSqlError):
    def __init__(self, failure: CompilerFailure):
        self.failure = failure
        super().__init__(
            failure.code,
            failure.evidence,
            message=failure_message(failure),
        )
