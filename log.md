首先创建文件结构

然后写入main.py

发现缺少对应的库，下载解决

python -m pip install fastapi redis uvicorn

---

2026-09-18 (用 Claude Code 完成 Step 2-5)

1. Docker Hub 被墙 → 配了 4 个镜像站到 Docker Desktop (daocloud / 1ms.run / xuanyuan.me / 1panel.live)
2. 写好 compose.yaml、Dockerfile、requirements.txt,环境跑起来了
3. 写好 scenarios/redis_down.yaml、scripts/inject_fault.py、scripts/reset_env.py、evaluator/evaluate.py
4. 踩坑:redis-py 8.x 默认重试 10 次(指数退避),Redis 挂掉后请求卡 43 秒
   修复:socket_timeout=1 + health_check_interval=1 + Retry(NoBackoff(), 0)
   修复后降级响应 ~3.5s(DNS 查已停容器占 ~4s,属环境特性)
5. 完成标准达成:一条命令注入故障 → 一条命令恢复 → 独立程序输出 PASS,复验 2 次 100% 可重复
6. agent/ 按计划留空,下次做 Step 6-7(tools + 无 LLM 的简单循环)

---

2026-09-18 Step 6:Agent 受限工具层

1. 写好了 agent/state.py:4 个 dataclass(ApiHealth / ServiceStatus / ActionResult / LogsResult)
2. 写好了 agent/tools.py:5 个工具(get_api_health / get_service_status / get_service_logs /
   restart_service / verify_api)
   - 所有 docker 命令 subprocess.run 列表形式 + cwd=项目根目录(从 __file__ 推导),不用 shell=True
   - restart 白名单 {"redis"},拒绝其它服务且不执行
   - API 超时 6 秒(> 降级时 DNS 4 秒),docker 命令 10~30 秒超时
   - 全部失败转结构化返回值,不抛异常
3. 踩坑:Windows 上 subprocess(text=True) 默认 GBK 解码,Docker 输出 UTF-8 → UnicodeDecodeError
   修复:encoding="utf-8", errors="replace"(tools.py 里三处)
4. 测试全过:健康态 / 故障态 / 权限拒绝(api 确实没被重启)/ verify_api False→True
5. 下一步 Step 7:agent.py 主循环(MAX_STEPS,先不接 LLM)

---

2026-09-18 Step 7:无 LLM 的确定性 Agent 循环

1. 写好了 agent/agent.py:观察→诊断→行动→验证 闭环,四条路径全测过:
   - 健康态 → ALREADY_HEALTHY(退出码 0)
   - redis 挂 → 看日志拿到证据("User requested shutdown")→ restart → RECOVERED(退出码 0)
   - api 挂 redis 运行 → UNRESOLVED,不瞎动(退出码 1)
   - 全挂 → 修复 redis 但验证 5 次仍败 → FAILED(退出码 1)
2. 遵守约束:只修一次、验证最多 5 次间隔 1s、只用受限工具、不调 reset_env/evaluator
3. 踩坑:python agent/agent.py 运行时,agent/ 目录进了搜索路径,agent/agent.py
   遮蔽了 agent 包 → ImportError("cannot import name 'tools' from 'agent'")
   修复:加 agent/__init__.py(正式包优先于同名脚本文件)
4. Agent 和裁判独立性验证:Agent 自己恢复后,evaluator 独立输出 PASS
5. 下一步:把 run_agent 里的"决策规则"替换成 LLM 判断(Step 8)

---

2026-09-18 代码审查修复(两处)

1. tools.py 异常盲区:get_service_status 没捕获 FileNotFoundError / TimeoutExpired,
   get_service_logs 漏捕 FileNotFoundError —— docker 不存在或命令超时会直接崩,
   而不是按设计走退出码 2。已补上,并用 mock 模拟两种故障做了回归测试,
   验证 Agent 正确返回退出码 2。
2. README.md 补充了 Agent 自动恢复用法、预期输出和退出码含义表。

---

2026-09-18 Step 8-9:端到端验收 + 测试与文档

Step 8 验收结果:
1. 标准成功路径 PASS → FAIL → RECOVERED → PASS,与预期一致
2. 幂等性:健康时跑 Agent → ALREADY_HEALTHY(退出码 0),不重启任何服务
3. 可重复性 3 轮:5.9s / 6.0s / 6.0s,全部 RECOVERED + PASS,平均恢复耗时 ≈ 6.0s
   (耗时构成:约 4s 是降级 /health 的 DNS 确认 + 约 1s 状态/日志 + 约 1s 重启/验证)
   注:第一轮循环曾出现一次"Agent 退出码 1 但 evaluator PASS"的异常,输出被静音未能定位,
   复测 3/3 干净;若再出现需保留 Agent 完整输出追查
