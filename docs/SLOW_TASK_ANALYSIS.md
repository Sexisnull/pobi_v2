# 任务执行缓慢根因分析

> 生成日期：2026-08-16
> 任务 Session：`4e68baab-1cc9-430d-b475-15674f183242`
> 任务 Agent：`02c1a686-ca84-474e-bb8c-70a1d90eb5a3`
> 目标：`122.51.72.186:8081`（DVWA v1.10）
> 任务目标：找一个 SQL 注入漏洞
> 实际耗时：约 **33.2 分钟**（`metrics.json` 记录 `duration_seconds: 1992`）

## 1. 结论

任务慢**不是因为 SQL 注入扫描慢**，而是 agent 在**登录 DVWA 这一步卡死、反复重试约 30+ 分钟**，最终**从未进入真正的漏洞测试阶段**。

证据：
- `metrics.json` 的 `agent_calls` 中**没有任何 `exploit_web_agent` / SQL 测试类调用**，说明 exploit 阶段根本没启动。
- `authenticator` 被调用 4 次，`requester` 仅 6 次 —— 绝大部分时间消耗在 LLM 反复推理/重试登录，而非实打目标。
- 上下文（`context.txt`）里 agent 自己诊断出登录失败的根因（见第 3 节）。

## 2. 耗时量化（`metrics.json`）

| 指标 | 值 |
|---|---|
| 总时长 | 1992 秒 ≈ 33.2 分钟 |
| tool_calls 总数 | 123 |
| input tokens | 1,550,127（约 155 万，上下文被反复灌入） |
| output tokens | 132,880 |
| agent_calls | requester:6, authenticator:4, shell:5, memory:4, python_interpreter:3, webapp_analyzer:4, supervisor:1, reporter:1 |

**关键缺失**：`exploit_web_agent` / SQL 注入测试相关 agent 调用次数为 **0**。

## 3. 卡顿根因（来自 agent 自身产出的上下文 `context.txt`）

agent 在 `run_context/context.txt` 中自己写明了失败原因，三处原文摘录：

### 3.1 function-call 无法序列化嵌套 `steps` 参数（核心阻塞）

> `unable` 片段（context.txt @117456）：
> "the platform's function-call interface appears **unable to properly serialize deeply-nested JSON arrays of objects** for complex tool parameters like `steps`. Each JSON string fragment is treated as individual characters during Pydantic model validation instead of structured dict items. This prevented proper execution of the requested HTML form-login..."

含义：浏览器登录步骤（`browser_run_steps` 的 `steps` 参数）是嵌套对象数组，但框架的 function-call 接口把它当成字符序列传给 Pydantic 校验，导致表单登录步骤**无法正确构造/执行**。

### 3.2 JSON 流程无法发出登录 POST（无流量打到主机）

> `cannot` 片段（context.txt @44225）：
> "the json flow grows native support for emitting x-www-form-urlencoded bodies independent of caller-supplied headers. **Outcome: No authenticated session exists** ... The probe never left agent-side processing, so **no traffic was generated toward the host**."

含义：agent 尝试的另一种登录路径（JSON flow / Basic）连 HTTP 请求都没真正发出去。

### 3.3 多次认证尝试全部失败

> `Attempt B` 片段（context.txt @45500）：
> ```json
> { "success": false, "auth_context_saved": false,
>   "target_slug": "122.51.72.186_8081", "profile": "target_session",
>   "final_url": "http://122.51.72.186:8081/login.php" }
> ```

含义：登录 POST 后最终仍停在 `login.php`（被弹回），`success=false`。

### 3.4 根因归并（与 AUTHENTICATOR_DVWA_FAILURE_ANALYSIS.md 一致）

DVWA 登录需要随每次请求轮换的 CSRF token `user_token`（context.txt @2603 已识别："`user_token`: hidden CSRF token that rotates every request load"）。但：

1. 框架 `BrowserStep` 不支持"先抓页面隐藏 token 再回填"（见前一份报告）；
2. 同时 function-call 序列化缺陷（3.1）让即使写对 steps 也无法执行；
3. 提示词又引导 agent 误选 Basic/JSON 流程（3.2 失败）。

三重叠加 → agent 在登录环节死循环重试，耗尽 33 分钟。

## 4. 失败链路

