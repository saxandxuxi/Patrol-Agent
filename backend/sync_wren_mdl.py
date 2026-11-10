"""同步 Wren MDL：按实时库结构生成 wren_projects/<项目>/models/，然后重新编译。

结构信息（表名/列名/类型/注释）全部来自 information_schema，人工只维护语义：
    wren_projects/<项目>/overrides.yml   模型名、业务描述、计算字段、ref_sql、primary_key
    wren_projects/<项目>/relationships.yml  JOIN 路径
    wren_projects/<项目>/cubes/            业务指标

用法（不需要 PYTHONUTF8，脚本自己给子进程设好）：
    cd backend
    .\\.venv\\Scripts\\python.exe sync_wren_mdl.py                 # 两个库都同步
    .\\.venv\\Scripts\\python.exe sync_wren_mdl.py --kind mysql
    .\\.venv\\Scripts\\python.exe sync_wren_mdl.py --no-build      # 只生成不编译
    .\\.venv\\Scripts\\python.exe sync_wren_mdl.py --build-only    # 改了关系/指标后只重编译
    .\\.venv\\Scripts\\python.exe sync_wren_mdl.py --generator official   # 用官方流程版生成器

两套生成器（A/B 对比用，跟 .env 的 MDL_GENERATOR 一个意思）：
    legacy    原 mdl_sync.py：手写类型归族，类型抹平长度/精度
    official  官方流程版：wren utils parse-types 归一类型 + validate 再 build + 外键关系草稿
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")

from app.services import mdl_sync, mdl_sync_official


def main() -> int:
    parser = argparse.ArgumentParser(description="按库结构生成/更新 Wren MDL")
    parser.add_argument("--kind", default="", help="mysql / postgresql，默认两个都做")
    parser.add_argument("--no-build", action="store_true", help="只生成 YAML，不跑 wren context build")
    parser.add_argument("--build-only", action="store_true", help="不改 models/，只重新编译")
    parser.add_argument(
        "--generator",
        default="",
        choices=["", "legacy", "official"],
        help="用哪套生成器，默认跟随 .env 的 MDL_GENERATOR",
    )
    args = parser.parse_args()

    from app.config import settings

    chosen = args.generator or str(getattr(settings, "mdl_generator", "legacy") or "legacy")
    module = mdl_sync_official if chosen == "official" else mdl_sync
    print(f"[generator] {chosen}")
    kinds = [args.kind] if args.kind else list(mdl_sync.PROJECTS.values())
    failed = False
    for kind in kinds:
        if args.build_only:
            result = module.build_project(kind)
        else:
            result = module.sync_project(kind, build=not args.no_build)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("built") is False and not args.no_build:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