4. 失败路径:API 不可访问 → UNRESOLVED(退出码 1,不崩不谎报);白名单外服务 → 拒绝且不执行;
   Docker 未启动 / compose 超时 / 非 JSON 响应由单元测试 mock 覆盖

Step 9 交付:
1. tests/test_tools.py(23 个)+ tests/test_agent.py(6 个)= 29 个单元测试全过,
   HTTP 和 subprocess 全部 mock 隔离;验证了退出码、只修一次、权限拒绝不执行等约束
2. scripts/run_e2e.py:环境启动→基线→注入→Agent→判定 一键验收,退出码 0/1
3. README.md 已有"Agent 自动恢复"章节(上一轮完成)
4. 踩坑:e2e 脚本捕获子进程输出时再次撞编码 —— 子进程向管道默认写 GBK,
   解码按 UTF-8 → 产生 � → 打印时 GBK 编不出 → UnicodeEncodeError
   修复:子进程 env 设 PYTHONIOENCODING=utf-8 + 解码端 utf-8 + stdout reconfigure(errors="replace")
   教训:Windows 上任何"捕获子进程输出"的代码,两端编码必须显式对齐

当前状态:RecoverBench V0 全链路(环境/故障/工具/Agent/裁判/测试)可用,可接 LLM(Step 10)。

---

2026-09-18 V1 重构:统一工具协议 + 策略/循环分离

Step 2(工具层):
1. restart_service 改名 start_service(实际动作就是 compose start,名副其实)
2. 暴露 4 个工具:get_api_health / get_service_status / get_service_logs / start_service,
   verify_api 保留为内部辅助函数,不进注册表
3. 返回协议统一为可序列化 dict {"ok","data","error"},ok=True 只表示查询成功
4. 补齐:非对象 JSON / 字段缺失检测、日志 tail 1~100 + 2000 字符上限、
   全部命令超时、服务名白名单校验(校验失败绝不执行)、
   未知 docker 状态(restarting 等)running=None,不当作"已停止"
5. 单元测试 31 个覆盖上述全部

Step 3(策略/循环分离):
1. agent.py → 纯命令行入口;loop.py → 公共循环(决策→执行→记录→步数预算→异常兜底)
2. state.py → Decision(ToolCall | Finish)+ HistoryEntry(可序列化、可回放)
3. policies/rule.py → RulePolicy,决策完全从 history 推导,无内部状态;
   policies/llm.py → 占位。以后比较两种策略时,工具执行/日志/预算共用同一套代码
4. 决策只有 ToolCall(含 wait_seconds 小扩展)与 Finish(结论+证据)
5. RulePolicy 复现 V0 全部行为:健康即止 / exited 才启动 / running 报 unresolved /
   未知状态不贸然动手 / 修复后最多验证 5 次
6. 验收:51 个单元测试全过;e2e 脚本全过;真实环境 ALREADY_HEALTHY / RECOVERED /
   UNRESOLVED / FAILED 四条路径退出码全对
7. 设计决定:RulePolicy 不再调用 get_service_logs(V0 的日志步骤),证据来自状态观察;
   日志工具保留在注册表供未来 LLM 策略使用

---

2026-09-18 Step 4-5:工具分发器 + 预算控制 + 完整轨迹记录

Step 4(分发与预算):
1. 新增 agent/dispatch.py:四道检查(工具存在 → 参数类型/范围 → 服务白名单 → 预算),
   参数表 ARG_SPECS 集中声明;未登记参数表的工具由测试守着不让进注册表
2. 新增 agent/budget.py:工具 10 次 / 修改 2 次 / 整轮 120 秒(monotonic)/ 模型超时 30 秒 /
   模型重试 1 次
3. 关键设计:
   - 先扣决策预算再做其余检查 —— 非法调用也消耗预算,失控策略必被预算终止
   - 工具超时 = min(工具默认值, 剩余时间),模型的超时同理,阻塞调用无法突破总预算
   - 被拒绝的修改不消耗"环境修改"预算(没动环境就不算),但决策预算照扣
   - 工具新增可选 timeout 参数;不接受该参数的自定义工具不注入,保持注册表通用
4. 真实演示:失控策略 3 次即停、时间预算耗尽 0 次调用即停、
   修改预算 1 次后第 2 次启动请求被拦、非法服务名连命令都不执行

Step 5(轨迹记录):
1. 新增 agent/trace.py:runs/<run_id>/{config.json, trace.jsonl, result.json}
   - config:代码版本(git 短 hash)、策略、模型、prompt 版本、预算
   - trace:每次决策 / 工具参数 / 结果 / 耗时 / 模型用量(一行一事件)
   - result:结论、退出码、耗时、步骤数、预算用量、evaluator 槽位
2. 凭据保护:键名含 api_key/token/secret/password/authorization 的字段写盘前替换为 ***
3. 裁判独立:Agent 不调 evaluator;判定由 evaluator 独立执行后经
   `--attach runs/<id>` 写入 result.json
