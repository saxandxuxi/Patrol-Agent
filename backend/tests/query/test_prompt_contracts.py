import ast
import re
import unittest
from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompts"
BACKEND_DIR = PROMPT_DIR.parent / "backend"
HEADER = re.compile(r"^\s*<!--(.*?)-->\s*", re.S)
PLACEHOLDER = re.compile(r"<<[a-z_][a-z0-9_]*>>")
DECLARATION = re.compile(r"占位符：\s*(.*)", re.S)
HARD_RULE = re.compile(r"^\s*(\d+)\.", re.M)


def prompt_text(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def body_and_header(name: str) -> tuple[str, str]:
    text = prompt_text(name)
    matched = HEADER.match(text)
    if not matched:
        raise AssertionError(f"{name} 缺少文件头说明")
    return matched.group(1), text[matched.end() :]


class PromptContractTests(unittest.TestCase):
    def test_each_prompt_declares_every_runtime_placeholder(self):
        for path in sorted(PROMPT_DIR.glob("*.md")):
            if path.name == "README.md":
                continue
            with self.subTest(prompt=path.name):
                header, body = body_and_header(path.name)
                declared = DECLARATION.search(header)
                self.assertIsNotNone(
                    declared,
                    f"{path.name} 的文件头没有声明占位符",
                )
                self.assertEqual(
                    set(PLACEHOLDER.findall(declared.group(1))),
                    set(PLACEHOLDER.findall(body)),
                    f"{path.name} 的占位符声明与实际正文不一致",
                )

    def test_query_spec_hard_rules_are_unique_and_consecutive(self):
        _, body = body_and_header("query_spec.md")
        hard_rules = body.split("硬规则：", 1)[1].split("允许的 view：", 1)[0]
        numbers = [int(value) for value in HARD_RULE.findall(hard_rules)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_prompt_call_arguments_match_declared_placeholders(self):
        calls: dict[str, set[str]] = {}
        roots = (BACKEND_DIR / "app", BACKEND_DIR / "evals")
        for root in roots:
            for path in root.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    function = node.func
                    if not (
                        isinstance(function, ast.Attribute)
                        and function.attr == "messages"
                        and isinstance(function.value, ast.Name)
                        and function.value.id == "prompts"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        continue
                    name = node.args[0].value
                    calls.setdefault(name, set()).update(
                        keyword.arg
                        for keyword in node.keywords
                        if keyword.arg is not None
                    )

        self.assertTrue(calls)
        prompt_names = {path.stem for path in PROMPT_DIR.glob("*.md")}
        for name, arguments in sorted(calls.items()):
            # 旧链路仍有不可达的 prompt 名称；这里只保护当前目录中仍存在的提示词。
            if name not in prompt_names:
                continue
            with self.subTest(prompt=name):
                _, body = body_and_header(f"{name}.md")
                self.assertEqual(
                    arguments,
                    {
                        placeholder[2:-2]
                        for placeholder in PLACEHOLDER.findall(body)
                    },
                    f"{name} 的代码传参与提示词占位符不一致",
                )

    def test_query_spec_comparison_rule_is_complete(self):
        _, body = body_and_header("query_spec.md")
        self.assertIn(
            "不要为了比较大小而写 `calculation`",
            body,
        )
        self.assertNotRegex(
            body,
            r"不要为了比较大小而写\s*\n\s*\d+\.",
        )

    def test_query_spec_group_share_uses_columns_calculation(self):
        _, body = body_and_header("query_spec.md")
        self.assertIn("group_share", body)
        self.assertIn(
            "`calculate_columns/percentage_share`",
            body,
        )
        self.assertIn(
            "禁止只用 `calculate_scalar` 和第一行",
            body,
        )

    def test_query_prompts_use_runtime_supported_views(self):
        for name in ("query_spec.md", "query_spec_audit.md"):
            with self.subTest(prompt=name):
                _, body = body_and_header(name)
                self.assertIn("<<supported_views>>", body)
                self.assertNotIn(
                    "count / sum / avg / distribution / list / detail",
                    body,
                )

    def test_group_change_prompt_uses_two_same_shape_nodes(self):
        _, query_spec = body_and_header("query_spec.md")
        _, audit = body_and_header("query_spec_audit.md")

        for body in (query_spec, audit):
            self.assertIn("calculate_group_change", body)
            self.assertIn("相同 subject", body)
            self.assertIn("唯一分组维度", body)
            self.assertIn("指标", body)
        self.assertIn("哪种病害增加最多", query_spec)
        self.assertIn(
            "不得给 post_calc 添加 `dataset_comparison`",
            query_spec,
        )

    def test_subject_router_distinguishes_task_identity_from_disease_evidence(self):
        _, router = body_and_header("subject_router.md")
        self.assertIn("task_disease_evidence", router)
        self.assertIn("病害属性", router)
        self.assertIn("task_number", router)

        _, query_spec = body_and_header("query_spec.md")
        self.assertIn("task_disease_evidence", query_spec)
        self.assertIn("病害属性", query_spec)

    def test_query_spec_audit_stays_a_compact_checklist(self):
        header, body = body_and_header("query_spec_audit.md")
        self.assertLess(len(body), 6000)
        self.assertIn("不重复抄写", header)

    def test_prompt_readme_lists_all_current_prompts(self):
        readme = prompt_text("README.md")
        prompt_names = {
            path.name
            for path in PROMPT_DIR.glob("*.md")
            if path.name != "README.md"
        }
        missing = sorted(name for name in prompt_names if name not in readme)
        self.assertEqual(missing, [], f"README 缺少提示词：{missing}")

    def test_mdl_sql_generator_prompt_is_contract_bounded(self):
        _, body = body_and_header("mdl_sql_generator.md")

        self.assertIn("QuerySpec 和合同是只读权威", body)
        self.assertIn(
            "只能使用给定的 MDL 模型、字段、指标、枚举和关系 ID",
            body,
        )
        self.assertIn("只允许一条只读 SELECT 或 WITH ... SELECT", body)
        self.assertIn("不输出 Markdown、思考过程或解释", body)
        self.assertIn('"mdl_sql"', body)
        self.assertIn('"used_models"', body)
        self.assertIn('"used_relationship_ids"', body)

    def test_mdl_sql_generator_keeps_assist_evidence_untrusted(self):
        _, body = body_and_header("mdl_sql_generator.md")

        self.assertIn("assist_evidence", body)
        self.assertIn("trusted=false", body)
        self.assertIn("并行", body)
        self.assertIn("不能直接决定 SQL", body)

    def test_planning_prompt_avoids_copyable_specific_week_example(self):
        _, body = body_and_header("planning.md")
        self.assertNotIn("2026年第31周", body)
        self.assertNotIn("两条实测规则", body)


if __name__ == "__main__":
    unittest.main()
