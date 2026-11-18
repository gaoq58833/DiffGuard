# DiffGuard

> 多 Agent 代码审查：规则先筛，专家并行复核，结论直接当 CI 门禁。

## 它解决什么问题

- 一次 PR 全量丢给模型，token 高、结论不稳、出了问题没法归因。
- 安全、规范、性能混在一个 prompt 里，三类判断互相稀释。
- 没有量化口径，"审查得准不准" 只能靠感觉。

DiffGuard 把这三件事拆开：规则做零成本初筛，三个专家 Agent 各看各的，最后由确定性规则收敛成放行 / 评论 / 阻断。

## 功能

- **规则前置**：14 条内置规则（安全 6 / 性能 3 / 规范 5），不调模型也能出结论。
- **三专家并行**：安全 / 规范 / 性能独立上下文，互不污染。
- **全链路异步**：模型调用用 `await`，等待期间不占线程；CPU 型解析走线程池。
- **确定性结论**：P0、P1 阻断，P2 评论，仅 P3 放行，同一份 diff 结果可复现。
- **送审脱敏**：密钥类命中在发给模型前替换为 `<REDACTED>`。
- **无 key 降级**：未配置密钥时只跑规则，流程不中断。
- **HTTP 服务**：与 CLI 共用同一内核，FastAPI 暴露 `/review`，图在启动时编译一次。

## 快速开始

