# 道路 AI 巡检养护智能体

这是一个面向道路巡检养护业务的语义化 AI 助手 MVP。用户通过自然语言提问，系统自动路由到数据库只读查询、RAGFlow 知识检索、SeaweedFS 图片访问，并以表格、图表或 CSV 返回结果。

## 快速开始

1. 复制 `.env.example` 为 `.env`，填写 SSH、MySQL、PostgreSQL、Qwen、RAGFlow 配置。
2. 安装后端依赖并启动：

```powershell
Set-Location backend
python -m pip install -r ..\requirements.txt
python run.py
```

3. 构建并启动前端开发服务器：

```powershell
Set-Location frontend
npm install
npm run dev
```

生产模式下可执行 `npm run build`，FastAPI 会自动托管 `frontend/dist`。

后端默认监听 `http://127.0.0.1:18730`。

## 端口配置与占用排查

默认端口是 `18730`，刻意避开 `8000`、`8080` 这类经常被占用的端口。端口由 `.env` 的 `APP_PORT` 决定：

```env
APP_HOST=127.0.0.1
APP_PORT=18730
```

**改端口只需要改这一处。** 后端启动时读 `APP_PORT`；[frontend/vite.config.js](frontend/vite.config.js) 也已改成从同一个 `.env` 读取，`npm run dev` 的 `/api`、`/vendor` 代理会自动指向新端口，不用再去改代码。改完重启后端和 `npm run dev` 即可。

> 说明：`config.yaml` 是早期设计遗留文件，代码不读取它，改端口不用管它。真正生效的只有 `.env`。

### 启动时报「只允许使用一次」/ address already in use

说明该端口已被别的进程监听。Windows（PowerShell）按下面三步排查：

```powershell
# 1) 查占用端口的进程，记下 PID
netstat -ano | findstr :18730
# 或
Get-NetTCPConnection -LocalPort 18730 | Select-Object OwningProcess

# 2) 看这个 PID 是什么程序
tasklist /FI "PID eq <PID>"

# 3) 确认可以结束后再结束它
taskkill /PID <PID> /F
# 或
Stop-Process -Id <PID> -Force
```

macOS / Linux 用 `lsof -i :18730` 查、`kill -9 <PID>` 结束。

最常见的占用者其实是**上一次没关掉的 `run.py`**（旧终端窗口还开着）。结束旧进程即可；也可以直接在 `.env` 里把 `APP_PORT` 换成另一个没被占用的端口，然后重启后端和 `npm run dev`。

注意：关掉终端窗口不等于后端会退出。Windows 不会自动结束子进程，而且 `.venv\Scripts\python.exe` 只是个跳板，真正的服务是它拉起的解释器进程，所以你之前关掉的那个终端可能和占用端口的进程毫无关系。现在 `python run.py` 会先查端口：被占用时打印占用者 PID 并直接退出（不再抛 uvicorn 的绑定异常），加 `--kill` 可以先结束旧实例再启动：

```powershell
python run.py --kill
```

## 知识库与 SQL 引擎

RAGFlow 只保留 4 个知识库：

- `养护行业规范`：知识问答的默认检索库；
- `工程量计算文件`：问到工程量/定额/费用时叠加检索；
- `数据库字段字典`：库表结构兜底（代码从 `information_schema` 生成）；
- `SQL问答示例`：人工确认过的「问题 + 查询意图 JSON + SQL」三元组，作 few-shot。

`mysql语句知识` / `postgresql语句知识` / `seaweedfs语句知识` 已从代码中移除（详见 [docs/RAGFlow知识库精简方案.md](docs/RAGFlow知识库精简方案.md)）：语法交给生成 SQL 的模型本身，SeaweedFS 取图走 HTTP API 而不是 SQL。RAGFlow 后台里如果还留着这 3 个数据集，可以手动删除，代码已不再引用。

生成 SQL 按三级降级：**确定性模板 → Wren 语义层 → 内置大模型**。Vanna 已移除。

## Wren 语义层（已启用）

Wren 负责"复杂问题不用模板也能生成正确 SQL"：LLM 只写引用模型名/字段名的 **MDL-SQL**，
JOIN、字典去重、JSON 取值、del_flag 由 Wren 编译器补全，避免模型自由发挥写错表。

```env
WREN_ENABLED=true        # .env 里已打开
WREN_USE_MDL_CONTEXT=false   # 是否把 MDL 模型清单塞进提示词（默认关，实测不需要）
```

**启动**：Wren 需要 Python ≥3.11，本机后端 venv 已重建：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe run.py     # .venv = Python 3.11.15（含 wrenai）
```

**改完 MDL 要重新 build**（否则 `target/mdl.json` 还是旧的）：

```powershell
$env:PYTHONUTF8="1"                   # wren CLI 读 YAML 用系统编码，GBK 下中文 MDL 会报 UnicodeDecodeError
Set-Location wren_projects\road_mysql
..\..\backend\.venv\Scripts\wren.exe context build
Set-Location ..\road_pg
..\..\backend\.venv\Scripts\wren.exe context build
```

建模细则与重建环境步骤见 [backend/requirements-wren.txt](backend/requirements-wren.txt)。

### MDL 是自动生成的（表结构变了怎么办）

`wren_projects/<项目>/` 里的东西分两半：

| 内容 | 谁负责 | 来源 |
|---|---|---|
| `wren_project.yml`、`models/<模型>/metadata.yml` | **代码生成** | `information_schema` / `pg_catalog`：表名、列名、类型、主键、表注释、字段注释 |
| `overrides.yml` | 人工 | 模型名、业务描述、计算字段、`ref_sql` 去重、要隐藏的列 |
| `relationships.yml`、`cubes/` | 人工 | JOIN 路径、业务指标（库里没有这些信息） |

库那边加列 / 改列名 / 改类型之后，**重跑一次生成器**就同步了（models/ 会被覆盖重写）：

```powershell
Set-Location backend
.\.venv\Scripts\python.exe sync_wren_mdl.py          # 生成 models/ + 自动跑 wren context build
.\.venv\Scripts\python.exe sync_wren_mdl.py --build-only   # 只改了关系/指标时用
.\.venv\Scripts\python.exe check_mdl_drift.py        # 体检：结构比对 + 编译验证
```

前端「数据源」页也有一个 **重建语义层** 按钮（走 `POST /api/schema/mdl/sync`），点一下就重新生成 + 编译。

`check_mdl_drift.py` 输出分三档：`[X]` 会导致查询失败（表/列不存在、模型编译不过）、
`[!]` 类型对不上、`[i]` 提示（没进模型的列，例如被 `hide` 掉的）。有 `[X]` 时退出码为 1，可挂 CI。

生成器写 `models/` 时会顺手带上 `PYTHONUTF8=1` 跑 `wren context build`，所以**你不需要记那个编码坑**——
只有自己手敲 `wren ...` 命令时才要用。

## 目录

- `backend/app`：FastAPI 后端
- `frontend`：Vue3 + Vite + Element Plus + ECharts
- `知识库文件`：RAGFlow 知识库镜像源目录
- `docs/superpowers/specs`：设计文档

## 安全

所有密码、API Key 只放在 `.env`，不要提交到 git。数据库只允许 `SELECT`，并做表白名单、行数上限和删除标记过滤。
