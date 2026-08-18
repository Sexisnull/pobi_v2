# PoBi v2 平台状态与接口能力文档

> 文档性质：现状说明（接口清单权威参考 + 状态快照）。
> 注意：§2 接口清单以源码为准，长期有效；§3/§4/§5 为状态快照，会随演进更新。
> 最近更新：2026-08-18（§3/§4/§5 复核，过时结论已收敛）。

---

## 1. 项目架构概览

PoBi v2 是一个**前后端分离的 AI 智能体驱动授权渗透测试平台**，面向安全靶场/授权站点的自动化侦察、漏洞利用与报告生成。

### 1.1 分层结构

```
浏览器/客户端
   └─ FastAPI 网关（pobi_v2/pobi_v2）
        ├─ routers/         # REST + SSE 接口层（11 个路由）
        ├─ engine/          # 任务编排：executor / event_bus / agent_adapter / memory
        ├─ db/              # SQLAlchemy 2.0 异步模型与持久化
        └─ services/        # 跨域服务（邮件、定价等）
   └─ pobi_agent（内核）    # 纯 Python 智能体内核（监督者/规划/工具调用）
        ├─ core_agent/      # 主智能体循环
        ├─ tools/           # 网络请求 pw_send_payload、浏览器、文件、沙箱执行等
        └─ agents/          # RequesterAgent、ExploitAgent 等专业子智能体
   外部依赖：
        ├─ PostgreSQL       # 主库（多租户隔离）
        ├─ Redis + ARQ      # 任务队列与后台 worker
        └─ Kali 沙箱容器     # 隔离命令执行环境（docker 运行）
```

### 1.2 里程碑进度（来自 `README.md` / `docs/PROJECT_GOAL.md`）

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 项目初始化（FastAPI + 多模块结构 + 配置） | 完成 |
| M2 | 数据模型（User/Target/Task/Finding/Audit 等） | 完成 |
| M3 | 认证与多租户（JWT、ApiToken、租户隔离） | 完成 |
| M4 | 任务执行引擎 + ARQ worker + 沙箱 | 完成 |
| M5 | 用户注册登录 / 目标管理 / 任务管理 API | 完成 |
| M6 | 持续会话 + 记忆（memory workspace） | 完成 |
| M7 | 安全护栏（自动批准 / 人工审批 / 危险动作拦截） | 完成 |
| M8 | 报告导出（HTML/Markdown/JSON）+ 使用量统计 | 完成 |

### 1.3 当前核心能力

- 多租户 SaaS 隔离、JWT 鉴权、API Token（PAT）。
- 基于内核 `pobi_agent` 的任务编排，监督者智能体驱动 RequesterAgent / ExploitAgent。
- 任务实时 SSE 流、事件落库（TaskEvent）、发现/产物落库（Finding/Artifact）。
- 人工审批网关、危险命令检查点、报告导出。

---

## 2. 全部接口能力清单

下列接口基于 `pobi_v2/pobi_v2/routers/` 源码逐项核查。鉴权列：`JWT` = 登录会话 Cookie/Bearer；`PAT` = API Token；`Admin` = 管理员权限；`公开` = 无需鉴权。

### 2.1 认证 `auth.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| POST | `/api/v1/auth/login` | 公开 | 跨租户 | 邮箱+密码登录，签发 JWT |
| GET | `/api/v1/auth/me` | JWT | 是 | 当前用户信息 |
| GET | `/api/v1/auth/tenants` | 公开 | 跨租户 | 租户列表（注意：`register` 已被注释，创建租户走其他路径） |

### 2.2 目标管理 `targets.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/targets/` | JWT | 是 | 列出当前租户目标 |
| POST | `/api/v1/targets/` | JWT | 是 | 创建目标（URL/范围） |
| GET | `/api/v1/targets/{target_id}` | JWT | 是 | 目标详情 |
| PUT | `/api/v1/targets/{target_id}` | JWT | 是 | 更新目标 |
| DELETE | `/api/v1/targets/{target_id}` | JWT | 是 | 删除目标 |

### 2.3 任务管理 `tasks.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/tasks/` | JWT | 是 | 任务列表 |
| POST | `/api/v1/tasks/` | JWT | 是 | 创建任务 |
| GET | `/api/v1/tasks/{task_id}` | JWT | 是 | 任务详情 |
| POST | `/api/v1/tasks/{task_id}/enqueue` | JWT | 是 | 入队执行 |
| POST | `/api/v1/tasks/{task_id}/cancel` | JWT | 是 | 协作式取消 |
| POST | `/api/v1/tasks/{task_id}/delete` | JWT | 是 | 删除任务 |
| GET | `/api/v1/tasks/{task_id}/plan` | JWT | 是 | 获取任务规划 |
| GET | `/api/v1/tasks/{task_id}/live` | JWT | 是 | 实时状态快照 |
| GET | `/api/v1/tasks/{task_id}/events` | JWT | 是 | 任务事件流（历史） |
| GET | `/api/v1/tasks/{task_id}/usage` | JWT | 是 | 该任务令牌用量 |
| GET | `/api/v1/tasks/usage/summary` | JWT | 是 | 租户用量汇总 |

