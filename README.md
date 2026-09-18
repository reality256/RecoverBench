# RecoverBench

一个用来测试 AI Agent 故障恢复能力的 benchmark。V0 只做一种故障:**Redis 挂掉**。

## 架构

```
Client ──▶ FastAPI App ──▶ Redis
               ▲
               │ inspect / repair
          ┌────┴────┐
          │  Agent  │ (tools: health / logs / status / start)
          └────┬────┘
               │
          ┌────┴─────┐
          │ Evaluator │ (PASS / FAIL)
          └──────────┘
```

## 跑实验(推荐入口)

一条命令重复执行"注入故障 → Agent 恢复 → 独立裁判",每轮独立重置、独立轨迹:

```bash
python scripts/run_benchmark.py --policy rule --scenario redis_down --repeat 5
python scripts/run_benchmark.py --policy llm  --scenario redis_down --repeat 5
python scripts/run_benchmark.py --policy llm  --scenario healthy    --repeat 5
```

每轮:恢复到干净基线 → 等待就绪 → 预置业务测试数据 → 验证基线 → 注入故障(或保持正常)
→ 验证场景状态 → 启动 Agent → 独立裁判评测 → 保存结果 → 清理环境。

- 基线或注入失败 → 记为 `setup_failed`,**不启动 Agent**
- 清理放在 `finally`,清理失败立即中止整个实验(避免污染后续轮次)
- Agent 自述与裁判结果**分别保存**,准备/Agent/裁判耗时分别记录
- 结果在 `runs/<bench_id>/`:每轮 `r<N>/{config,trace,result}.json`,外加 `summary.json` 与 `rounds.jsonl`

退出码:0 = 全部 PASS,1 = 有轮次 FAIL,2 = 实验基础设施出错。

汇总成对照表(可传多个 benchmark ID):

```bash
python scripts/report_benchmark.py                       # 汇总全部
python scripts/report_benchmark.py exp-rule-healthy exp-llm-healthy
```

输出:策略 × 场景的结果表(有效轮数 / 通过率 / 耗时 / 工具数 / 修改数 / Token),
外加无效实验、错报成功、失败原因清单。
注意 **healthy 的通过率代表"保持健康",与故障恢复成功率含义不同,需分开解读**。

## 裁判(独立评测)

```bash
python evaluator/evaluate.py --key race:test --value <期望值>   # 健康 + 业务读取
python evaluator/evaluate.py                                    # 只做健康检查(便捷模式)
```

判定 = 健康检查与业务读取**连续通过 3 次**(间隔 1 秒,截止时间内完成)。
结果三态:**PASS / FAIL / ERROR**,退出码 **0 / 1 / 2**(ERROR = 裁判输入不完整或判官自身异常)。

**Agent 说成功不算数**。曾实测:一个什么都不做的"谎报模型"自称 `RECOVERED`(退出码 0),
裁判独立判定 `FAIL` —— 两者分别记在 `result.json` 的 `label` 与 `evaluator` 字段里。

## 手动跑通闭环(教学用)

```bash
# 1. 启动环境
docker compose up -d --build

# 2. 确认健康
curl http://localhost:8000/health   # {"status": "healthy", "redis": "healthy"}

# 3. 注入故障
python scripts/inject_fault.py

# 4. 确认降级
curl http://localhost:8000/health   # {"status": "degraded", ...}

# 5. 恢复环境
python scripts/reset_env.py

# 6. 独立评估
python evaluator/evaluate.py        # PASS
```

## Agent 自动恢复(推荐流程)

Agent 只用受限工具(不调 reset_env.py)自行观察、诊断、恢复,和裁判相互独立:

```bash
# 1. 启动环境
docker compose up -d --build

# 2. 注入故障
python scripts/inject_fault.py

# 3. 让 Agent 自己诊断并恢复
python agent/agent.py
```

预期输出:

```
[1] get_api_health({}) → {'ok': True, 'data': {'status': 'degraded', 'redis': 'unavailable', ...}, 'error': None}
[2] get_service_status({'service': 'redis'}) → {'ok': True, 'data': {'service': 'redis', 'running': False, 'state': 'exited'}, 'error': None}
[3] start_service({'service': 'redis'}) → {'ok': True, 'data': {'action': 'docker compose start redis', ...}, 'error': None}
[4] get_api_health({}) → {'ok': True, 'data': {'status': 'healthy', 'redis': 'healthy', ...}, 'error': None}
[5] 策略结束 → recovered: 启动后第 1 次验证,API 恢复健康
结论: RECOVERED(启动后第 1 次验证,API 恢复健康)

退出码: 0
```