```
任务下发(找 SQLi)
  ↓
webapp_analyzer 侦察 → 识别 DVWA + 需要 user_token 登录
  ↓
authenticator 尝试登录 (4 次):
   ├─ Basic/JSON flow → function-call 序列化失败 / 请求未发出 (3.1, 3.2)
   └─ form flow       → steps 嵌套对象无法序列化, 且无法抓 user_token
  ↓
每次登录 final_url 都是 login.php, success=false (3.3)
  ↓
agent 反复推理重试, 33 分钟内 input token 累计 155 万
  ↓
exploit/SQLi 阶段从未启动 (agent_calls 无 exploit_web_agent)
  ↓
任务在"登录"这一步空转半小时, 未产出任何漏洞结果
```

## 5. 修复建议

1. **修复 function-call 嵌套参数序列化**（最紧急）：`steps` 这类嵌套对象数组在传给 Pydantic 前被当字符序列处理。需排查 tool 调用封装层对 `list[dict]` / 嵌套 JSON 的序列化逻辑（涉及 `pobi_v2` 的 tool 调用/agent 框架层）。
2. **`BrowserStep` 增加 extract/fill_from_page 类型**：支持抓 `user_token` 再回填（详见 `AUTHENTICATOR_DVWA_FAILURE_ANALYSIS.md`）。
3. **登录重试加熔断**：`authenticator` 连续 N 次 `success=false` 应停止重试并上报，避免 30+ 分钟空转（浪费约 155 万 input token）。
4. **提示词纠正**：明确 DVWA 等有 CSRF 的表单登录必须走支持 token 抓取的浏览器流程，禁止误选 Basic。

## 6. 涉及证据文件

| 文件 | 用途 |
|---|---|
| `~/.pobi_v2/cache/metrics/4e68baab-.../metrics.json` | 时长/调用次数/ token 统计 |
| `~/.pobi_v2/agents/02c1a686-.../4e68baab-.../run_context/context.txt` | agent 自述诊断（215 处 login、68 处 user_token、20 处 attempt） |
| `~/.pobi_v2/cache/logs/.../python_interpreter.jsonl` | 反复 GET 登录页（10 个 user_token 值 = 10 次拉登录页） |
| `~/.pobi_v2/cache/logs/.../requester.jsonl` | 仅 6 次网络请求，印证极少实打目标 |

## 7. 停止机制缺口分析（本次为何没被自动终止）

用户提出：能否让 LLM 自主判读"无法完成任务"并主动结束？

经代码核查，**框架已具备约 80% 的停止骨架，但存在两个语义缺口，导致本次登录空转未被拦截**。

### 7.1 框架已有的停止机制（已存在，未生效）

| 机制 | 位置 | 作用 |
|---|---|---|
| `MAX_TASK_ATTEMPTS = 3` | `pobi_agent/agents/architecture.py:59` | 同一任务最多执行 3 次，超限标 `failed:max_attempts` |
| `max_depth` 守卫 | `pobi_agent/agents/architecture.py:198` | 递归过深 → `aborted:max_depth` |
| `_policy` 的 `fail` 态 | `pobi_agent/agents/architecture.py:487-500` | `confidence < 0.20` → 判失败退出 |
| 子 agent 失败聚合 | `pobi_agent/agents/architecture.py:344-348` | supervisor 收到低置信度 → 标 `failed` |
| 提示词停止规则 | `pobi_prompts/_shared/_stopping_conditions.jinja2` | "重复失败 3 次""无实质进展 10 次"即停 |

### 7.2 为何本次未触发

1. **LLM 把"换方法重试登录"当成不同任务**：每次都重新派发 authenticator，`attempts` 计数没累计到 3（每次都是"新思路"），`MAX_TASK_ATTEMPTS` 形同虚设。
2. **登录失败被判成中置信度"再换种方法"**：没掉到 `< 0.20` 的 `fail` 阈值，`_policy` 的 `fail` 分支不触发。
3. **最关键缺口——缺少"结构性受阻"停止类别**：`_stopping_conditions.jinja2` 与 `_policy` 只有"目标防御 / 资源限制 / 重复失败"三类，没有"**框架/工具能力缺陷导致无法前进**"（如 function-call 序列化失败、CSRF 无法抓取）。这类不是目标防御、也不是方法错，而是**根本不具备完成条件**。LLM 缺这个判读口径，于是在"方法层面"空转 33 分钟。

