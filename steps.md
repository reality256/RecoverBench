# RecoverBench 下一步施工计划

## 0. 当前进度

V0 的“题目”和“裁判”已经完成：

- FastAPI 能通过 `/health` 反映 Redis 状态。
- `scripts/inject_fault.py` 能稳定停掉 Redis。
- `scripts/reset_env.py` 能手动恢复 Redis。
- `evaluator/evaluate.py` 能独立输出 `PASS` / `FAIL`。
- 故障注入、恢复、评估已经重复验证过。

现在缺的是“答题者”：`agent/` 下的文件仍为空。因此下一步不要急着接 LLM，先做一个权限受限、行为可预测的确定性 Agent，把自动恢复闭环跑通。

最终目标是执行：

```powershell
python scripts/inject_fault.py
python agent/agent.py
python evaluator/evaluate.py
```

并得到：

```text
Agent: RECOVERED
PASS
```

---

## Step 6：实现 Agent 的受限工具层

### 6.1 在 `agent/state.py` 定义结构化状态

不要让 Agent 到处传递随意拼接的字符串。先定义几个简单的数据结构：

- `ApiHealth`：保存 API 是否可访问、`status`、Redis 状态和错误信息。
- `ServiceStatus`：保存服务名、是否运行和 Docker 返回的原始状态。
- `ActionResult`：保存动作是否成功、执行了什么动作、输出和错误。

推荐用 `dataclasses.dataclass`。状态字段保持简单，暂时不需要数据库或状态机框架。

完成标准：

- 状态可以直接打印，便于看 Agent 的判断过程。
- 网络失败、命令超时等情况都能用返回值表达，而不是直接让程序崩溃。

### 6.2 在 `agent/tools.py` 实现五个工具

实现以下函数：

```python
get_api_health()
get_service_status(service)
get_service_logs(service, tail=50)
restart_service(service)
verify_api()
```

各工具职责如下：

1. `get_api_health()`
   - 请求 `http://localhost:8000/health`。
   - 超时应略大于当前 Redis 故障时约 3.5 秒的响应时间，建议 6 秒。
   - 解析 JSON，并返回 `ApiHealth`。
   - HTTP 错误、超时、JSON 格式异常都转成结构化错误。

2. `get_service_status(service)`
   - 通过 `docker compose ps` 查看服务状态。
   - 使用 `subprocess.run([...])` 的参数列表形式，不使用 `shell=True`。
   - 设置命令超时，例如 10 秒。
   - 返回 `ServiceStatus`。

3. `get_service_logs(service, tail=50)`
   - 获取最近的容器日志，只用于诊断。
   - 限制日志行数，避免一次读入无限输出。

4. `restart_service(service)`
   - V0 只允许操作 `redis`，维护一个明确的 allowlist：`{"redis"}`。
   - 当前故障是容器被 `stop`，最直接的恢复命令是 `docker compose start redis`。
   - 禁止接收任意 shell 命令，禁止操作 allowlist 之外的服务。
   - 返回 `ActionResult`，不要静默吞掉失败。

5. `verify_api()`
   - 可以复用 `get_api_health()`。
   - 只有 `status == "healthy"` 且 `redis == "healthy"` 才算成功。

所有 Docker 命令都应在项目根目录执行。不要依赖“用户正好从哪个目录启动脚本”，可通过 `Path(__file__).resolve()` 推导项目根目录并传给 `subprocess.run(cwd=...)`。

### 6.3 先单独手测工具

环境正常时：

```powershell
docker compose up -d --build
python -c "from agent.tools import get_api_health, get_service_status; print(get_api_health()); print(get_service_status('redis'))"
```

预期：API healthy，Redis running。

故障状态时：

```powershell
python scripts/inject_fault.py
python -c "from agent.tools import get_api_health, get_service_status; print(get_api_health()); print(get_service_status('redis'))"
```

预期：API degraded，Redis not running；工具自身不崩溃。

权限边界测试：

```powershell
python -c "from agent.tools import restart_service; print(restart_service('api'))"
```

预期：拒绝操作 `api`，且不会真的重启它。

---

## Step 7：实现无 LLM 的确定性 Agent 循环

在 `agent/agent.py` 实现一条清晰的流程：

