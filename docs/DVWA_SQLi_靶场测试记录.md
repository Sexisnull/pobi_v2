# DVWA SQLi 靶场测试记录

> 记录对 `http://122.51.72.186:8081` 的授权渗透测试过程、环境配置与流程改动。
> 创建时间：2026-08-11

## 1. 靶场与目标

| 项 | 值 |
|----|----|
| 靶场地址 | `http://122.51.72.186:8081` |
| 应用 | DVWA v1.10 |
| 安全级别 | **medium** |
| 登录凭据 | `admin` / `password` |
| 测试范围 | 仅 `http://122.51.72.186:8081/vulnerabilities/sqli/` |
| 漏洞类型 | SQL 注入（SQLi） |

> 用户已确认该靶场配置下此端点**确实存在 SQL 注入漏洞**，测试目的是用项目 agent 流程复现并产出确定性证据与报告。

## 2. 流程与配置改动（本轮）

### 2.1 沙箱镜像切到 Kali（关键）
- 原 fork 版默认 `ubuntu:latest`，缺失 sqlmap 等工具链。
- 参考项目 `straylabs-ai/deadend-cli` 的 `environments/images/` 含 `kalilinux.Dockerfile` 与 `webapp_sec.Dockerfile`，证实原始设计即用 Kali 沙箱执行命令。
- 改动：
  - `pobi_v2/core/config.py`：新增 `sandbox_image = "xoxruns/sandboxed_kali:latest"`（环境变量 `POBI_V2_SANDBOX_IMAGE` 可覆盖）。
  - `pobi_agent/sandbox/sandbox_manager.py`：`create_sandbox(image=None)` 不再硬编码 ubuntu，`image` 为 None 时回退读 `settings.sandbox_image`。
  - `.env` 第 21 行：`POBI_V2_SANDBOX_IMAGE=ubuntu:latest` → `xoxruns/sandboxed_kali:latest`（**这才是上次任务实际用 ubuntu 的根因**）。
- 验证：配置读取正确；`docker run --rm xoxruns/sandboxed_kali:latest which sqlmap` 确认 `/usr/bin/sqlmap` 存在。

### 2.2 威胁建模阶段强制登录（关键）
- 原流程：威胁建模 prompt 只要求"识别认证需求"，从不实际登录 → 后续探测拿匿名会话，DVWA 一直 302 重定向到 login.php，验证失败。
- 修复方向：复用项目内置的 `AuthContext` 机制（authenticator 登录后持久化 profile，下游工具传 `auth_profile` 自动加载 cookie），**无需手写 cookie 透传**。
- 改动：
  - `pobi_agent/pobi_agent.py`：`threat_model` 与 `threat_model_stream` 两个入口 prompt 新增段落——发现端点需认证时必须在威胁建模阶段调用 `authenticate` 建立 `auth_profile`，并记录 profile 名供后续复用。
  - `pobi_prompts/supervisor.instructions.jinja2`：新增 `## AUTHENTICATION & SESSION REUSE (MANDATORY)` 硬规则——后续所有请求必须带 `auth_profile`；shell 工具（sqlmap/curl）从 AuthContext 取 cookie 显式传入；已建会话不重复登录。

### 2.3 待完成（sqlmap 工具封装）
- 当前 supervisor 指令虽把 `sqlmap` 列为可用工具，但**无封装、无路由保证沙箱里有该二进制**（现已通过 Kali 镜像解决二进制问题）。
- 仍未实现：独立的 `sqlmap_tool` 封装 + supervisor 对 SQLi 类任务的优先路由 + ValidationGate 增加 `SqlmapStrategy`（判定 sqlmap 输出的确定性注入标志）。
- 这是下一步要做的事，使"扫到 SQLi → 调 sqlmap 确定性验证"端到端打通。

## 3. 通过 API 执行测试

### 3.1 环境与鉴权
- PG(5432) / Redis(6379) / API(8000) 均通过 docker-compose 运行且健康（`/health` 返回 ok）。
- API 需要 Bearer Token；开放注册开启，注册用户进 `pobi-range` 租户：
  - `email: pobi_sqli_test@demo.com` / `password: pobi-sqli-2026` / tenant: `pobi-range`
- 启动 arq worker（消费任务队列）：
  ```
  nohup .venv/bin/python -m arq pobi_v2.engine.worker.WorkerSettings > logs/worker_run.log 2>&1 &
  ```

### 3.2 创建的资源
- Target：`326560e1-b1d9-44a0-9228-97bb90cbd858`
  - name: DVWA SQLi 靶场 / url: `http://122.51.72.186:8081` / in_scope: 该 host
- Task：`f8fe66f8-ef3c-459e-8d92-bc320c868130`
  - name: DVWA SQLi 注入验证
  - objective: 登录 → 仅测 `/vulnerabilities/sqli/` → 发现注入点后用 kali 沙箱 sqlmap 确定性验证（含 --cookie）→ 输出证据与报告
  - status: queued（已由 worker 接管执行）

### 3.3 任务执行观察（worker.log）
- agent 在威胁建模阶段访问 `http://122.51.72.186:8081/` 收到 302 跳转 `login.php`，识别出 DVWA v1.10、`security=low`（注：本次靶场为 medium，需确认 agent 后续实际读取到的安全级别）、登录字段 `username/password/user_token`、CSRF token 机制。

## 4. 已知问题 / 待解决
1. **sqlmap 工具未封装**：即便 Kali 镜像有 sqlmap，agent 当前仍靠 LLM 手搓 payload + Judge 判定，未走确定性 sqlmap 验证。需实现 2.3。
2. **安全级别 medium 适配**：DVWA medium 的 sqli 有 `mysqli_real_escape_string` + 输入框前端限制（但参数仍在 URL 中，盲注/union 仍可行）。需确认 agent 是否绕过前端限制用 URL 参数测试。
3. **事件可见性**：event_bus 默认 memory 后端跨进程不共享，SSE 实时流需切 redis 才能在 Web 端看到逐步思考；最终报告经 `GET /api/v1/report/{task_id}` 读取。

## 5. 复现命令（供后续）

```bash
# 1. 注册/登录拿 token
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"pobi_sqli_test@demo.com","password":"pobi-sqli-2026"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2. 查询任务状态/报告
curl -s http://127.0.0.1:8000/api/v1/tasks/f8fe66f8-ef3c-459e-8d92-bc320c868130 -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:8000/api/v1/report/f8fe66f8-ef3c-459e-8d92-bc320c868130/report -H "Authorization: Bearer $TOKEN"
```
