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
uv run pytest        # 28 passed
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

## 架构

```text
parse_diff → static_scan → supervisor ─┬→ security_agent ─┐
                                       ├→ style_agent    ─┼→ aggregate → 报告
                                       └→ perf_agent     ─┘
```

| 层 | 职责 | 状态写入 |
| --- | --- | --- |
| 编排层 | 解析 diff、规则初筛、派发三个专家 | 覆盖写 `files`、`candidates`、`dispatch` |
| 专家层 | 三个 Agent 并行审查，只返回增量 | 追加写 `issues`、`expert_results` |
| 汇总层 | 去重、定级、算结论 | 唯一写 `final_issues`、`final_verdict`、`stats` |

三个专家拿的是同一份状态快照，归并发生在 superstep 屏障之后，因此并行执行不会互相覆盖。静态候选只影响各专家看到的上下文排序，不决定谁被执行：三路始终全量派发。

入口是 async 的：三个专家作为 asyncio 任务并发，等模型返回时不占线程；解析与静态扫描是 CPU 型工作，交给 `asyncio.to_thread`。

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
  app.py            FastAPI 服务层（/health、/review）
  graph.py          图编排与 review() 入口
  state.py          状态契约与 reducer
  schemas.py        Issue / Verdict / 报告数据结构
  nodes/            pipeline、experts、aggregate
  tools/            diff 解析、规则库、AST 扫描、脱敏
  prompts/          三份专家系统提示
config/models.yaml  模型 ID、预算、阈值
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