```text
观察 API
  ↓
健康？──是──▶ 输出 ALREADY_HEALTHY，结束
  │否
  ↓
查看 Redis 状态和最近日志
  ↓
Redis 未运行？──否──▶ 输出 UNRESOLVED，结束
  │是
  ↓
启动 Redis
  ↓
等待并重复验证 API
  ↓
健康？──是──▶ 输出 RECOVERED
  │否
  └────────▶ 输出 FAILED
```

### 7.1 建议的行为约束

- 最多执行一次修复动作，避免失控循环。
- 修复后最多验证 5 次，每次间隔 1 秒。
- 只在证据表明 Redis 未运行时启动 Redis。
- 每一步打印简短、结构化的观察和动作，便于复盘。
- 返回明确的进程退出码：
  - `0`：已健康或恢复成功。
  - `1`：诊断后仍无法恢复。
  - `2`：Agent 自身配置或工具调用出错。
- 不调用 `scripts/reset_env.py`。Agent 应通过受限工具完成恢复，避免把“标准答案脚本”直接当作 Agent。
- 不调用 evaluator。Agent 和裁判必须相互独立。

### 7.2 V0 的确定性决策规则

当前只有一个场景，因此规则可以非常小：

```python
if api_is_degraded and redis_is_not_running:
    start_redis()
    verify_until_healthy()
else:
    report_unresolved()
```

此处先不用 LLM。先证明工具、权限、恢复动作和验证闭环都可靠，后面再把“决策规则”替换成模型判断。

---

## Step 8：端到端验收

### 8.1 标准成功路径

```powershell
docker compose up -d --build
python evaluator/evaluate.py
python scripts/inject_fault.py
python evaluator/evaluate.py
python agent/agent.py
python evaluator/evaluate.py
```

预期顺序：

```text
PASS
FAIL
Agent: RECOVERED
PASS
```

### 8.2 幂等性测试

系统已经健康时直接运行：

```powershell
python agent/agent.py
```

预期：输出 `Agent: ALREADY_HEALTHY`，不重启任何服务。

### 8.3 可重复性测试

连续执行 3 轮：

```powershell
python scripts/inject_fault.py
python agent/agent.py
python evaluator/evaluate.py
```

每轮都应恢复成功并输出 `PASS`。把每轮结果和耗时补充到 `log.md`。

### 8.4 失败路径测试

至少验证以下情况不会让 Agent 崩溃：

- Docker Desktop 未启动。
- API 容器不可访问。
- `docker compose` 命令超时或失败。
- 健康接口返回非 JSON 内容。
- 请求重启 allowlist 以外的服务。

失败时应有明确错误信息和非零退出码，不能错误地报告 `RECOVERED`。

---

## Step 9：补测试和文档

自动恢复跑通后再做这一小轮收尾：

1. 为 `agent/tools.py` 的纯判断逻辑写单元测试，外部 HTTP 和 subprocess 用 mock 隔离。
2. 增加一个端到端测试脚本，按“启动 → 注入 → Agent 恢复 → evaluator 判定”的顺序执行。
3. 在 `README.md` 增加“Agent 自动恢复”的运行命令和示例输出。
4. 在 `log.md` 记录实现选择、踩坑、三轮复验结果和平均恢复耗时。

---

## 暂时不要做的事

在上述闭环稳定前，先不要扩展这些内容：

- 不接 OpenAI API 或其他 LLM。
- 不加第二种故障。
- 不上 Kubernetes。
- 不做 Web UI。
- 不让 Agent 获得任意 shell 权限。
- 不把 evaluator 的判断逻辑复制进 Agent 当成“答案”。

原因很简单：V0 当前最重要的里程碑，是证明 Agent 能在受限权限下根据观测自动恢复唯一已知故障，而且结果能由独立 evaluator 验证。

---

## 本阶段完成定义（Definition of Done）

同时满足以下条件，Step 6-9 才算完成：

- `agent/state.py`、`agent/tools.py`、`agent/agent.py` 不再为空。
- Agent 只通过受限工具观察和修复环境。
- Redis 被停掉后，Agent 能自动启动它并恢复 API。
- 健康状态下运行 Agent 不产生不必要动作。
- 连续 3 轮端到端实验全部输出 `PASS`。
- 失败路径返回非零退出码，不误报成功。
- README 和施工日志与实际行为一致。

完成这些以后，下一阶段才是：把规则判断抽成可替换的策略接口，再接入 LLM，并比较“规则 Agent”和“LLM Agent”的恢复成功率、耗时、动作数与安全性。