### 2.4 指令注入 `instruction.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| POST | `/api/v1/tasks/{task_id}/instruction` | JWT | 是 | 向运行中任务追加指令 |

### 2.5 实时流 `stream.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/tasks/{task_id}/stream` | JWT | 是 | SSE 实时事件流 |

### 2.6 持久化查询 `persistence.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/persistence/tasks/{task_id}` | JWT | 是 | 任务完整详情 |
| GET | `/api/v1/persistence/tasks/{task_id}/events` | JWT | 是 | 事件明细 |
| GET | `/api/v1/persistence/tasks/{task_id}/findings` | JWT | 是 | 发现项 |
| GET | `/api/v1/persistence/tasks/{task_id}/artifacts` | JWT | 是 | 产物文件 |
| GET | `/api/v1/persistence/tasks/{task_id}/audit` | JWT | 是 | 审计事件 |

### 2.7 审批网关 `approval.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/approval/requests` | JWT | 是 | 待审批列表 |
| GET | `/api/v1/approval/requests/{request_id}` | JWT | 是 | 审批详情 |
| POST | `/api/v1/approval/requests/{request_id}/decision` | JWT | 是 | 通过/拒绝决策 |

### 2.8 报告导出 `report.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/report/{task_id}` | JWT | 是 | HTML 报告 |
| GET | `/api/v1/report/{task_id}/markdown` | JWT | 是 | Markdown 报告 |
| GET | `/api/v1/report/{task_id}/json` | JWT | 是 | JSON 结构化报告 |

### 2.9 系统运维 `system.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/system/worker-status` | Admin | 否 | Worker 健康 |
| POST | `/api/v1/system/task-reconcile` | Admin | 否 | 任务对账（清理幽灵任务） |
| GET | `/api/v1/system/kali-status` | Admin | 否 | 沙箱状态 |
| GET | `/api/v1/system/llm-status` | Admin | 否 | LLM 连通性 |
| GET | `/api/v1/system/probe` | 公开 | 否 | 健康检查 |

### 2.10 定价 `pricing.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/pricing/config` | JWT | 是 | 读取令牌定价 |
| PUT | `/api/v1/pricing/config` | Admin | 否 | 更新定价 |

### 2.11 API Token `api_tokens.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| POST | `/api/v1/api-tokens/` | JWT | 是 | 创建 PAT |
| GET | `/api/v1/api-tokens/` | JWT | 是 | 列出 PAT |
| GET | `/api/v1/api-tokens/{token_id}` | JWT | 是 | 令牌详情 |
| DELETE | `/api/v1/api-tokens/{token_id}` | JWT | 是 | 吊销 PAT |

---

## 3. 平台 API Key 与环境变量清单

来源：仓库根目录 `.env`（**已 gitignore，不入库、仅本地**）。以下仅保留前缀/占位，不暴露完整密钥。

| 变量名 | 用途 | 归属/平台 | 是否入库 |
|---|---|---|---|
| `OPENAI_API_KEY` | LLM 调用密钥 | 火山方舟 Ark（OpenAI 兼容接口，经百度 agent-awd 网关） | 否（仅 `.env`） |
| `OPENAI_BASE_URL` | LLM 网关地址 | `https://agent-awd.baidu.com/v1` | 否 |
| `POBI_V2_MODEL` | 模型标识 | `openai/glm-5.2-agent-chanllenge` | 否 |
| `POBI_API_TOKEN` | 平台访问令牌 | 自有平台（前缀 `pk_3758d20e_...`） | 否 |
| `POBI_V2_TOKEN_ENCRYPTION_KEY` | 令牌加解密密钥 | 平台 | 否 |
| `BENCHMARK_TOKEN` | 基准平台评测令牌 | `tsecbench.zc.tencent.com`（前缀 `a4284b71-...`） | 否 |
| `BENCHMARK_BASE_URL` | 基准平台地址 | 腾讯安全基准平台 | 否 |
| `JWT_SECRET` | JWT 签名密钥 | 平台 | 否 |
| `POBI_V2_ADMIN_EMAIL` / `POBI_V2_ADMIN_PASSWORD` | 管理员凭据 | 平台 | 否 |
| `POBI_V2_SANDBOX_IMAGE` | 沙箱镜像 | `${ACR_REGISTRY:-}xoxruns/sandboxed_kali:latest`（DooD 复用常驻 `pobi_kali`） | 否 |
| `POBI_V2_SANDBOX_NETWORK` | 沙箱网络 | `pobi_net`（bridge，非 host；全面容器化） | 否 |
| `POBI_V2_KALI_CONTAINER_NAME` | 复用沙箱容器名 | `pobi_kali` | 否 |
| `POBI_HOME` / `POBI_CACHE_HOME` | 工作区/缓存根 | 本地 `/root/.pobi_v2` | 否 |
| `POBI_V2_AUTO_APPROVE` | 安全护栏开关 | `true`（自动批准） | 否 |
| `POBI_V2_MAX_TASK_DURATION_MINUTES` | 任务超时 | `60` 分钟 | 否 |
| `POBI_MAX_RUNTIME_SECONDS` | 单任务 wall-clock 熔断（待落地） | 默认 `21600`（6h），见 `EVOLUTION_ROADMAP.md` 阶段 5 | 否 |

