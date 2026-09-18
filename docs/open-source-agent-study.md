# RecoverBench 相近开源项目调研与学习指南

调研日期：2026-09-18。阅读对象：希望逐步把 RecoverBench 从 Redis 单故障实验发展成可靠 agent 评测平台的开发者。

## 1. 结论与阅读顺序

有高度相关的开源项目。最值得学习的是 **AIOpsLab、ITBench、ITBench 配套 Agent、HolmesGPT**，但它们分别解决评测平台、场景构建、诊断执行、运维调查等不同问题。

建议先读 AIOpsLab 的编排与记录实现，再看 ITBench 的场景组织，最后学习两个 agent 项目的工具设计。RecoverBench 已有合适的基础架构，下一步应优先让实验可重复、结果可解释，再接入 LLM 和更多故障。

本文依据本地代码、官方 GitHub 仓库、部分源文件与官方说明整理。没有部署或实测这些外部项目，也没有进行性能排名。“适合借鉴”和实施方案属于针对 RecoverBench 的设计建议；不把 README 的功能声明当成已验证的运行效果。链接指向调研时默认分支，后续可能变化。

## 2. 你的项目实际已经做到哪里

以代码为准，而不是旧施工计划：

| 部件 | 当前实现 | 意义 |
|---|---|---|
| 环境 | FastAPI + Redis，Docker Compose | 低成本、可观察的真实故障环境 |
| 注入 | `scripts/inject_fault.py` | 已有独立故障入口 |
| 决策 | `agent/policies/rule.py` | 根据历史记录选择动作的确定性基线 |
| 执行 | `agent/loop.py` | 公共循环、工具注册检查、10 步上限、工具异常转换 |
| 工具 | `agent/tools.py` | 四个工具，查询服务白名单，写操作仅允许启动 Redis |
| 状态 | `agent/state.py` | `ToolCall`、`Finish`、`HistoryEntry` |
| 裁判 | `evaluator/evaluate.py` | 独立查询 `/health`，输出 PASS/FAIL |
| 验收 | `scripts/run_e2e.py` | 健康基线→故障→恢复→裁判的完整流程 |
| LLM | `agent/policies/llm.py` | 只有接口占位，尚未接入模型 |

因此，你做的是“**故障恢复 benchmark + 可替换的答题 agent**”。只搜索聊天 agent 或通用多 agent 框架，会错过最相关的设计。

已有实现值得保留：策略与执行分开；工具成功不等于服务健康；agent 不调用标准恢复脚本；裁判在 agent 之外；规则策略可作为长期基线。

文档存在滞后：`steps.md` 仍称 agent 文件为空；README 示例使用旧 dataclass 返回值和 `restart_service`，实际工具为 dict 协议与 `start_service`，当前规则策略也没有读日志步骤。后续应同步，避免照文档理解错当前架构。

## 3. 项目对照

