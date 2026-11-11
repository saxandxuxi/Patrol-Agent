import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.query.mdl_sql.compiler.llm_fallback import (
    generate_mdl_sql_once,
)
from app.query.mdl_sql.errors import MdlSqlFallbackError
from app.query.mdl_sql.models import (
    CapabilityContext,
    CompilerFailure,
    MdlContext,
    MdlQueryContract,
    PromptContextBundle,
)
from app.services import llm


def prompt_context() -> PromptContextBundle:
    contract = MdlQueryContract(
        contract_id="plan-q1:node-1:contract-hash",
        plan_id="plan-q1",
        node_id="node-1",
        query_spec_hash="spec-hash",
        capability_snapshot_hash="capability-hash",
        mdl_fingerprint="mdl-hash",
        contract_hash="contract-hash",
        subject="disease",
        view="count",
        root_model="disease",
        field_bindings={},
        filters=(),
        dimensions=(),
        metrics=(),
        outputs=(),
        group_by=(),
        order_by=(),
        limit=None,
        required_assertions=("preserve_user_scope",),
        allowed_models=("disease",),
        required_relationship_ids=(),
        intermediate_models=(),
        mandatory_predicates=(),
        output_aliases={},
    )
    return PromptContextBundle(
        schema_version=1,
        contract=contract,
        business_capability=CapabilityContext(
            subject="disease",
            fields={},
            metrics={},
            derived_metrics={},
            enum_values={},
            entity_families={},
            assertions={},
        ),
        mdl_subset=MdlContext(
            root_model="disease",
            models={
                "disease": {
                    "description": "病害",
                    "columns": {
                        "id": {
                            "name": "id",
                            "data_type": "bigint",
                            "description": "主键",
                        }
                    },
                }
            },
            relationships=(),
        ),
    )


def compiler_failure() -> CompilerFailure:
    return CompilerFailure(
        code="compiler_not_covered",
        stage="deterministic",
        evidence={
            "reason": "uncovered view",
            "allowed_models": ("disease",),
        },
        contract_id="plan-q1:node-1:contract-hash",
        contract_hash="contract-hash",
    )


def valid_reply() -> str:
    return json.dumps(
        {
            "mdl_sql": "SELECT COUNT(*) AS count FROM disease",
            "used_models": ["disease"],
            "used_relationship_ids": [],
        },
        ensure_ascii=False,
    )