前置要求：Python 3.11+、[uv](https://docs.astral.sh/uv/)。

```powershell
uv sync
Copy-Item .env.example .env
# 编辑 .env，把 DASHSCOPE_API_KEY 换成百炼控制台里的真实密钥
uv run diffguard review --diff tests/fixtures/sample.patch --title "feat: 按 uid 查询用户"
```

预期输出（节选）：

```text
## DiffGuard 审查报告

**PR**：feat: 按 uid 查询用户
**结论**：阻断 (BLOCK)
问题数：3 (P0 1 / P1 2 / P2 0 / P3 0)
```

进程退出码为 `2`。跑单测：

```powershell
uv run pytest        # 33 passed
```

也可以起 HTTP 服务：

```powershell
uv run uvicorn diffguard.app:app --port 8000
```

```powershell
$body = @{
    diff     = Get-Content tests/fixtures/sample.patch -Raw
    pr_title = "feat: 按 uid 查询用户"
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/review -Method Post -ContentType 'application/json' -Body $body
```

## 用法

| 命令 | 作用 |
| --- | --- |
| `uv run diffguard review --diff fix.patch` | 审查补丁并打印 Markdown |
| `uv run diffguard review --diff fix.patch --json` | 输出结构化 JSON |
| `uv run diffguard review --diff fix.patch --out review.md` | 报告写入文件 |
| `uv run diffguard review --diff fix.patch --no-llm` | 只跑规则，不调模型 |
| `uv run uvicorn diffguard.app:app --port 8000` | 启动 HTTP 服务 |

从管道读 diff：

```bash
git diff origin/main...HEAD | uv run diffguard review --diff -
```

退出码：`0` 放行或评论，`2` 阻断。CI 里不需要额外解析输出。

JSON 输出字段：`verdict`（`pass` / `comment` / `block`）、`issues`、`stats`、`trace`。

## HTTP 服务

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/health` | 返回模型映射、是否启用模型、并发上限 |
| `POST` | `/review` | 提交 diff，返回与 `--json` 同构的 `ReviewReport` |

请求体：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `diff` | string | 必填 | unified diff，不能为空 |
| `pr_title` | string | `""` | PR 标题 |
| `use_llm` | bool | `true` | 置 `false` 只跑规则；未配密钥时自动降级 |

服务启动时编译一次图，后续请求只新建状态。超过 `service.max_concurrent_reviews` 的请求排队等待；单次审查超过 `budget.task_timeout_seconds` 返回 `504`。

## MCP 工具服务

工具层可以独立作为 MCP Server 启动，供 Claude Desktop、其他 Agent 或脚本复用。内部图仍然直接调用同一批函数，两条路径的结果完全一致。

```powershell
uv run diffguard-mcp                      # stdio，供本地客户端
uv run diffguard-mcp --http --port 8124   # streamable HTTP
```

| Tool | 说明 |
| --- | --- |
| `parse_diff` | 解析 unified diff，返回文件、hunk 与行号映射 |
| `scan_rules` | 跑内置正则规则，默认只返回新增行命中 |
| `scan_complexity` | 按圈复杂度阈值扫描改动触及的函数 |
| `scan_blocking_io` | 找出异步函数中的同步阻塞调用 |

客户端接入（以 Claude Desktop 为例）：

```json
{
  "mcpServers": {
    "diffguard": {
      "command": "uv",
      "args": ["--directory", "f:/DIffGuard", "run", "diffguard-mcp"]
    }
  }
}
```

按方案的分工表，只有依赖重、需要跨 Agent 复用的工具对外暴露；脱敏与编排逻辑留在进程内。

## 整体流程

```text
CLI / HTTP
    │
    ▼
parse_diff ──→ static_scan ──→ supervisor
                                  │
                ┌─────────────────┼─────────────────┐
                ▼                 ▼                 ▼
         security_agent     style_agent       perf_agent     （并行）
                └─────────────────┼─────────────────┘
                                  ▼  屏障：全部分支完成
                              aggregate
                                  │
                                  ▼
                     Markdown / JSON / 退出码
```

| 阶段 | 节点 | 做什么 | 写入状态 |
| --- | --- | --- | --- |
| 1 解析 | `parse_diff` | unified diff → 文件、hunk、新增行号映射 | `files` |
| 2 初筛 | `static_scan` | 14 条规则 + AST 扫描，零模型成本 | `candidates`、`issues` |
| 3 派发 | `supervisor` | 决定派发名单（当前恒为三路） | `dispatch` |
| 4 复核 | 三个专家并行 | 排序、裁剪、脱敏后交给模型，产出问题 + 建议 | `issues`、`expert_results` |
| 5 屏障 | — | 等全部分支完成，按 reducer 归并 | — |
| 6 汇总 | `aggregate` | 去重、排序、定级、统计 | `final_issues`、`final_verdict`、`stats` |
| 7 输出 | `report` | Markdown 或 JSON；退出码 0 放行、2 阻断、1 错误 | — |

异步边界：专家节点是 `async def`，等模型返回时不占线程；`parse_diff` 与静态扫描是 CPU 型工作，放在 `asyncio.to_thread` 里；`aggregate` 是纯计算，保持同步。

三个专家拿的是同一份状态快照，静态候选只影响各自看到的上下文排序，不决定谁被执行：三路始终全量派发。归并发生在 superstep 屏障之后，并行不会互相覆盖。

## Agent 职责

| Agent | 模型 | 负责 | 明确不报 |
| --- | --- | --- | --- |
| `security_agent` | `qwen3.7-max` | SQL / 命令 / 模板注入、反序列化、硬编码凭据、`eval`/`exec`/`shell=True`、路径穿越、弱哈希 | 越权与权限模型（单文件 diff 判不了）、命名与格式 |
| `style_agent` | `qwen3.8-flash` | 命名与既有约定冲突、重复逻辑、异常处理缺失（裸 except / 静默吞）、圈复杂度过高、注释与实现不符 | 个人偏好（引号、空行、行宽）、架构选型、性能 |
| `perf_agent` | `qwen3.7-plus` | 循环内查询（N+1）、无界查询、重复 IO、热路径重计算、异步上下文中的同步阻塞 | 没有量级或 IO 次数证据的猜测 |

三个专家都只输出问题与文字建议，不下结论；`style` 与 `perf` 的提示词禁用 P0/P1，因此这两个维度不会导致阻断。系统提示在 [prompts/](file:///f:/DIffGuard/src/diffguard/prompts)。

## 状态字段

全局状态只有一份 `PRReviewState`，字段与写入方式如下（定义见 [state.py](file:///f:/DIffGuard/src/diffguard/state.py)）：

| 分组 | 字段 | 类型 | 写入方式 | 写入者 |
| --- | --- | --- | --- | --- |
| 输入 | `pr_title`、`diff` | `str` | 只读 | 入口一次性写入 |
| 输入 | `files` | `list[FileDiff]` | 覆盖写一次 | `parse_diff` |
| 候选 | `candidates` | `list[Issue]` | 覆盖写一次 | `static_scan` |
| 控制 | `dispatch` | `list[str]` | 覆盖写一次 | `supervisor` |
| 控制 | `steps` | `int` | 追加（`operator.add`，每次 +1） | 所有节点 |
| 结论 | `issues` | `list[Issue]` | 追加（`operator.add`） | `static_scan` + 三个专家 |
| 结论 | `expert_results` | `list[ExpertResult]` | 追加（`operator.add`） | 三个专家 |
| 结论 | `final_issues` | `list[Issue]` | 覆盖写一次 | **仅** `aggregate` |
| 结论 | `final_verdict` | `str` | 覆盖写一次 | **仅** `aggregate` |
| 轨迹 | `trace` | `list[TraceEvent]` | 追加（`operator.add`） | 所有节点 |
| 出口 | `stats` | `dict[str, int]` | 覆盖写一次 | **仅** `aggregate` |

两条不变式：

- 带 reducer 的字段只能追加，不能替换。所以汇总结论放在 `final_*`，而不是覆写 `issues`。
- 无 reducer 的字段只有一个写入者；两个节点同时写会报并发写冲突。

## 工具清单

全部实现为同步 Python 函数。其中 4 个按方案分工表通过 MCP Server 对外暴露，图内部直接 import 调用，不经过 MCP。

| 工具 | 形式 | 实现 | 使用位置 | MCP |
| --- | --- | --- | --- | --- |
| diff 解析 | 纯函数（CPU 型，经 `to_thread`） | `tools/diff.py` | `parse_diff` 节点、MCP | `parse_diff` |
| 规则引擎 | 纯函数（正则） | `tools/rules.py` | `static_scan` 节点、MCP | `scan_rules` |
| 圈复杂度 | 纯函数（`ast` 模块） | `tools/ast_scan.py` | `static_scan` 节点、MCP | `scan_complexity` |
| 阻塞 IO 检测 | 纯函数（`ast` 模块） | `tools/ast_scan.py` | `static_scan` 节点、MCP | `scan_blocking_io` |
| 脱敏 | 纯函数 | `tools/redact.py` | 送审上下文、证据输出 | 否 |
| 上下文装配 | 节点内逻辑 | `nodes/experts.py` | 三个专家 | 否 |
| 模型调用 | `ChatOpenAI` + 结构化输出 | `nodes/experts.py` | 三个专家 | 否 |

## 去重与归并

规则和专家可能对同一个问题各报一条，汇总阶段按 `(文件, 起始行, 规则标识)` 去重：

- **键相同**：保留规则那条。规则是确定性判定，同一份 diff 每次结果一致；模型只是拿到 diff 后的二次筛查。
- **模型声明重复**：专家输出里的 `related_rule_id` 表示「本条就是候选清单中的某一条」。只有该编号确实在本次候选内、且文件与行号完全对上时才认，编号被改写为规则编号，从而走同键合并。
- **对不上就不合并**：编号不存在、行号有偏差、或干脆留空，都按独立问题保留。

方向是宁可多报一条重复，也不在不确定时删掉一条。

## 内置规则

规则只在新增行上运行，不调模型；产出的 `category` 决定候选进入哪个专家的上下文，不影响是否派发（三路始终全量）。

| 规则 | 维度 | 等级 | 触发条件 |
| --- | --- | --- | --- |
| `SEC_SQL_INJECTION` | 安全 | P0 | SQL 关键字与字符串拼接 / 格式化同现 |
| `SEC_HARDCODED_SECRET` | 安全 | P1 | 硬编码密钥、令牌、口令（证据自动脱敏） |
| `SEC_EVAL_EXEC` | 安全 | P1 | `eval` / `exec` / `shell=True` |
| `SEC_PICKLE_LOADS` | 安全 | P1 | 对不可信输入直接 `pickle.loads` |
| `SEC_PATH_TRAVERSAL` | 安全 | P1 | 外部输入直接参与路径拼接或读文件 |
| `SEC_WEAK_HASH` | 安全 | P2 | `md5` / `sha1` 用于口令或校验 |
| `PERF_LOOP_IN_QUERY` | 性能 | P2 | 循环体内逐条发起查询（N+1） |
| `PERF_BLOCKING_IO_IN_ASYNC` | 性能 | P2 | `async def` 内调用 `time.sleep`、`requests.*`、`httpx.*` 同步方法 |
| `PERF_RANGE_LEN` | 性能 | P3 | `for i in range(len(x))` |
| `STY_BARE_EXCEPT` | 规范 | P2 | 裸 `except:` |
| `STY_MUTABLE_DEFAULT_ARG` | 规范 | P2 | 可变对象作默认参数（`def f(x=[])`） |
| `STY_DEBUG_BREAKPOINT` | 规范 | P2 | 遗留的 `breakpoint()` / `pdb.set_trace()` |
| `STY_WILDCARD_IMPORT` | 规范 | P3 | `from x import *` |
| `STY_COMPLEX_FUNC` | 规范 | P3 | 函数圈复杂度超过阈值（默认 10），且本次改动触及该函数 |

`PERF_BLOCKING_IO_IN_ASYNC` 与 `STY_COMPLEX_FUNC` 走 AST，能判断「调用是否在异步函数内」「函数是否被本次改动触及」；其余为逐行正则。只报新增行，存量代码不会刷屏。

## 配置

`config/models.yaml` 集中管理模型与预算，代码里不出现模型名。

| 环境变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DASHSCOPE_API_KEY` | 否 | — | 百炼密钥；缺失时只跑规则 |
| `DIFFGUARD_CONFIG` | 否 | `config/models.yaml` | 配置文件路径 |
| `DIFFGUARD__<分组>__<字段>` | 否 | — | 覆盖任意 YAML 节点，如 `DIFFGUARD__BUDGET__PER_PR_TOKEN_BUDGET=30000` |

常用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `budget.per_agent_context_tokens` | `8000` | 单个专家的上下文上限，超出即裁剪代码块 |
| `budget.per_pr_token_budget` | `50000` | 单任务上限，超限降级为仅规则审查 |
| `budget.task_timeout_seconds` | `300` | 任务超时（秒），超时服务层返回 504 |
| `service.max_concurrent_reviews` | `4` | 服务层同时运行的审查数，超出排队 |
| `rules.complexity_threshold` | `10` | 圈复杂度阈值 |
| `rules.hunk_context_lines` | `20` | AST 分析使用的上下文行数 |

优先级：环境变量 > `config/models.yaml` > 代码默认值。

## 项目结构

```text
src/diffguard/
  cli.py            命令行入口（review 子命令、退出码）
  app.py            FastAPI 服务层（/health、/review）
  mcp_server.py     MCP 工具服务（4 个 tool）
  graph.py          图编排：build_graph()、run_graph()、review()
  state.py          全局状态契约与 reducer
  schemas.py        Issue / ExpertResult / Verdict / TraceEvent
  config.py         YAML + 环境变量配置
  models.py         模型构建与无密钥降级
  report.py         decide_verdict()、render_markdown()
  nodes/pipeline.py     解析、初筛、派发
  nodes/experts.py      三个专家 Agent
  nodes/aggregate.py    去重、定级、统计
  tools/                diff 解析、规则库、AST 扫描、脱敏
  prompts/              三份专家系统提示
config/models.yaml  模型 ID、三层预算、阈值
tests/              单测与 fixture
```

## 接入 CI

```yaml
- run: git diff origin/main...HEAD > pr.patch
- run: uv sync
- run: uv run diffguard review --diff pr.patch --out review.md
```

阻断时退出码为 `2`，job 直接失败。需要把报告贴回 PR 时读 `review.md`，或加 `--json` 后解析 `issues`。

## 排障

| 症状 | 原因 | 解法 |
| --- | --- | --- |
| 结论恒为放行、`issues` 为空 | 未配置密钥，规则也未命中 | 检查 `DASHSCOPE_API_KEY`，用 `--json` 看 `trace` 是否进了专家节点 |
| 报 404 model not found | `models.yaml` 里的模型 ID 已下线 | 对照百炼模型页改 `models.<agent>.name` |
| 报告里出现 `<REDACTED>` | diff 含密钥，脱敏生效 | 预期行为，原文不会外发 |
| 本地退出码是 2 但 CI 没红 | 命令链吞了退出码 | 去掉 `\|\| true` 一类的兜底 |
| 改了 `config/models.yaml` 不生效 | 同名环境变量优先级更高 | 用 `DIFFGUARD__...` 统一覆盖，或先清空变量 |

## 限制

- M1 只做单轮审查，不做跨文件数据流分析。
- 汇总是确定性规则，尚未接入模型 judge。
- 专家对静态候选的「否定」不会移除该条命中：候选在 `static_scan` 阶段已写入 `issues`。
- 去重依赖模型主动填写 `related_rule_id`；漏填时同一问题会重复上报（多报，不漏报）。
- 服务层只有并发限流，没有鉴权，只应监听内网或置于反向代理之后。
- 评测集与门禁指标在 M2 补齐。

## 许可证

内部项目，暂未附带开源许可证。