```bash
# 4. 独立评估
python evaluator/evaluate.py        # PASS
```

退出码含义:

| 退出码 | 含义 |
|---|---|
| 0 | 已健康(ALREADY_HEALTHY)或恢复成功(RECOVERED) |
| 1 | 诊断后仍无法恢复(UNRESOLVED / FAILED) |
| 2 | Agent 自身出错(工具调用失败、超出预算 BUDGET_EXCEEDED) |

## 模型策略(LLM)

同一个循环、同一套工具,把决策者从规则换成模型:

```bash
cp .env.example .env    # 填入 LLM_API_KEY(https://platform.deepseek.com)
python agent/agent.py --policy llm
```

模型:`deepseek-flash`(默认,快且便宜)或 `deepseek-v4-pro`(更强)。
可用模型名请以 `GET https://api.deepseek.com/v1/models` 为准 ——
`deepseek-chat`/`deepseek-reasoner` 是已弃用的兼容别名,不要再用。

换模型前后的体检(一次调用,几百 token):

```bash
python scripts/check_llm.py     # 验证 配置 + 认证 + 多轮工具调用
```

不想花 API 费用先验证链路?用脚本化的假模型跑真实环境:

```bash
python scripts/run_llm_dryrun.py
```

**模型每轮能看到**:任务描述(故障症状)、服务拓扑、工具描述、之前的调用与结果、剩余预算。
**模型看不到**:场景 YAML、注入/恢复脚本、`expected.root_cause`、裁判内部答案、含答案的场景名。
正常对照场景与故障场景使用**同一段任务描述**。

安全边界:模型输出只会被解析成"工具调用"或"结束结论",绝不会被当作 Python/shell 执行;
工具名必须在注册表内、参数必须过分发器校验。

**Agent 的结论 ≠ 事实**。退出码反映的是 Agent(或模型)自己的判断;
最终成功与否由裁判决定,两者都记在 `runs/<run_id>/result.json` 里:

```bash
python evaluator/evaluate.py --attach runs/<run_id>
python scripts/inspect_run.py <run_id>
```

## 预算控制

每次运行都有硬预算,防止策略失控(尤其是接模型之后):

| 预算 | 默认值 |
|---|---|
| 工具调用次数 | 10 次(非法调用也消耗,防止模型一直输出无效参数) |
| 环境修改次数 | 2 次 |
| 整轮时间 | 120 秒(单调时钟;所有工具/模型超时都被"剩余时间"夹住) |
| 单次模型调用 | 30 秒独立超时 |
| 模型接口重试 | 最多 1 次,计入整轮预算 |

超预算时结论为 `BUDGET_EXCEEDED`,退出码 2 —— 循环一定会结束,且不会执行未授权动作。

## 运行轨迹

每次运行都会写一份可回放的记录:

```
runs/<run_id>/
  config.json    代码版本、策略、模型、prompt 版本、预算
  trace.jsonl    每次决策、工具参数、结果、耗时、模型用量
  result.json    Agent 结论、裁判结果、耗时、调用次数
```

查看方式(只看运行目录就能还原"看到了什么、做了什么、为什么结束"):

```bash
python scripts/inspect_run.py           # 看最近一次
python scripts/inspect_run.py <run_id>  # 看指定一次
```

裁判结果由 evaluator 独立写入(`evaluator/evaluate.py --attach runs/<run_id>`),Agent 不调用裁判。
API key 等凭据在写盘前会被自动替换为 `***`。

## 测试

```bash
python -m unittest discover -s tests -v   # 单元测试(工具/预算/分发/轨迹/策略)
python scripts/run_e2e.py                 # 端到端验收
```

单独测试工具层:

```bash
python -c "from agent.tools import get_api_health, get_service_status; print(get_api_health()); print(get_service_status('redis'))"
```

## 升级路线

Redis Down → Redis Latency → API Crash → DB unavailable → CPU spike → Bad config → Prometheus metrics → multi-fault → Kubernetes → ITBench