> 安全约定：所有密钥均在 `.env`，由 `python-dotenv` 加载；仓库 `.gitignore` 已忽略；注意不要将真实值写入任何文档或提交。

---

## 4. 任务「为何都失败、为何没有产物」——代码级结论

### 4.1 历史失败根因（整合 4 份既有分析报告，已复核）

既有报告位于 `docs/`：`AUTHENTICATOR_DVWA_FAILURE_ANALYSIS.md`（已删）、`SLOW_TASK_ANALYSIS.md`（已删）、`TASK_F3D1D6F6_HANG_ANALYSIS.md`（已删）、`TASK_MANUAL_CANCEL_ISSUES.md`。共性根因在 2026-08-18 复核时**多数已修复**：

1. **认证阶段卡死（DVWA 场景）**：原 `AuthenticatorAgent` 在登录环节死循环。**已解决**——改为 `AuthResolver` + `auth_profiles`，支持 CSRF `user_token` 抓取与多步登录，DVWA 已可正常登录。
2. **内存命名空间不一致（F3D1D6F6）**：原 `_persist_agent_summary` 命名空间不匹配。随着 `Path` 命名空间污染修复与目录统一归口（`检查重构后的目录架构.md`），该静默失败路径已消除。
3. **沙箱命令无超时挂死（F3D1D6F6）**：原 streaming 路径缺超时。随全面容器化（DooD 复用常驻 `pobi_kali`，不再 `--network host` 临时容器）与目录归口，挂死与容器堆积问题已消除。
4. **协作式取消不可靠（手动取消问题）**：cancel 在长 LLM 循环内检查点仍可能延迟，但 `task-reconcile` 兜底后状态/事件/重跑均正常，无数据一致性问题（见 `TASK_MANUAL_CANCEL_ISSUES.md` 复核结论）。

### 4.2 「过程产物为何没有」的代码级解释

关键在 `pobi_v2/pobi_v2/engine/executor.py`：

- **运行轨迹（TaskEvent）会落库**：`event_bus.py` 的 `persist_event_worker` 把约 17 类事件（tool_call_start/end、llm_input/response、phase_changed 等）实时写入 `TaskEvent` 表，失败/中断任务也有这一层记录。
- **结构化产物只在成功分支落库**：`executor._run_task_body` 在 `outcome` 成功返回后，于 `task.status == "completed"` 分支（约第 339–358 行）才调用 `_persist_outcome`，写入 `Finding` / `Artifact`。**失败路径（登录失败、挂死、被取消）到不了该分支**，因此：
  - 没有 `Finding`（漏洞证据）落库；
  - 没有 `Artifact`（产物文件）落库；
  - `TaskEvent` 里有原始轨迹，但缺少「接口资产 / 请求响应样本 / 侦察指纹」等结构化资产。
- **结论**：用户感觉「产物没有」，是因为产物抽取/落库被绑定在「任务成功完成」这一前置条件上，而实测任务普遍在认证/沙箱阶段就失败或挂死，根本到达不了产物落库分支。

### 4.3 影响与改进方向（仍待启动）

- 将「中间产物/侦察资产」的落库从「成功完成」解耦，改为事件驱动、按 Target 累积（见 `docs/THREAT_MODEL_STORAGE_DESIGN.md`，设计稿未落地）。
- 引入单任务 wall-clock 熔断 `POBI_MAX_RUNTIME_SECONDS`，从源头防死循环（见 `EVOLUTION_ROADMAP.md` 阶段 5）。

---

## 5. 现状小结

- 平台架构完整、M1–M8 里程碑均已落地，接口能力覆盖认证、目标、任务、流、审批、报告、运维、定价、Token 共 11 个路由。
- 历史失败（认证死循环 / 沙箱挂死 / 内存命名空间）多数已修复，任务可跑通并产出；校验/目录/容器化均已收敛。
- 当前**缺少按 Target 维度的威胁建模信息持久化**（接口、请求/响应样本、侦察资产），同一站点重跑仍需重复扫描。设计见 `docs/THREAT_MODEL_STORAGE_DESIGN.md`，落地待启动。
- 扫描完成判定语义（非 flag 真实目标）仍有限：judge 以 `task.objective` 为唯一标尺，缺多目标收敛与严重度分级（见 `EVOLUTION_ROADMAP.md` 阶段 5）。