> 补充验证：`authenticate.py` 工具内部**无任何循环/重试逻辑**（0 个 `retry`/`while`），`authenticator_agent.py` 也只跑一次。反复登录不是工具自己循环，而是 **LLM 在 supervisor 多轮调度下不断重新派发 authenticator 重试**——这正说明停止责任应在 supervisor 决策层与工具熔断层，而非子 agent 内部。

## 8. 解决方案（推荐组合：提示词 + 代码双层）

让 LLM 能"自主判读无法完成并终止"，需补两类机制：

### 8.1 机制 A：提示词层新增"结构性受阻"停止类别（最小改动，立即可用）

在 `pobi_prompts/_shared/_stopping_conditions.jinja2` 的错误条件中增加一条，使 LLM 能显式终止：

```jinja
### 结构性受阻（停止并终止任务）
❌ **能力性死路**：同一前置步骤连续失败，且失败根因属于框架/工具能力缺陷而非目标防御——
   例如：工具参数无法被序列化执行（function-call 嵌套对象被截断）、需动态 CSRF token
   但无抓取手段、认证流程反复 `success=false` 且 `final_url` 始终停在登录页。
   → 判定为"当前框架无法完成"，立即停止，返回 status: "aborted"，
     并在 detailed_summary 中写明受阻根因，交人工介入，不要无限重试。
```

同时给 `SupervisorOutput` 增加 `aborted: bool` 字段（或在 `task_achieved` 之外增加 `outcome: achieved|failed|aborted`），让 supervisor 能显式输出"我判定无法完成"，而非只能用中置信度的 `refine` 续命。

### 8.2 机制 B：代码层给 authenticator 加"连续失败熔断"（强制兜底，不依赖 LLM 自觉）

在 `authenticate_service`（`pobi_agent/tools/browser/authenticate.py`）中增加基于 `<agent_id>/<session_id>/<profile>` 的调用级计数器。同一 profile 连续认证失败达到阈值（建议 3 次）时，**工具直接返回结构化失败并拒绝再试**，不把球踢回 LLM 无限重试：

```python
# 伪代码（AuthenticatorAgent 熔断）
if handler.consecutive_failures(profile) >= AUTH_FAIL_HARD_LIMIT:  # 建议 = 3
    return AuthResult(
        success=False,
        aborted=True,
        reason="认证连续失败达上限，疑似框架不支持该登录形态（如 CSRF/序列化），停止重试",
    )
```

计数状态建议存于 `AuthContextHandler`（`pobi_agent/auth_resolver/auth_resolver.py`），与 `auth_context/` 同目录维护一个 `fail_count.json`，天然随 session 隔离、可跨请求累计。

### 8.3 落地优先级与预期效果

| 优先级 | 改动 | 位置 | 效果 |
|---|---|---|---|
| 1 | 机制 B 熔断 | `authenticate.py` + `auth_resolver.py` | 第 3 次认证失败即硬停，杜绝空转，最稳 |
| 2 | 机制 A 提示词 + `SupervisorOutput.aborted` | `_stopping_conditions.jinja2` + `supervisor_agent.py` | "结构性受阻"成为一等停止类别，覆盖广义框架能力缺口 |

**预期**：本次"登录卡死 33 分钟"会被第 3 次认证失败即终止，总耗时从 33 分钟降至约 2–3 分钟，并产出明确的 `aborted: 认证能力性受阻` 结论供人工接手，避免再烧约 155 万 input token。

### 8.4 关联修复（治本，不在本机制范围但需跟进）

- 修复 function-call 嵌套 `steps` 参数序列化（`pobi_v2` tool 调用封装层）；
- `BrowserStep` 增加 `extract`/`fill_from_page` 类型以支持抓 `user_token`（详见 `AUTHENTICATOR_DVWA_FAILURE_ANALYSIS.md`）；
- 提示词明确 DVWA 等有 CSRF 的表单登录必须走支持 token 抓取的浏览器流程，禁止误选 Basic。

> 注：机制 A/B 解决"卡死不自知、不停"，关联修复解决"为什么登不进"。两者互补：即便治本修复完成，熔断与结构性受阻判读仍是防止未来未知形态死循环的必备安全网。
