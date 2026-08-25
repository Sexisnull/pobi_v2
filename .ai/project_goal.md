# 项目定位与技术基线

> 本文件为 `.ai` 心智知识库之一，信息取自 `README.md` / `docs/PROJECT_GOAL.md` / `pyproject.toml` / `pobi_v2/main.py`，以源码为准。

## 项目概述

**Pobi v2** 是一个前后端分离的 **AI 渗透测试 Web 平台**，重构自 `pobi`（deadend-cli 演进分支）。把原本单机命令行运行的 AI 自主渗透引擎，封装为企业级 Web 服务，供安全团队对授权目标做自动化侦察、漏洞利用与报告生成。

最终形态：「可信可用的 Web 版 deadend-cli」——保留原内核多智能体协作、Docker 沙箱验证、ADaPT 递归规划等核心能力，叠加 Web 独有的多租户、持久化、实时流、审批护栏、报告导出等增量价值。

## 核心目标

- Web 控制台创建「授权目标」与「渗透任务」，按 `in_scope` / `out_of_scope` 注入授权范围护栏，越权任务直接拒绝。
- 实时事件流（SSE）观察 AI Agent 思考 / 工具调用 / 置信度 / 状态流转；SSE 断连可经 `/events` 回放。
- 高危工具调用人工审批（fail-closed 护栏），超时 / 拒绝均拦截。
- 多租户隔离（Tenant/User + JWT + 资源租户隔离），所有接口默认 Bearer 令牌。
- 结构化报告导出（Markdown / JSON） + 审计日志留存，满足合规可追溯。
- 会话级 token 累计落库，按每百万 token 单价估算成本。

## 技术选型

| 层级 | 技术 | 版本 | 选型理由 |
|------|------|------|---------|
| Web 框架 | FastAPI | >=0.115.0 | 异步、SSE 原生支持、OpenAPI 自动文档 |
| ASGI 服务器 | uvicorn[standard] | >=0.30.0 | 本地开发 / 生产（gunicorn+uvicorn 多 worker） |
| ORM | SQLAlchemy 2.0 | >=2.0.41 | 异步引擎、2.0 声明式风格 |
| 迁移 | Alembic | >=1.13.0 | 与 SQLAlchemy 配套 |
| 校验 | Pydantic / pydantic-settings | >=2.11.5 / >=2.6.0 | Schema 与配置 |
| 数据库 | PostgreSQL (psycopg3) | - | 主库，多租户隔离 |
| 队列 | ARQ (Redis) | >=0.25.0 | 异步任务队列 + 后台 worker + 取消状态 |
| 缓存/总线 | Redis | >=5.0.0 | ARQ 队列 + 事件总线 + 取消/指令状态 |
| 实时推送 | sse-starlette | >=2.1.0 | SSE 长连接 |
| 鉴权 | bcrypt + pyjwt | >=4.1.0 / >=2.8.0 | 密码哈希 + JWT |
| 前端 | 原生 JS SPA (vanilla) | - | 零构建，FastAPI 直接挂载 `web/`，SSE 同源 |
| AI 内核 | pobi_agent（内嵌子包） | - | CoreAgent 决策内核 / DeadEndAgent 编排 / 工具链 / 置信度护栏；经 uv workspace 复用 |
| LLM 路由 | litellm + instructor + pydantic-ai-slim | 见 pyproject | 多供应商（anthropic/openai/openrouter/gemini/requesty/local） |
| 沙箱 | Docker + Playwright | - | Kali 沙箱隔离执行；无 Docker 回退轻量 ScanWorkflow(M7) |

## 业务假设

- 仅面向**授权**渗透测试（靶场 / 已获书面授权的站点），授权范围由 `Target.scope` 注入护栏。
- 高危操作默认 fail-closed；`agent_mode` 支持 `hacker`（默认需审批）/ `yolo`（自动批准，用于授权靶场自动化）。
- 内嵌 `pobi_agent` / `pobi_prompts` 为仓库子包（非 PyPI 安装），运行依赖在 `pyproject.toml` 显式声明。

## 迭代里程碑

- [x] M1–M8：FastAPI 骨架 / 队列+SSE / 持久化 / 多租户 / 审批 / 前端 SPA / 轻量扫描 / 完整多智能体主路径
- [x] M8+：运行指令通道 / 系统对账 / 统一 LLM 抽象层 / Token 用量 / probe 快路径
- [x] 跨任务数据存储（Recon 双主轴）：本地 SQLite per-task + PG per-target 聚合层，按 `target_id` 收敛历史沉淀、新任务 `seed_from_pg` 复用；详见 `docs/RECON_LOCAL_STORE_DESIGN.md`、`.ai/roadmap.md`
- [ ] 自定义工具添加（下一步）：在 `pobi_agent/tools/` 体系上支持用户自定义工具注入 Agent 工具集，复用工审护栏与沙箱边界，见 `.ai/roadmap.md` T1–T6
- [ ] M9 预研：模型路由按角色分流（payload 等敏感输出走本地无审核模型，规划推理走云端有审核模型），4 步改造路线见 `README.md` §模型路由
- [ ] 工程治理待办（A1–A7，见 `docs/PROJECT_GOAL.md` §2.4）
- [ ] 扫描内核优化（S1–S6，见 `docs/PROJECT_GOAL.md` §2.5）