4. 新增 scripts/inspect_run.py:只看运行目录即可还原全过程
5. 循环行为变更:未注册工具不再致命,而是记为失败结果、策略可继续(预算照扣),
   更贴近模型场景;明确传 max_steps 时才用"超过最大步数"结束
6. 验收:91 个单元测试全过;e2e 新增"轨迹完整"步骤(三文件齐全 + jsonl 可解析)全过;
   退出码新增 budget_exceeded → 2

已知待办:llm.py 仍是占位;接模型时用 budget.model_timeout() 约束单次调用、
用 budget.consume_model_retry() 限重试、用 recorder.log_model_call() 记用量。

---

2026-09-18 Step 6:接入 LLMPolicy(DeepSeek,OpenAI 兼容)

实现:
1. agent/config.py:环境变量优先 → .env 兜底 → 默认值;支持 LLM_API_KEY/DEEPSEEK_API_KEY
2. agent/llm_client.py:只负责请求/响应/用量/接口错误分类(超时、连接失败、429/5xx 可重试,
   401/400 不可重试);key 只在请求头,不进日志;错误体截断 200 字符
3. agent/policies/llm.py:构造上下文 → 请求模型 → 解析工具调用或 finish 结论
   - 工具 schema 由 dispatch.ARG_SPECS 自动生成(单一事实来源,不会与校验逻辑脱节)
   - finish 是伪工具,由策略拦截成 Finish 决策,永不进分发器
   - 参数 JSON 解析失败 → 交给分发器拒绝,错误信息带原文让模型自我修正
   - 模型只回文本 → 提醒一次;仍不回工具 → agent_error(明确终止原因)
   - 可重试失败重试 1 次(计入预算);不可重试直接 agent_error
4. agent/agent.py 加 --policy rule|llm 和 --task;缺 key 时明确报错退出码 2
5. scripts/run_llm_dryrun.py:用脚本化假模型跑真实环境,不花钱即可验证整条链路

真实联调结果(用环境变量里的 DEEPSEEK_API_KEY):
- 正常环境:模型自主调 5 个工具(健康→api状态→redis状态→api日志→redis日志)→ healthy,零修改
- Redis 停止:模型调 8 步(观测→诊断→读日志发现 SIGTERM→start_service→验证→再确认状态)→
  RECOVERED,裁判独立判定 PASS;8 次模型调用约 1.4 万 token

踩坑(重要):
1. 脱敏器误杀用量字段:键名含 "token" 的 prompt_tokens/completion_tokens/max_tokens
   被替换成 ***,毁掉 Step 5 的模型用量记录。
   修复:改为精确命中 + 单数后缀匹配(_token 而非 _tokens),并加回归测试
2. 模型调用事件的 step 记成了消息条数,已改为循环步数
3. 安全事件:用户把真实 API key 粘进了 .env.example(会提交的文件)。
   已确认它从未被提交/推送,并把 key 迁移进 .env(gitignore 覆盖),.env.example 恢复占位符

验收(完成标准):
- 正常环境模型检查后结束、无修改动作 ✅
- Redis 停止后模型自主选工具并恢复 ✅
- 模型请求失败/非法调用/预算耗尽都有明确终止原因 ✅(单元测试覆盖)
- 成功与否由裁判决定 ✅(result.json 同时记录 Agent 结论与裁判判定)
- 128 个单元测试全过

---

2026-09-18 模型名核定(重要)

1. 用 GET /models 直接问 API:这个 key 当前可用模型是 deepseek-flash 和 deepseek-v4-pro,
   并没有 deepseek-chat(它是已弃用的兼容别名,官方文档标注 2026-07-24 弃用)
2. base_url https://api.deepseek.com/v1 正确
3. 多轮工具调用实测(两个模型都过):
   - 第 1 轮返回 tool_calls,第 2 轮回传工具结果后仍正常
   - 两个模型都返回 reasoning_content 字段(默认思考模式);文档警告"思考模式下
     涉及工具调用的回合必须回传 reasoning_content 否则 400" —— 实测我们的
     "从历史重建 assistant 消息(不带该字段)"方式不会触发 400,两个模型都返回 200
4. 已更新:.env(deepseek-flash)、.env.example、agent/config.py 默认值
5. check_llm.py 升级为两轮体检:第 2 轮完全复刻策略的消息重建方式,
   把"配置不对"和"多轮链路不对"分开诊断
6. 真实联调(deepseek-flash + Redis 停止):7 次模型调用、6 次工具、1 次修改,
   13.2 秒恢复,裁判 PASS;模型同样从日志里读出了 SIGTERM 证据

经验:模型名别信记忆和二手文档,直接 GET /models 问 API;换模型只需要改一行 .env。

---

