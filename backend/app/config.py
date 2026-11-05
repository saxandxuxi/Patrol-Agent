from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "127.0.0.1"
    app_port: int = 18730
    # CORS：默认放开来源但不带凭证（credentials + "*" 会被浏览器直接拦掉）
    # 逗号分隔写法（也兼容 JSON 数组）；用 cors_origins 属性取 tuple
    cors_allow_origins: str = "*"
    cors_allow_credentials: bool = False
    app_secret_key: str = ""
    frontend_dist: Path = Path("frontend/dist")

    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    # ── 多供应商（都是 OpenAI 兼容端点）：LLM_PROVIDERS 里列谁就认谁 ──────────────
    # 角色模型可以写成「供应商/模型」（如 deepseek/deepseek-chat）；只写模型名时沿用默认供应商
    # （QWEN_*），旧配置不用改。
    llm_providers: str = "dashscope"
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_models: str = "glm-4.6,glm-4.5,glm-4.5-air"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_models: str = "deepseek-chat,deepseek-reasoner"
    moonshot_api_key: str = ""
    moonshot_base_url: str = "https://api.moonshot.cn/v1"
    moonshot_models: str = "moonshot-v1-8k,moonshot-v1-32k"

    def provider_configs(self) -> dict[str, dict[str, Any]]:
        """供应商 → {api_key, base_url, models[]}（只回 LLM_PROVIDERS 里点名的那些）。"""
        wanted = [item.strip().lower() for item in str(self.llm_providers or "").split(",") if item.strip()]
        out: dict[str, dict[str, Any]] = {}
        for name in wanted:
            if name == "dashscope":
                out[name] = {
                    "api_key": self.qwen_api_key,
                    "base_url": self.qwen_base_url,
                    "models": [self.qwen_model],
                }
                continue
            key = str(getattr(self, f"{name}_api_key", "") or "")
            if not key:
                continue
            out[name] = {
                "api_key": key,
                "base_url": str(getattr(self, f"{name}_base_url", "") or ""),
                "models": [
                    item.strip()
                    for item in str(getattr(self, f"{name}_models", "") or "").split(",")
                    if item.strip()
                ],
            }
        return out
    llm_timeout_seconds: float = 120.0
    llm_max_tokens: int = 1024
    # 知识问答：开启思考模式与联网搜索（受模型支持情况影响）
    knowledge_enable_thinking: bool = True
    knowledge_enable_search: bool = True
    knowledge_max_tokens: int = 2048
    # 同一会话里，超过这个分钟数再提问就视为新话题，不再继承上一轮的路线/桩号等上下文
    session_context_ttl_minutes: int = 30
    # 查询结果的本地保留策略：**条数**和**天数**两条线，谁先到就清谁。
    # 以前只按条数留 200 条，正常问一小时就能把早上的结果挤掉——历史里点"查看这次结果"
    # 就报"结果已不存在"。现在放宽到 2000 条 / 30 天（一条结果约 70KB，满配约 140MB）。
    result_cache_limit: int = 2000
    result_cache_ttl_days: int = 30
    # ── 本地库/日志文件的保留期（定时清理任务用）─────────────────────────────
    # 本地 agent.db 之前涨到 200MB+，大头是 agent_logs 和 query_history：
    # 按天 + 按最大行数两条线清旧记录，必要时再 VACUUM 回收文件空间。
    log_retention_days: int = 7
    log_max_rows: int = 20000
    history_retention_days: int = 30
    retrieval_retention_days: int = 30
    db_purge_hours: float = 6.0
    db_vacuum_enabled: bool = True
    # 运行日志文件（logs/app/app.log）超过这个大小就轮转，只留一个备份
    log_file_max_mb: int = 20

    ssh_host: str = "10.168.210.78"
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str = ""

    mysql_host: str = "10.168.210.78"
    mysql_port: int = 3307
    mysql_database: str = "new_four_road_base_ai"
    # 容器名/基地址都走配置：以前 db.py 里各自写死一份，改 config 不生效。
    mysql_container: str = "mysql"
    mysql_user: str = "Yiviewmysql"
    mysql_password: str = ""
    mysql_charset: str = "utf8mb4"

    pg_host: str = "10.168.210.78"
    pg_port: int = 5436
    pg_database: str = "gpsdb"
    pg_container: str = "postgres"
    pg_user: str = "postgres"
    pg_password: str = ""

    # 默认走本机隧道端口（28888 转发到对象存储）；要直连就改 .env。
    seaweedfs_base_url: str = "http://127.0.0.1:28888"
    seaweedfs_bucket: str = ""

    ragflow_base_url: str = "http://127.0.0.1"
    ragflow_api_key: str = ""
    ragflow_embedding_model: str = "text-embedding-v3@Tongyi-Qianwen"
    ragflow_llm_model: str = "qwen-plus"
    ragflow_top_k: int = 10
    ragflow_similarity_threshold: float = 0.2
    # 向量权重：实测 RAGFlow 的 similarity 是"向量 × w + 关键词 × (1-w)"，
    # 中文短句的关键词那一路分数极低（0.2 上下），w=0.5 时正确项只有 0.55 左右、
    # 和错项挤在一起；w=0.9 时正确项 0.84、第二名 0.70（分差拉得开），纯向量 0.90。
    # 见 docs（示例召回体检）与本轮实测：同义问法能不能召回，主要卡在这个权重上。
    ragflow_vector_similarity_weight: float = 0.85
    ragflow_chunk_token_num: int = 1024
    # 父块-子块会把 Markdown 的标题/代码行切成碎片块（实测中位长度仅 10~45 字），
    # 因此默认关闭。注意：RAGFlow 里该开关一旦开启，API 无法再关闭，只能删库重建。
    ragflow_parent_child: bool = False
    ragflow_children_delimiter: str = "\n"
    # 解析页范围（1 基、闭区间），用于跳过封面/公告/前言/目次等前置页
    ragflow_page_ranges: dict[str, list[list[int]]] = {
        "养护行业规范": [[6, 100000]],
        "工程量计算文件": [[6, 100000]],
    }
    ragflow_graphrag_datasets: tuple[str, ...] = (
        "养护行业规范",
        "工程量计算文件",
        "数据库字段字典",
    )

    data_dir: Path = Path("data")
    # 日志根目录（按类型分文件夹）：logs/app 运行日志、logs/eval 评测日志、logs/test 测试输出。
    # 以前 agent.log 和一堆 eval_*.log 全堆在 data/ 里，和数据库/缓存混在一起。
    log_dir: Path = Path("logs")
    knowledge_root: Path = Path("知识库文件")
    sql_max_rows: int = 500
    # 「整体情况」这类汇总问题的取数上限：要算总面积/平均/最大并列，行数不能截断。
    sql_overview_max_rows: int = 5000
    # 导出上限：界面只显示前若干行，但下载 CSV 必须拿全量，所以导出时把 SQL 的
    # 显示用 LIMIT 换成这个上限重跑一次。
    sql_export_max_rows: int = 50000
    confidence_threshold: float = 0.40
    retry_limit: int = 2
    agent_max_attempts: int = 3   # 自我纠错尝试次数（含首次，见项目智能化升级方案 升级4）
    # 走哪条路：wren=**默认跳过确定性模板**，全部交给语义层（模板老是抢先答错，用户要求关掉）；
    # hybrid=模板优先（单主体简单查询秒回），保留给对比/回退用。
    nl2sql_engine: str = "wren"
    # 三级降级：确定性模板 → Wren（可用时）→ 内置 LLM。
    # 见 docs/RAGFlow知识库精简方案.md §3.1（已移除 Vanna）。
    # 生成 SQL 专用模型（留空则用默认模型）。实测 SQL 生成用 coder 族比通用族更稳，
    # 所以这里和语义/校验分开配（qwen3-coder-plus 已从端点下架，2026-10 换成同族 qwen3-coder-flash）。
    nl2sql_model: str = ""
    # 结果校验 / 反思改 SQL / 问题优化用的"裁判模型"（留空则跟随语义理解模型，
    # 再回落到 SQL 模型）。实测小模型（qwen3.7-flash）会把明显答非所问的回复判通过，
    # 所以默认用强通用模型（qwen3.7-plus）——它要看懂"用户问了几个点、回复答了几个点"。
    # 同一个模型既写 SQL 又判自己容易自说自话，分开放能减少假阳/假阴。
    verify_model: str = ""
    # 「语义理解」环节用的模型（意图分类 / 拆分 / 上下文补全 / 结果解读 / 后置计算说明 /
    # 数据源判断 / 反思改 SQL）。这些环节决定后面整条链路的走向，值得用强模型；
    # 都是短输入短输出，单次成本远低于写 SQL。留空则用默认模型。
    semantic_model: str = ""
    # L2 示例直达（docs/RAG示例直达与AI直答旁路方案.md §6）：
    # 同问判定用便宜模型（role_model("semantic")），阈值/分差/召回数都在设置页可改。
    # 同义改写只要"打分模型认为口径一致"就摆卡，门槛别卡死（用户确认才是最后一道闸）
    # （docs/示例参数对齐与同义召回放宽方案.md 第二节）
    rag_direct_threshold: float = 0.75
    rag_direct_margin: float = 0.08
    rag_direct_topk: int = 8

    # ══════════════════════════════════════════════════════════════════════
    # 调参区：下面这些原来散落在代码里写死，现在都能在 .env 里改
    # （只列"会影响行为的数"，纯业务常量如编码映射、词表不在此列）
    # ══════════════════════════════════════════════════════════════════════

    # ── 推荐卡（候选/排序）──────────────────────────────────────────────
    # 一张确认卡最多摆几条候选
    capability_limit: int = 6
    # 同类候选上限：通用算子/内置代码模板很容易把卡占满，压住它们
    capability_per_kind_cap: int = 2
    # 多条候选分差 ≤ 这个值（且在同一层内）时，让一次小模型只做排序
    capability_rerank_max_gap: float = 0.6
    # 确认卡 token 有效期（分钟）：卡片摆出来后多久内点确认还有效
    capability_token_ttl_minutes: int = 30
    # 自动沉淀模板候选的排序权重：基础分 + 槽位完备度×加成 − 缺槽数×扣分
    auto_template_score_base: float = 4.0
    auto_template_score_completeness: float = 0.6
    auto_template_score_missing_penalty: float = 0.15

    # ── 示例直达 / 同义召回（L2）────────────────────────────────────────
    # 同问**打分模型**：默认关（实测一次 ~20s；候选最后由用户在卡上拍板）
    rag_direct_score_enabled: bool = False
    # 高置信直摆开关：相似度 ≥ fast_threshold + 结构同型 + 与次名分差 ≥ fast_margin
    # → 跳过打分模型直接摆卡（省 3~10 秒）
    rag_direct_fast_enabled: bool = True
    rag_direct_fast_threshold: float = 0.78
    rag_direct_fast_margin: float = 0.05
    # 同义召回的召回门槛：骨架/向量相似度低于这个值不进候选
    example_recall_min_score: float = 0.4
    # 示例检索时一次向量召回几条（越大越慢）
    example_recall_topk: int = 20
    # 检索到的片段相似度下限（示例库 / 问题索引低于它就丢弃）
    retrieval_min_similarity: float = 0.3

    # ── 缓存与 TTL（秒）─────────────────────────────────────────────────
    # 进程内缓存条数上限（计划/语义/校验/意图/重排等共用这个量级）
    cache_max_entries: int = 200
    # 查询计划缓存时间：同一句话在对话里被问两三次，靠它省重复规划
    plan_cache_ttl_seconds: float = 300.0
    # 路线台账 / 病害类型字典的缓存时间（库在 SSH 隧道后面，别每题都查）
    reference_data_ttl_seconds: float = 300.0
    # 字段目录 / 取值样例的缓存时间
    field_catalog_ttl_seconds: float = 600.0
    # 字段目录**读库失败**后的重试间隔：失败时先用本地快照顶着、并按这个间隔重试实时读。
    # 以前失败也把空目录缓存满 TTL，字段白名单会整段变空（lat/lng 都被判成"库里没有"）。
    field_catalog_retry_seconds: float = 15.0
    # 看图题一次最多展示/探测多少张（超过的如实说明"只展示前 N 张"）。
    # 探测已改成一条远程命令批量做（84 张约 3 秒），所以别设太小——以前写死 6 张，
    # "看一下病害样图"表里 84 条却只出 5 张。
    image_display_limit: int = 120
    # 图片本地缓存保留天数（图在 SSH 隧道后面的 SeaweedFS 上，缓存住就不用每次穿隧道）。
    # 0 = 不缓存。
    image_cache_ttl_days: float = 7.0
    # 图片缓存目录大小上限（MB）：超了从最旧的开始删。样图单张就有 1MB 量级，
    # 不设上限看到几百张就会堆几百 MB。0 = 不限。
    image_cache_max_mb: float = 500.0
    # "把能调出来的图片全部调出来"这类**全库图片清单**题，一次最多收起/探测多少个图片路径。
    # 比 IMAGE_DISPLAY_LIMIT 大：清单题要先把目录里所有图片列的去重路径**收齐**再判存在
    # （实测全库 323 个），截断会让靠后的列（路段封面这种按字母排后排的表）整批看不到。
    image_catalog_limit: int = 600
    # 宏观元数据查询（库/表/字段结构，docs/宏观元数据查询_语义理解方案.md）一次在回复里
    # 最多列多少行（表清单/字段清单）；超过的如实说"还有 N 张未列出"。
    metadata_list_limit: int = 200
    # 回答末尾附一段「中间过程」（复合题每一步的问法/引擎/行数/时间口径 + 链路回填的键值）：
    # 排查"为什么答错/哪一步断了"不用再另写脚本读日志。默认开，觉得吵就在 .env 里关掉。
    show_process_steps: bool = True
    # "哪张表记录最多"这类排名题：对前 N 张表跑精确 COUNT(*)。
    # information_schema.TABLES.TABLE_ROWS 是 InnoDB 估算值（实测偏差可能过半），排序会错，
    # 所以排名题默认把前 10 张核实一遍；设 0 = 只用估算值、不跑精确统计。
    metadata_refine_top_n: int = 10
    # 模板生命周期（docs/模板沉淀标准与复用校验_系统性方案.md §5.3）：被摆到推荐里
    # N 次却一次没被复用 → 标记 cold（不进推荐但保留，防误删）。调小=更激进地清推荐噪音。
    template_cold_after_offers: int = 10
    # cold 判定的最小年龄（天）：刚挖出来的模板不可能"被摆了很多次还没人用"，
    # 没有这道闸，一次回归跑批（几十次候选收集）就能把新模板标成冷门。
    template_cold_min_age_days: float = 7.0
    # 模板 LLM 审核发现 high 级问题时**是否自动降级**（mature → candidate，退出推荐）。
    # 默认 false：实测审核误报率高——13 条被判"该降级"的里抽查多条明确误报（只含一个
    # `{route_id}` 的正常模板被说成"参数错位"；三处指标都答了的 SQL 被说成"只答一部分"），
    # 降级的代价是丢掉好模板，而审核在弹卡侧还有确定性闸兜着。
    # 关掉后审核结论照旧记录、维护页照旧展示，降不降由人决定。
    template_review_auto_demote: bool = False
    # 挖槽的**语义补漏**：确定性的 AST 挖槽看不懂"这个数字对应问法里的哪个值"
    # （实测 `ORDER BY ABS(p.pile_distance - 1802)` 里的桩号被写死，换 k1+000 复用会静默答错）。
    # 开着时，只在"挖完槽还有用户数字留在 SQL 里"才问一次模型（该不该挖、属于哪一列），
    # 替换与校验仍由代码做；正常模板零 LLM 成本。关掉就退回纯 AST + 拒绝沉淀那一套。
    template_mine_use_llm: bool = True
    # 语义层（MDL）生成器：legacy = 原有 mdl_sync.py（手写类型字典 + information_schema），
    # official = 官方流程版 mdl_sync_official.py（wren utils parse-types + validate 再 build）。
    # 两套并存，方便 A/B 对比；重建语义层的接口可以按次覆盖这个默认值。
    mdl_generator: str = "legacy"
    # 编码字典（code→中文）缓存时间
    dictionary_ttl_seconds: float = 600.0
    # 示例召回的骨架词表缓存时间
    example_vocab_ttl_seconds: float = 600.0
    # PostgreSQL 两张点位表"有没有数据"的探测缓存（跨库路由要用它决定 PG 要不要让路）
    pg_probe_ttl_seconds: float = 300.0

    # ── 结果与校验 ──────────────────────────────────────────────────────
    # 通用明细一次最多取多少行
    generic_detail_limit: int = 500
    # 图表推断：一列里数值占比达到这个比例才当数值列
    analysis_numeric_ratio: float = 0.8
    # 数字比对容差（"两边加起来是否等于总数""展示数是否等于总数"这类判断）
    number_compare_epsilon: float = 0.5
    # 回答发出前的"反思自检"（升级3）：检查数字自洽/结论有没有数据支撑/有没有瞎编的比率。
    # 只对分析型、复合型回答跑（简单查询多花一次调用不划算）。
    # 复杂问题执行前先「大白话复述确认」（方案第二章）。默认关：开了会在执行前多一轮交互，
    # 不传 confirmed 的调用会拿到确认文案而不是答案（所以评测脚本按关处理）。
    confirm_before_query: bool = False
    # 悬浮球嵌入用的访问口令（留空=不校验）。宿主平台把它的 token 透传过来即可，
    # 校验方式对齐亿维接口协议的风格：请求头 token: <value>（也支持 ?token=）。
    widget_token: str = ""
    reflect_check_enabled: bool = True
    # 回答末尾的"后续建议"（升级4）：主动提示用户接下来可能想知道什么。
    followup_suggestions_enabled: bool = True
    # 前端「模型设置」里可选的模型名（下拉候选，也允许自己填）。**只列当前端点上实测在架的**
    # ——qwen3-coder-plus / qwen3-max / qwen3-turbo 已下架（实测 404），列出来只会让用户选到坏模型。
    model_choices: tuple[str, ...] = (
        "qwen3-coder-flash",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.7-flash",
        "qwen-plus",
        "qwen-flash",
        "deepseek-v4.1-flash",
    )

    mysql_allowed_tables: tuple[str, ...] = (
        "ai_problem",
        "ai_problem_base",
        "ai_asset_main",
        "ai_asset_base",
        "ai_distance_capture",
        "basic_facilities_routes",
        "basic_facilities_sections",
        "basic_facilities_sections_pile_detail",
        "basic_facilities_bridges",
        "basic_facilities_tunnels",
        "basic_facilities_slope",
        "project_manage_project_main",
        "maintenance_project",
        "maintenance_road_work_record",
        "ai_work_order",
        "maintenance_work_order",
        "ai_maintain_patrol",
        "ai_maintain_routes_patrol",
    )

    pg_allowed_tables: tuple[str, ...] = (
        "gps_points",
        "inclination_points",
    )

    knowledge_datasets: dict[str, str] = {
        "养护行业规范": "standards",
        "工程量计算文件": "standards",
        "数据库字段字典": "database_dictionary",
        "SQL问答示例": "sql_examples",
    }

    def datasets_of_kind(self, *kinds: str) -> list[str]:
        """按用途取数据集名（数据集清单只此一份，代码里不再各写一遍）。"""
        return [name for name, kind in self.knowledge_datasets.items() if kind in kinds]

    # 知识问答默认查哪个库；问到计量/定额/费用时再叠加哪些库（名字只在这里出现一次）
    knowledge_default_dataset: str = "养护行业规范"
    knowledge_measure_datasets: tuple[str, ...] = ("工程量计算文件",)

    # Wren AI 语义层（见 docs/WrenAI集成方案.md）
    wren_enabled: bool = False          # 打开后才尝试用 Wren 编译 MDL-SQL
    wren_projects_dir: Path = Path("wren_projects")
    wren_use_mdl_context: bool = False  # 把 MDL 的结构化模型/字段注进提示词

    @property
    def cors_origins(self) -> list[str]:
        """CORS 允许来源：字符串按逗号切；空/星号都回到 ["*"]。

        注意不能用 tuple 字段 —— pydantic-settings 对环境变量里的 tuple 会强制按 JSON
        解析，写 CORS_ALLOW_ORIGINS=http://a,http://b 会直接 SettingsError 起不来（实测）。
        """
        text = str(self.cors_allow_origins or "").strip()
        if not text or text == "*":
            return ["*"]
        if text.startswith("["):
            import json as _json

            try:
                return [str(item) for item in _json.loads(text)] or ["*"]
            except ValueError:
                return ["*"]
        return [item.strip() for item in text.split(",") if item.strip()] or ["*"]

    @field_validator(
        "frontend_dist", "data_dir", "log_dir", "knowledge_root", "wren_projects_dir", mode="after"
    )
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # 日志按类型分目录；应用日志固定写在 logs/app/app.log
    (settings.log_dir / "app").mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