class MdlLlmFallbackTests(unittest.TestCase):
    def test_one_valid_reply_becomes_an_unaudited_candidate(self):
        model = {"model": "sql-model"}
        with (
            patch.object(llm, "chat", return_value=valid_reply()) as chat,
            patch.object(llm, "chat_json") as chat_json,
        ):
            candidate = generate_mdl_sql_once(
                prompt_context(),
                compiler_failure(),
                model=model,
            )

        self.assertEqual(candidate.source, "llm_fallback")
        self.assertEqual(
            candidate.mdl_sql,
            "SELECT COUNT(*) AS count FROM disease",
        )
        self.assertEqual(candidate.contract_id, "plan-q1:node-1:contract-hash")
        self.assertEqual(candidate.contract_hash, "contract-hash")
        self.assertEqual(candidate.used_models, ("disease",))
        self.assertEqual(candidate.used_relationship_ids, ())
        self.assertIsNone(candidate.failure)
        chat.assert_called_once()
        chat_json.assert_not_called()
        self.assertEqual(chat.call_args.kwargs["model"], model)
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 4096)
        messages = chat.call_args.args[0]
        prompt = "\n".join(message["content"] for message in messages)
        self.assertIn("plan-q1:node-1:contract-hash", prompt)
        self.assertIn("compiler_not_covered", prompt)

    def test_prompt_carries_parallel_mdl_business_and_assist_evidence(self):
        context = replace(
            prompt_context(),
            assist_evidence=(
                {
                    "rule_id": "lane_ordinal",
                    "source": "code_rule",
                    "candidate_field": "lane_index",
                    "candidate_value": "3",
                    "trusted": False,
                },
            ),
        )
        with patch.object(llm, "chat", return_value=valid_reply()) as chat:
            generate_mdl_sql_once(context, compiler_failure())

        messages = chat.call_args.args[0]
        prompt = "\n".join(message["content"] for message in messages)
        self.assertIn('"business_capability"', prompt)
        self.assertIn('"mdl_subset"', prompt)
        self.assertIn('"assist_evidence"', prompt)
        self.assertIn('"lane_ordinal"', prompt)
        self.assertIn('"trusted":false', prompt)

    def test_default_sql_role_is_used_when_model_is_not_passed(self):
        role = {"model": "role-sql-model"}
        with (
            patch(
                "app.query.mdl_sql.compiler.llm_fallback.role_model",
                return_value=role,
            ) as role_model,
            patch.object(llm, "chat", return_value=valid_reply()) as chat,
        ):
            generate_mdl_sql_once(
                prompt_context(),
                compiler_failure(),
            )

        role_model.assert_called_once_with("sql")
        self.assertEqual(chat.call_args.kwargs["model"], role)

    def test_invalid_replies_fail_closed_after_exactly_one_chat_call(self):
        valid = json.loads(valid_reply())
        replies = {
            "markdown": f"```json\n{valid_reply()}\n```",
            "prose": f"结果如下：{valid_reply()}",
            "empty_sql": json.dumps({**valid, "mdl_sql": "  "}),
            "json_array": json.dumps([valid], ensure_ascii=False),
            "non_string_model": json.dumps(
                {
                    **valid,
                    "used_models": [{"name": "disease"}],
                },
                ensure_ascii=False,
            ),
            "blank_relationship_id": json.dumps(
                {
                    **valid,
                    "used_relationship_ids": [""],
                },
                ensure_ascii=False,
            ),
            "two_statements": json.dumps(
                {
                    **valid,
                    "mdl_sql": (
                        "SELECT COUNT(*) AS count FROM disease; "
                        "SELECT COUNT(*) AS count FROM disease"
                    ),
                },
                ensure_ascii=False,
            ),
            "forbidden_statement": json.dumps(
                {
                    **valid,
                    "mdl_sql": "DELETE FROM disease",
                },
                ensure_ascii=False,
            ),
            "extra_key": json.dumps(
                {**valid, "explanation": "not allowed"},
                ensure_ascii=False,
            ),
            "missing_key": json.dumps(
                {
                    "mdl_sql": valid["mdl_sql"],
                    "used_models": valid["used_models"],
                },
                ensure_ascii=False,
            ),
            "invalid_json": "{not-json",
        }

        for name, reply in replies.items():
            with self.subTest(reply=name):
                with (
                    patch.object(llm, "chat", return_value=reply) as chat,
                    patch.object(llm, "chat_json") as chat_json,
                ):
                    with self.assertRaises(MdlSqlFallbackError) as caught:
                        generate_mdl_sql_once(
                            prompt_context(),
                            compiler_failure(),
                        )

                self.assertEqual(caught.exception.code, "llm_sql_invalid")
                chat.assert_called_once()
                chat_json.assert_not_called()

    def test_provider_failures_keep_infrastructure_codes(self):
        cases = (
            ("rate_limited", 429, "llm_rate_limited"),
            ("quota_exhausted", 429, "llm_quota_exhausted"),
            ("transport", None, "llm_transport_error"),
            (
                "provider_unavailable",
                503,
                "llm_provider_unavailable",
            ),
        )

        for category, status, expected in cases:
            with self.subTest(category=category):
                error = llm.LLMError(
                    category,
                    category=category,
                    status_code=status,
                )
                with patch.object(llm, "chat", side_effect=error) as chat:
                    with self.assertRaises(MdlSqlFallbackError) as caught:
                        generate_mdl_sql_once(
                            prompt_context(),
                            compiler_failure(),
                        )

                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(
                    caught.exception.evidence["provider_category"],
                    category,
                )
                chat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