2026-09-18 Step 7-8:结构化裁判 + 统一实验 Runner

前置验证:
- Redis 停止再启动后预置值保留?→ 实测保留(RDB 快照),所以"验证业务读取"这个裁判标准可行

Step 7(裁判):
1. evaluate.py 重写:判定 = /health + /cache/{key} 连续通过 3 次(间隔 1s,截止时间内)
2. 三态 PASS/FAIL/ERROR + 退出码 0/1/2;ERROR = 裁判输入不完整或自身异常
3. probe() 与 evaluate() 分离:runner 用 probe 做基线/场景验证,用 evaluate 出最终判定
4. --key 省略时为"只做健康检查"的便捷模式,并在输出里明确提示
5. attach 改为可写入完整结构化结果(dict)
6. 抓到并修复一个设计 bug:循环上限原本设成 attempts(3 次),导致"中途抖动一次就永远
   不可能连续 3 次通过"。改为由截止时间兜底 + max_checks 兜底(防无限轮询)
7. run_e2e.py 同步:FAIL 的期望退出码从 0 改为 1,并加入预置数据步骤

Step 8(runner):
1. scripts/run_benchmark.py:一条命令重复实验;--policy/--scenario/--repeat
2. 每轮严格:重置基线→就绪→预置数据→验证基线→注入→验证场景→Agent→裁判→保存→清理
3. 关键约束都落实:每轮独立重置;基线/注入失败不启动 Agent(记 setup_failed);
   清理在 finally 且失败即中止整个实验;runner 能调重置命令而 Agent 不能;
   Agent 自述与裁判结果分别保存;准备/Agent/裁判/清理耗时分别记录;顺序执行
4. 产物:runs/<bench_id>/r<N>/{config,trace,result}.json + rounds.jsonl + summary.json
5. 场景数据化:scenarios/healthy.yaml 新增;redis_down.yaml 真正被 runner 读取使用
6. agent.py 加 --run-id / --runs-root,让 runner 控制轨迹归档

实测结果:
- 裁判四态:健康+值对→PASS(0);值不对→FAIL(1);redis 停止→FAIL(1);缺期望值→ERROR(2)
- rule × redis_down × 3 轮:3/3 PASS,平均 准备 5.1s / Agent 5.3s / 裁判 2.1s
- llm × redis_down × 2 轮:2/2 PASS,平均 Agent 13.4s
- llm × healthy × 2 轮:2/2 PASS(自述 ALREADY_HEALTHY,零修改)
- 谎报检测:一个不做任何事就宣称 recovered 的假模型 → Agent 自述 RECOVERED(退出码 0),
  裁判 FAIL,两者分别记录 ✅ 完成标准达成
- 144 个单元测试全过

---

2026-09-18 Step 9:边界测试 + 20 轮对照实验 + V1 验收

1. 新增 tests/test_edge_cases.py(23 个),覆盖规格列出的全部边界:
   未知工具/非法参数不执行真实动作(用真注册表 + mock subprocess,断言 0 次调用)、
   反复请求被预算终止、模型超时/401 明确报错且轨迹保留错误原因、
   模型宣称成功但裁判独立判 FAIL、runner 四种 setup_failed 都不启动 Agent、
   正常场景零修改、RulePolicy 完整 V0 序列回归
   踩坑:假工具函数必须接受 **kwargs —— 分发器会注入 timeout
2. 新增 tests/test_report.py(7 个):验证"无效实验不进通过率分母"、
   错报成功识别、平均值的缺失值处理
3. trace.summarize_run():从运行目录汇总工具数/修改数/模型调用/token
4. scripts/report_benchmark.py:多 benchmark 汇总成对照表 + 列出无效实验/错报成功/失败原因
5. runner 踩坑(两处,都是自己代码的锅):
   - 改用量统计时删掉了 claim 变量,打印语句还在引用 → NameError
   - 异常轮次记录没有 started_at 字段,汇总时 KeyError —— 异常处理路径自己抛异常
   教训:异常路径的字段完整性同样要保证,改代码后必须真跑一遍
6. 20 轮对照实验(2 场景 × 2 策略 × 5 轮,顺序执行,全部有效):
   rule/healthy 100% 355ms 1.0 工具 0 修改 0 token
   llm/healthy  100% 7722ms 4.4 工具 0 修改 8354 token
   rule/redis_down 100% 5321ms 4.0 工具 1 修改 0 token
   llm/redis_down  100% 12394ms 6.0 工具 1 修改 12055 token
   无效实验 0、错报成功 0、失败 0
7. 报告:docs/V1对照报告.md(含设置、结果表、解读、完成标准核对、已知局限)
   关键观察:两个策略都 100%,说明场景太容易,这份报告只能证明"LLM 能用",
   不能证明"LLM 比规则强";要区分策略需增加候选根因/干扰项
8. 175 个单元测试全过