| 项目 | 类别与接近程度 | 最值得学 | 引入成本判断 |
|---|---|---|---|
| [AIOpsLab](https://github.com/microsoft/AIOpsLab) | 很高；agent 与故障环境交互的评测平台 | 实验生命周期、任务接口、轨迹记录 | 整体部署较重；抽取设计成本低 |
| [ITBench](https://github.com/itbench-hub/ITBench) | 很高；面向 IT 自动化的场景与评测体系 | 场景规范、故障机制、环境组织 | Kubernetes 场景对当前阶段较重 |
| [ITBench-CISO-SRE-FinOps-Agent](https://github.com/itbench-hub/ITBench-CISO-SRE-FinOps-Agent) | 相关；配套 agent 与分析工具 | runner、工具、结果评估分工 | 外部组件与数据依赖较多 |
| [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) | 相关；生产事件调查 agent | 工具调用循环、上下文控制、观测数据接入 | 学局部实现即可，不必整体迁入 |

成本是结合项目文档与 RecoverBench 规模的定性判断，没有实际安装计时。这里按功能匹配与可学习性筛选，不按 star 数证明质量。

## 4. AIOpsLab：最优先学习的评测骨架

### 官方实现

AIOpsLab 将应用、任务、故障、负载、评估器组织成问题；agent 通过 `get_action` 接入，由 orchestrator 管理交互。其任务覆盖检测、定位、分析和缓解。它更接近 RecoverBench 的整体目标，而非单独一个修复机器人。来源：[官方 README](https://github.com/microsoft/AIOpsLab#readme)。

源码中，`init_problem` 负责环境与问题初始化；`ask_agent` 和 `ask_env` 分隔决策与执行；`start_problem` 控制循环，在评估后保存结果并清理故障，异常路径也安排恢复。来源：[orchestrator.py](https://github.com/microsoft/AIOpsLab/blob/main/aiopslab/orchestrator/orchestrator.py)。

`Session` 保存会话 ID、问题、agent、时间、交互轨迹和结果，并提供 JSON 导出。来源：[session.py](https://github.com/microsoft/AIOpsLab/blob/main/aiopslab/session.py)。

### 对 RecoverBench 的启发

最值得迁移的是实验生命周期，而非部署栈。建议把现在的端到端脚本发展为统一 runner：

```text
准备环境 → 等待健康 → 注入故障 → 确认故障有效
                                 ↓
                       policy → tool → observation
                                 ↓
                     独立裁判 → 保存结果 → 清理
```

注意顺序：必须先保存裁判结果，再做环境清理。清理启动了 Redis，不能算 agent 修复成功。

你已有 `HistoryEntry`，可以直接扩展落盘，不必替换为大型框架。每轮保存 `run_id`、场景、策略、版本、起止时间、每次调用、工具结果和独立裁判结论。这样遇到失败时才能回答：是没查状态、选错动作、工具失败，还是环境根本没准备好？

推荐阅读顺序：README 接入说明 → `orchestrator.py` 的三个交互方法 → `session.py`。阅读时把它们对应到本地 `run_e2e.py`、`loop.py`、`state.py`，理解会更快。

## 5. ITBench：学习如何把“一个故障”变成“题库”

### 官方实现

ITBench 覆盖 SRE、CISO、FinOps，提供 Kubernetes 环境、场景与参考 agent。仓库将 `scenarios`、`schemas/json`、`components`、`clusters` 等内容分开组织。来源：[官方仓库](https://github.com/itbench-hub/ITBench)。

它对你的主要价值是场景工程。优先从 [scenarios](https://github.com/itbench-hub/ITBench/tree/main/scenarios) 与 [schemas/json](https://github.com/itbench-hub/ITBench/tree/main/schemas/json) 的入口理解组织方式；本文没有逐一执行或审计其中所有场景。

### 对 RecoverBench 的启发

你的 `scenarios/redis_down.yaml` 已声明故障、预期根因、成功条件、120 秒超时，但目前读取的执行代码中没有加载它，实际流程仍硬编码。建议首先让一个场景文件真正驱动实验。

场景需区分两种信息：

| agent 可见 | runner / evaluator 专用 |
|---|---|
| 故障症状、允许的观测工具、服务拓扑 | 注入方式、准确根因、标准恢复操作 |
| 允许的操作与预算 | 独立成功标准、清理方法 |

这是本项目的建议，并不意味着上述仓库以完全相同的字段划分。当前只有 Redis 一题，提示 agent“Redis 被停了”会使诊断失去意义；以后评测泛化能力时，不能把 `expected.root_cause` 原样放入提示词。

增加场景时，应先明确可观测差异与恢复动作。例如：

| 候选场景 | 要测的能力 | 前置工作 |
|---|---|---|
| 无故障 | 是否避免无谓修改 | 保持写动作计数 |
| Redis 停止 | 基础恢复 | 已有工具即可 |
| API 停止 | 是否定位到不同组件 | 决定是否允许启动 API，另设权限配置 |
| Redis 延迟 | 是否识别性能退化 | 加入可控延迟机制、耗时观测和移除延迟动作 |
| 错误配置 | 是否区分运行与可用 | 加配置观测及受限修复接口 |

不要只增加故障名而复用同一种恢复答案，也不要给 agent 一道没有合法修复工具的题，却把失败完全归因于模型能力。

## 6. ITBench 配套 Agent：重点学工具，注意版本迁移

### 官方实现与当前边界

旧 [ITBench-SRE-Agent](https://github.com/itbench-hub/ITBench-SRE-Agent) 明确标记归档并指向新仓库。当前 [新仓库 README](https://github.com/itbench-hub/ITBench-CISO-SRE-FinOps-Agent) 展示的是 Zero runner、SRE MCP 工具与评估组件，不能沿用旧版 CrewAI 架构介绍来描述当前版本。

[SRE Tools 文档](https://github.com/itbench-hub/ITBench-CISO-SRE-FinOps-Agent/blob/main/sre_tools/README.md) 提供告警汇总、事件/指标分析、依赖拓扑、变更分析与实体上下文聚合；其中 `offline_incident_analysis` 面向离线数据。快照诊断能评估分析质量，但不能独自证明在线服务已被修复。

### 对 RecoverBench 的启发

优秀工具应帮助 agent 获取有范围的证据。例如，未来可以增加“某个服务在某个时间段的错误统计”，而不是把所有日志一次性塞入上下文。

当前只有四个工具，Python 函数注册表已经够用。接 LLM 时建议补一个明确的工具规范，含名称、用途、参数 schema、是否修改环境、超时、输出上限；MCP 可在需要多个客户端共用这些工具时再引入。

以后增加多服务故障，可让诊断结果包含 `suspected_component`、`evidence_steps`、`proposed_action`。这些字段是本文建议，不是对外部仓库协议的复制。证据引用应指向真实观测；一个流畅的根因解释不应替代修复后检查。

阅读重点：新仓库 Components → `sre_tools/README.md` 的工具参数与返回示例 → 按需查看 runner 配置。暂不必接入完整离线数据、代理服务和所有观测组件。

## 7. HolmesGPT：学习如何让诊断 agent 不被数据淹没

### 官方实现

HolmesGPT 通过 agent 循环查询观测数据调查事件，使用 toolsets 接入不同数据源；README 描述了服务端过滤、工具输出处理等能力。它与 RecoverBench 的诊断部分相近，但不是故障恢复 benchmark 的直接替代品。来源：[官方 README](https://github.com/HolmesGPT/holmesgpt#readme)。

其 [tool_calling_llm.py](https://github.com/HolmesGPT/holmesgpt/blob/master/holmes/core/tool_calling_llm.py) 可见专门的工具执行器、最大步数、重复工具调用防护、上下文压缩和过大工具结果处理，以及调用统计结构。本文对这些是源码结构层面的核实，没有运行验证所有边界行为。

### 对 RecoverBench 的启发

你已经限制日志行数为 1–100、返回字符数为 2000，这个方向正确。后续应保留截断提示，并加入时间范围和服务过滤，让模型知道自己看到的是局部信息。

还应区分有意义的轮询和无效重复：启动 Redis 后最多五次健康检查是合理验证；没有新动作也没有状态变化，却不断读取相同日志，通常是在消耗预算。重复检测应结合参数、结果和阶段，不能简单禁止同一工具调用两次。

如果最后两次观测已足够说明权限或能力不足，让 agent 返回 `unresolved` 并保留证据，比无限尝试更适合作为可解释的实验结果。

## 8. 针对当前代码，最先补的五件事

以下来自本地静态阅读，是改进建议，本次没有修改业务代码。

### 8.1 先落盘轨迹与结果

`run_loop` 已返回历史，但入口将其接为 `_history` 后没有保存。建议一轮产生 `trace.jsonl` 和 `result.json`：前者逐步记录，后者记录最终结论。失败与预算耗尽也需要保存。

建议最小字段：

```json
{
  "run_id": "example-run",
  "scenario_id": "redis_down_001",
  "policy": "rule",
  "agent_conclusion": "recovered",
  "evaluator_pass": true,
  "fault_verified": true,
  "elapsed_seconds": null,
  "tool_calls": null,
  "write_actions": null,
  "termination_reason": "policy_finished"
}
```

这是字段示意，`null` 表示待实际测量，不是实验结果。接 LLM 后再加模型标识、提示词版本、采样参数、token 与费用信息。

### 8.2 让预算由执行层强制执行

目前 `MAX_STEPS` 限制循环轮数，但 `policy.decide()` 不在工具异常捕获范围内；也没有全局 deadline。`wait_seconds` 直接用于等待，未来不能让模型无限放大它。

建议由 runner/loop 统一约束总时限、单次模型调用时限、工具时限、等待上限和写操作次数。策略负责提出动作，执行层负责强制预算。场景 YAML 的 120 秒应真正传入这个机制。

### 8.3 提升裁判对“恢复”的判断

当前 evaluator 只检查一次 `/health`，未明确检查 HTTP 状态码；其脚本 PASS/FAIL 都没有显式设置失败退出码，现有 e2e 通过匹配 stdout 区分。

建议返回结构化判定与明确退出码，并同步修改调用方。恢复检查可以要求连续三次健康，再通过 `/cache/{key}` 读取预先设置的测试值验证业务路径。当前 API 没有写入口，测试数据应由环境准备阶段通过 Redis 写入，不能假设已有 HTTP 写接口。

裁判仍应独立于 agent。模型说“已恢复”只算提交结论，不算通过。

### 8.4 分清 agent 失败和实验无效

`run_e2e.py` 使用 `ok &= step(...)`，即使前置步骤失败，后续步骤仍会继续。基线不健康或注入未生效时，应停止该轮答题，记录 `invalid_run`，再进入清理。

建议用有限等待确认环境就绪，并用 `finally` 做清理。不要把清理后的健康状态拿去覆盖裁判已经记录的失败。

### 8.5 再接入最小 LLM 策略

沿用 `decide(history, available_tools)`，把模型响应解析成现有 `ToolCall` 或 `Finish`。增加工具参数与结论枚举校验，覆盖无效 JSON、未知工具、错误参数、模型超时等失败模式。

保留规则策略，使用同一工具权限、同一预算和同一裁判比较。日志属于被观察的数据，不应成为改变工具权限或运行规则的指令。

## 9. 建议的实施顺序与验收

| 阶段 | 工作 | 完成标准 |
|---|---|---|
| A：记录可信 | 轨迹落盘、结果协议、文档同步 | 成功与失败轮次都可复盘 |
| B：实验可信 | 场景加载、就绪等待、故障确认、预算、清理 | 前置失败不算有效样本，清理不影响得分 |
| C：模型可比 | 实现 LLM 策略和参数校验 | 规则与 LLM 共用执行层，可统计成本与失败类型 |
| D：能力可测 | 无故障对照与其他故障 | 有不同证据与不同动作，重复运行可汇总 |
| E：扩大环境 | 按需求加入指标、更多服务、Kubernetes | 现有场景接口可以复用 |

建议每个策略与场景先重复至少 10 轮做工程检查；这不是统计充分性的保证。报告成功次数和总次数，避免把少量全通过夸大为稳定泛化能力。无效轮次单独列出数量与原因，不能悄悄丢弃。

指标可先保持简单：

| 指标 | 推荐口径 |
|---|---|
| 恢复成功率 | 独立裁判通过的有效故障轮数 / 有效故障轮数 |
| 无故障正确率 | 无故障时保持健康且没有写操作的比例 |
| 恢复耗时 | agent 开始答题至首次满足稳定恢复条件；环境准备另计 |
| 调用与写动作数 | 分开统计观测成本和修改次数 |
| 越权请求 | 被拒绝的非法工具或参数请求单独计数 |
| 失败类型 | 推理错误、工具失败、超时、预算耗尽、未恢复分别记录 |

只对成功样本统计耗时时，应同时报告成功率和超时数。后续比较模型时，可随机交错运行顺序，固定环境与预算，减少环境漂移对结果的影响。

## 10. 按问题查阅的资料入口

| 想回答的问题 | 优先阅读 |
|---|---|
| benchmark 怎样组织 agent 和环境？ | [AIOpsLab README](https://github.com/microsoft/AIOpsLab#readme) |
| 谁负责循环、评估、清理？ | [AIOpsLab orchestrator.py](https://github.com/microsoft/AIOpsLab/blob/main/aiopslab/orchestrator/orchestrator.py) |
| 怎样保存一次实验？ | [AIOpsLab session.py](https://github.com/microsoft/AIOpsLab/blob/main/aiopslab/session.py) |
| 怎样组织更大题库？ | [ITBench 仓库](https://github.com/itbench-hub/ITBench) |
| 配套 agent 现在是什么结构？ | [ITBench 新 Agent 仓库](https://github.com/itbench-hub/ITBench-CISO-SRE-FinOps-Agent) |
| 一个诊断工具应返回什么？ | [SRE Tools 说明](https://github.com/itbench-hub/ITBench-CISO-SRE-FinOps-Agent/blob/main/sre_tools/README.md) |
| 如何处理重复调用和过大上下文？ | [HolmesGPT 工具调用循环](https://github.com/HolmesGPT/holmesgpt/blob/master/holmes/core/tool_calling_llm.py) |

最适合立即开始的练习：读完 AIOpsLab 的 `Session` 后，给 RecoverBench 现有历史记录增加持久化；再拿一轮成功和一轮失败结果，验证能否仅凭产物解释整次实验。
