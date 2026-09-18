# RecoverBench

一个用来测试 AI Agent 故障恢复能力的 benchmark。V0 只做一种故障:**Redis 挂掉**。

## 架构

```
Client ──▶ FastAPI App ──▶ Redis
               ▲
               │ inspect / repair
          ┌────┴────┐
          │  Agent  │ (tools: health / logs / status / restart)
          └────┬────┘
               │
          ┌────┴─────┐
          │ Evaluator │ (PASS / FAIL)
          └──────────┘
```

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
[1] get_api_health → ApiHealth(ok=True, status='degraded', redis='unavailable', ...)
[2] get_service_status(redis) → ServiceStatus(service='redis', running=False, raw='exited', ...)
[2] get_service_logs(redis, 20) → ... User requested shutdown ...
[3] restart_service(redis) → ActionResult(ok=True, ...)
[验证 1/5] → status=healthy, redis=healthy
结论: RECOVERED

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
| 2 | Agent 自身工具调用出错(如 Docker 不可用) |

单独测试工具层:

```bash
python -c "from agent.tools import get_api_health, get_service_status; print(get_api_health()); print(get_service_status('redis'))"
```

## 升级路线

Redis Down → Redis Latency → API Crash → DB unavailable → CPU spike → Bad config → Prometheus metrics → multi-fault → Kubernetes → ITBench
