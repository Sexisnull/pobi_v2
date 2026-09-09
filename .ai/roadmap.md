# 开发路线图

> **以源码为准**。本文件是演进计划的唯一清单：早期引用的 `docs/PROJECT_GOAL.md`（§2.4 工程治理 / §2.5 扫描内核优化）**该文件已不存在**，其条目已内联至下方「待办」的 A1–A7 与 S1–S6，勿再按原路径引用。

## 已落地（本轮更新确认）
- [x] **agent 循环 P2 低危项修复（2026-09-09）**：循环收敛性外的三类健壮性修复。
  - **P2-1 role 序列净化**：`executor.py` 驱动循环在 `window_messages` 后剔除末尾 `role=="tool"` 消息——CoreAgent 因 `max_iterations=50` 自然退出且最后一条为 tool 结果时，窗口化后的 history 以 tool 结尾，下一轮 `messages=[..., tool, user]` 触发 API 400。测试 `test_driver_loop_strips_trailing_tool_message`。
  - **P2-2 字段名澄清**：`context["supervisor_history"]` 更名 `context["agent_context"]`（实际存的是入参 agent_context = unified_context + task_state + tasks_context，非驱动循环对话历史），`_build_validation_input` 同步读取；`ValidationInput.supervisor_history` 字段名保留（公共接口）。
  - **P2-3 空决策防护**：`action=call_agent` 缺 `agent` 或 `prompt` 时不再委派子 agent（空 prompt 会让子 agent 收到空任务乱跑），回灌 `[invalid supervisor decision]` 提示由 supervisor 修正。测试 `test_driver_loop_blocks_empty_agent_prompt`。
  - 全量 222 passed / 2 skipped。

- [x] **ADaPT 外层循环缺陷修复（2026-09-09）**：评测定位 P0-1/P0-2 两个循环致命缺陷并修复（另顺带 P1-3/P1-4 刹车配置）。
  - **P0-1 退出条件**：`architecture.py` 外层 `while not should_exit or node.status != "completed"` 的 `or` 应为 `and`——子任务触发 `ValidationStopEvent`（exit_loop=True）后父节点仍继续迭代，全局退出机制失效。真实代码验证：修复前 root 收到 3 次 exit_loop 仍迭代至 executor 第 7 次调用（人为打断），修复后 2 次即正常退出。
  - **P0-2 迭代无上限**：attempts 自增从 `_solve` 入口移入 while 每轮迭代并检查——原实现 expand/refine 递归返回后迭代不经过入口，`MAX_TASK_ATTEMPTS=3` 失效，同一 node 可无限迭代（复刻实测 32 次 executor 调用仍不终止）直至外部超时熔断。修复后恒 conf=0.5（expand 区间）有界（≤10 次调用）、恒 conf=0.7（refine 区间）恰好 3 次后 `failed:max_attempts`。
  - **P1-3 轮次/预算解耦**：`SupervisorLoopConfig` 新增 `max_rounds=40` 独立于 `request_limit=80`——原 `max_rounds=request_limit` 导致每轮恰好 1 次请求时 usage 刹车永不先于 for 触发，且 recon_lookup 工具调用额外扣减决策轮次。
  - **P1-4 子 agent 独立预算**：新增 `_SUB_AGENT_USAGE_LIMITS`（request_limit=200）替代共享 supervisor 预算——原共享 40 次累计，高频子 agent（requester）单次深度测试即耗尽，后续轮次一调用即触顶永久降级。
  - 测试：新增 `tests/test_adapt_loop.py` 3 例（exit_loop 停止 / expand 有界 / refine 有界），更新 `test_supervisor_loop.py` 配置断言；全量 220 passed / 2 skipped。

- [x] **§8 路径 A：Supervisor 决策器 + 驱动循环（2026-09-05）**：根治 L3 主膨胀源（`message_history` 跨轮无界 + 无 `usage_limits` 刹车）。`supervisor.instructions.jinja2` 由 router 改写为 decider（`SupervisorDecision{action, agent, prompt, ...}`）；`executor.execute_supervisor` 新增驱动循环（`window_messages` 窗口化 → `supervisor.run` → 解析决策 → `_run_sub_agent` 直调子 agent → compact turn 回传 → 再窗口化），并在 `architecture.py` ADaPT 外层 `while` 跨轮持久 `supervisor_history`；恢复 `_DEFAULT_SUPERVISOR_LOOP_CONFIG` 有界 `UsageLimits` 原生刹车（`FallbackAgentResult` 触顶即终止）。落库闭包零改动，findings 无回归；`tests/test_supervisor_loop.py` 8 例全绿。与 M1（治 L2 渲染）正交，未并入。

- [x] **前置侦查 pre_recon + 指纹识别（2026-09-01）**：平台层自动通道，任务启动后、智能体侦查前自动执行指纹识别 + WAF 识别并落库、推送前端实时流。新增独立明细表 `recon_fingerprints`（幂等键 task_id+target_url）；引擎收敛到 `pobi_agent/tools/fingerprint/engine.py`（无 ctx）；认证感知（读取 PreAuth `preauth` 会话注入 cookies，无凭证降级外部探测）；落库四通道（fingerprint 明细 + technology facts + endpoints tech_stack + fingerprint|host 足迹）。**指纹识别不再作为 agent 工具**（已从 requester 装配移除），agent 通过 L0/L1 注入与 recon_lookup 复用结论。测试 `tests/recon/test_pre_recon.py` 5 项 + `test_fingerprint.py` 8 项，全量 recon 66 passed / 1 skipped。
- [x] **站点地图 sitemap + katana 集成（2026-09-02，G5 里程碑）**：前置侦查第二子阶段接入，Kali 内执行 katana 构建站点地图。**katana 由用户自建 Kali 镜像预装打包**（代码仅 `katana -version` 健康检查，缺失 → `skipped` 不阻断任务）。
  - M1 数据基座：`recon_http_transactions` 流水表（source 区分 `sitemap:katana`/`agent:requester`、status/headers/body/title/content_type/size/tech_stack/params/forms、auth_used/auth_required、**分层存储**）+ `ReconStore.insert_http_transaction/list_http_transactions` + `pobi_agent/utils/urls.py`（split_url/normalize_path/is_static_asset/is_noise_path/has_noise_params/extract_params）。
  - M2 引擎：`pobi_v2/engine/sitemap/katana_runner.py`（build_katana_command 含 Cookie/header 认证注入 + `-ef`/`-iqp` 去噪 + shlex 防注入；katana_available 健康检查；run_sitemap 组合器 + 原始 JSONL 落盘 `tasks/<id>/sitemap/katana_raw.jsonl` + SSE 事件）+ `pobi_agent/tools/sitemap/engine.py`（parse_katana_jsonl/should_keep/persist_sitemap → transactions + endpoints(置信度0.6, discovered_via=sitemap:katana) + 足迹 `sitemap|{host}` 防重）。
  - M3 pre_recon 接入：fingerprint 之后 `run_sitemap`，复用 `_resolve_auth(preauth)` 认证会话注入 katana；返回带 `sitemap`/`sitemap_status` 字段。
  - M4 requester 对齐：`pw_send_payload` 经 `_persist_http_tx` 从 raw 文本解析结构化字段 → `ContextEngine.add_recon_http_transaction` 落同一事务表（source=`agent:requester`），为站点地图页面对齐数据。
  - M5 防重复：L1 端点注入加 `via=` 来源标注；requester 提示词"不重复枚举 `via=sitemap:katana` 端点"；covered_block 历史端点跳过；**katana 不暴露给 agent 工具集**（仅前置侦查平台层）。
  - **三层防线（防重复请求/无意义页面）**：① katana 参数层（`-ef`/`-iqp`）；② 落库层 utils.urls 过滤（静态/登出错误噪音/纯分页参数变体；katana README 无 `-pcs`/`-fsu`/`-filter-page-type`，已修正不传）；③ agent 复用注入（L1 + covered_block）。
  - 测试 `tests/recon/test_sitemap.py` 9 项（解析/过滤/落库/命令构造/run_sitemap skipped 与 completed/requester 事务对齐），全量 recon 75 passed / 1 skipped。
- [x] **响应体分层存储 + 目标级增量更新（2026-09-02）**：sitemap 数据基座按「骨架必存 + 响应体分层 + 跨任务增量」改造。
  - **分层存储（M7，store.py）**：`recon_http_transactions` 加 `storage_strategy`（full/compressed/digest）+ `body_compressed`（gzip BLOB）列，旧库 `_ensure_column` 幂等补列。规则：<100KB → full 明文全量；100KB–1MB（非 API）→ compressed gzip 存 BLOB（DB 存 200 字符预览）；>1MB（非 API）→ digest 仅存前 2048 字符摘要；**json/xml API 响应例外——即使大也全量（≥100KB 走 compressed 压缩保留）**。常量 `_BLOB_THRESHOLD_BYTES/_COMPRESS_MAX_BYTES/_DIGEST_PREFIX_BYTES/_API_CONTENT_TYPE_RE` 可配。**读取侧按需拉取**：`list_http_transactions` 默认只回骨架（不拖大 body），`get_transaction_body(task_id, tx_id)` 解压/取全文。
  - **目标级增量更新（M8，PG 聚合）**：新增 PG 聚合表 `recon_http_transactions_agg`（alembic `0020_recon_http_tx_agg`，唯一键 target_id+tenant_id+method+url，含分层存储 body）。`upsert_to_pg` 扩展：本地脏事务（pg_synced_at IS NULL）→ PG url 维度收敛 upsert → 提交成功后打标（仅 PG 成功才置已同步，失败不误标可重试）；`seed_from_pg` 扩展：拉目标历史事务骨架灌本地端点树（covered_block / L1 增量提示，新任务不重复枚举已抓 url）。本地 sqlite 只存当前任务，任务完成推 PG 做 target 级关联。
  - 测试：test_sitemap.py 分层/digest/API/按需拉取/PG 不可达不误标 + test_incremental_seed.py 事务骨架 seed（新增 4 项），全量 recon 78 passed / 1 skipped（PG e2e 需容器内 `POBI_TEST_PG_URL` 跑，本地跳过）。后续：站点地图前端页面（渲染 recon_http_transactions + recon_endpoints，数据已备）。
- [x] **前置侦查数据模型重构 + PG 双向同步闭环（2026-09-02）**：将 `recon_http_transactions` 收敛为唯一真源，端点树改由事务派生，并打通 Layer1（supervisor 启动注入基线）/ Layer2（历史任务续扫同步）。
  - **根因修复**：生产主链路（deadend_runner → threat_model → execute_supervisor）兜底 `upsert_to_pg` 时，本地 append-only 流水同 `(method,url)` 多行直接整批 INSERT，撞 PG `recon_http_transactions_agg` 唯一键 `(target_id,tenant_id,method,url)` 同命令重复 → `CardinalityViolation`（取消任务场景必现）。修复：seed-out 前按 `(method,url)` 折叠脏行取最新代表，折叠的全部脏行统一打 `pg_synced_at` 防漏同步。
  - **端点树单一派生**：移除 sitemap(`persist_sitemap`) / 指纹(`persist_fingerprint`) / 运行期(`ContextEngine.add_recon_endpoint`) 对 `recon_endpoints` 的双写；三者只写 `recon_http_transactions` 流水。`ReconStore.derive_endpoints_from_transactions` 按 `(host, path_normalized)` 从 tx 派生端点树（状态择优 2xx>3xx>4xx>5xx、tech_stack/params 并集、auth_required 任一真、多观测状态码/方法在 notes 标注认证差异），pre_recon 落库后调用。`add_recon_endpoint` 改为写一笔 tx 观测（endpoint_parser），由派生归并。
  - **站点总览读时现算**：`ReconStore.build_site_overview` 从 tx 现算（主机数/端点观测/状态码分布/需认证端点/参数 Top/技术栈 Top/输入点），**不落物化表**，由 `build_index_view` L0 与 supervisor 基线块消费。
  - **Layer1 启动注入**：`execute_supervisor` 的 `supervisor_prompt` 新增「目标侦察基线」块（`build_baseline_block`，token 预算 2000，失败仅 warning），supervisor 启动即拥有指纹/总览/端点/认证面，无需先跑工具。
  - **Layer2 seed-in 解锁**：生产主链路原从未调用 `seed_from_pg`（仅 ADaPT `start_supervisor` 分支触发），故历史任务沉淀无法续扫。`deadend_runner` 任务启动、pre_recon 完成后、threat_model 前调用 `ReconStore.seed_from_pg` + `seed_local_artifacts` 预热（失败仅 warning）。
  - 测试：新增 `tests/recon/test_derive.py`（端点派生/总览/基线/seed-out 折叠 5 项），并同步更新 `test_sitemap.py`/`test_recon_persist.py`/`test_pre_recon.py` 断言以贴合「端点派生、不再双写」契约；全量 recon 86 passed / 1 skipped。
- [x] **任务认证前置 PreAuth（2026-09-01）**：任务创建阶段完成登录，为 L0 认证后爬取与 exploitation 提供会话。**⚠ 仅自动分支生效，手动分支于同日搁置（见本条说明）**。
  - 数据模型：`Task` 新增 `auth_mode/auth_status/auth_username/auth_secret(Fernet 加密)/auth_login_url/auth_profile(默认 preauth)/auth_error/auth_updated_at`（alembic `0018_task_auth`）。
  - 引擎层 `engine/preauth.py`：`run_auto_auth` 自动分支（复用原生 `authenticate_service`，无主控调度）**已生效**；`ManualAuthSession` 手动分支（`BrowserSession` 截图轮询 + navigate/click/fill/press/eval/wait 指令，10 分钟超时自动销毁）**已整体注释搁置**（`preauth.py:9` 注明"已禁用，2026-09-01 搁置"）。
  - API `routers/task_auth.py`：**实测仅 `GET /auth/status`、`POST /auth/auto` 两个端点**；~~`POST /auth/manual/start|action|capture|abort`、`GET /auth/manual/snapshot`~~ **已移除**（前端 `api.js` 对应调用同标注 `[DISABLED 2026-09-01]`）。
  - 会话落盘走原生 `AuthContextHandler` 三件套（`{profile}.json + {profile}.playwright.json + index.json`）；认证结果写 recon_facts（category=authentication）；**防覆盖靠 profile 名隔离**（`preauth` vs 原生自有 profile），authenticator 提示词注入"已有 preauth 会话优先 validate 复用"。
  - 前端：`Tasks.jsx` 创建表单认证方式选择（**manual 选项已注释**，`Tasks.jsx:508`）+ `AuthPanel.jsx` 手动登录远程控制面板（**随手动分支搁置而未被启用**）。
  - 测试：`tests/test_preauth.py`（manual 相关用例 `test_manual_session_ttl_*` 已注释）。
  - 说明：手动分支 `_MANUAL_SESSIONS` 为进程内注册表，仅 api 单 worker 有效（dev 可接受，多 worker 需 Redis）——此限制是搁置原因之一，重启该分支前须先改 Redis / 共享存储。
- [x] **创建前凭据预检（2026-09-01 增量）**：任务创建时配置账号密码后先真实登录验证，凭据错误不允许发放任务。`POST /api/v1/tasks/verify-auth`（无 task_id 依赖）+ `engine/preauth.verify_credentials`（临时目录 + 唯一 `verify_<uuid>` profile 跑 `authenticate_service`，验证结束清理，不落盘、不污染熔断计数）；前端 Tasks.jsx 增加「验证凭据」按钮 + 结果徽标（成功绿/凭据错误红/需人工橙），auto 模式创建门禁（`failed` 拦截、MFA/aborted 放行走手动）。测试 `tests/test_preauth.py` 增至 16 passed。
- [x] **pre_recon 时序修复 + executor 日志 bug（2026-09-02，由任务 `71fb302c` 暴露）**：① `executor._run_pre_recon_branch` 引用未定义 `log`（局部变量仅在 run_task/_run_task_body 内）→ pre_recon 完成后必 `NameError` → 任务必 failed、主 agent 未启动（PG tasks.error 实锤）；函数内补 `log = task_logger(tid)`。② pre_recon 与后台 preauth 认证并行抢跑 → 无凭证爬取：`run_pre_recon` 新增 `preauth_wait_timeout`（默认 0 向后兼容）+ `_wait_for_auth_context` 轮询认证会话就绪（间隔 1s，超时降级外部探测）；executor 在任务带认证（`task.auth_username`）时传 `PREAUTH_WAIT_TIMEOUT(60s)`。测试 `tests/recon/test_pre_recon.py` 新增 3 项，33 passed。
- [x] **凭据落任务目录 + authenticator 重认证闭环（2026-09-02 安全强化）**：凭据属**不可复用资产**（登录态会失效），因此从头到尾**不落任何数据库**（pgsql/sqlite），仅写任务目录文件。① 删除 `tasks.auth_secret` 列（alembic `0019_drop_auth_secret`），密码不再 Fernet 落库；② 新增 `CredentialsStore.save_credentials`（写任务目录 `tasks/<task_id>/reusable_credentials.json`，0600 权限）+ wallet 路径**动态解析**（task_root 注入时读任务钱包，否则回退全局 `~/.pobi_v2/reusable_credentials.json`）；③ `create_task`（auth_mode=auto）时 `preauth.save_task_credentials` 写任务钱包，密码明文仅在创建请求体/任务钱包/运行内存间流转；④ **authenticator 重认证闭环打通**：任务运行时（task_root 已注入）`CredentialsStore.resolve(target, 'preauth')` 从任务钱包读密码 → 重新认证不再缺凭据（此前缺口：密码只在 DB 且 agent 拿不到）；⑤ `POST /auth/auto` 凭据回退从 DB 解密改为任务钱包 resolve；⑥ recon_facts 的 authentication 事实不再携带 username（不落 recon sqlite），仅保留 auth_profile/auth_status 等非敏感元数据；⑦ 顺手修复 `tasks.py` 预存 bug：`logger` 从未定义却在使用（此前 create_task 必然 500，容器从未测到），补 `logger = logging.getLogger(__name__)`。端到端已验证：创建 auto 任务 → 任务钱包落盘（含 username/password/login_url）→ DB 无密码列 → authenticator resolve 读到凭据；测试 `tests/test_preauth.py` 新增 3 项（任务钱包写入/隔离、task_root 注入下 resolve、auth 事实无 username），25 passed；全量 178 passed（1 预存 shell 失败）。
- [x] **目标总览页（2026-08-31）**：`授权目标` 视图内左侧新增「目标列表 / 目标总览」标签页，点选目标卡片进入总览。后端在 `routers/targets.py` 新增 6 个 per-target 只读接口（`overview`/`tree`/`facts`/`threats`/`findings`/`artifacts`），跨 `recon_facts_agg` + `recon_endpoints_agg` + `recon_threats_agg` 与通用阶段表 `findings`/`artifacts`/`tasks` 聚合；树节点严重度按 `path_normalized` 匹配 `target_endpoint`（全等优先、回退子串）。隔离沿用首行 `Target.tenant_id` 校验，查询只走 `WHERE target_id`（不 JOIN tasks）。前端为 vanilla JS，无构建步骤。
- [x] **PG 增量同步 + 资产聚合表（2026-08-31）**：本地三表加 `pg_synced_at` 脏标记列（旧库幂等补列），`upsert_to_pg` 只同步脏行、成功后打标，`_recon_emit_sync` 改 in-flight 合并防 create_task 堆积；收敛策略改"内容最新 wins + confidence GREATEST"。新增 PG 资产聚合表 `recon_endpoints_agg`（alembic 0017）支撑目标全景图资产视图，`seed_from_pg` 续扫灌入本地；新增 `GET /targets/{id}/assets` 接口。用户环境需 `alembic upgrade head` + 重建 worker/api 容器。
- [x] **子 agent 死循环熔断 + 足迹驱动收敛（2026-08-31）**：修复任务 `535aee8e` requester 死磕 UNION SELECT 被 connection reset 拦截后 50 轮不收敛、30min 熔断 failed 的问题。落地：`pw_send_payload` 实时足迹写 `recon_techniques`（含 endpoint 前缀、成功/失败均记）→ 接通 `was_already_attempted` 防重复 → `is_surface_dead` 死路硬护栏（>=10 失败无成功即 BLOCKED）→ supervisor/requester prompt 注入 "Failed Attempt Footprints" 摘要证据引导转向 → 超时分支补错误描述。用户自行新建任务验证。
- [x] **跨任务数据存储（Recon 双主轴）**：`docs/RECON_LOCAL_STORE_DESIGN.md` 描述，2026-08-19 三阶全合入：
  - 本地 SQLite per-task（`recon_sessions/facts/endpoints/techniques/threats` + `ReconStore`，WAL/AES-GCM 占位/`lookup`/L0-L2 token 预算裁剪），`ContextEngine` 旁路非阻塞写入。
  - **PG 聚合层 per-target**（`recon_facts_agg`/`recon_threats_agg`/`recon_threat_evidence_link`，`pobi_v2/db/recon_models.py`，alembic `0014_recon_agg.py`）：按 `target_id` 跨任务收敛历史沉淀。
  - 闭环：`recon_sync_worker` 订阅 `__recon_sync__` 异步 upsert 到 PG；新任务 `ReconStore.seed_from_pg(target_id)` 预热复用；增量续扫 + 威胁状态机（`suspected→confirmed→exploited→remediated`）+ prompt 显式跳过重复工作。
  - **2026-08-27 修复**：本地库路径统一为任务级单一库 `tasks/<task_id>/<task_id>.db`（去掉 `recon/` 子目录），并修复双库根因（`pobi_agent.py` 原误用 `agents_storage_root` 作 task_root）；`recon_sync_worker` 已在 `main.py` 启动期拉起；`deadend_runner.py` 真实任务分支补注入 `target_id`/`tenant_id`（链路探针 `probe` 走 `probe_runner` 轻量路径，不构造 `DeadEndAgent`，不落库）。
  - 测试：`tests/recon/` 49 passed / 1 skipped（迁移用例需 `POBI_TEST_PG_URL`）。
  - 剩余可选项（非阻塞）：本地↔PG evidence 双向同步、真实 PG e2e、secret 级密钥管理（当前 AES-GCM 占位明文）。

## 进行中
- [x] **目标详情页「攻击流」三视图（2026-09-07 完成）**：目标详情页 6 个 Tab 全是静态清单快照，无法回答「什么时候发生的 / 这条攻击路径为什么成立 / 当时怎么跑的」。新增：
  - 后端 `GET /targets/{id}/attack-flow`：图谱（事实 → 威胁 → 端点，发现反证威胁）+ 任务泳道事件密度时间轴，**零迁移**，只消费 `recon_*_agg` + `recon_threat_evidence_link` + `findings`；时间轴只 select `created_at`/`event_type`（不取 payload），按固定桶数聚合，渲染量与事件总量解耦。
  - 后端 `GET /tasks/{id}/events/range`：只做 `count/min/max` 聚合，供回放播放器把进度条映射到 seq；明细仍走既有 `?after_seq=` 游标分页。
  - 前端 `components/attackflow/`：`Timeline`（零依赖，泳道 + 时间桶 + 滚轮缩放 / 拖拽框选 / hover 明细 / 点击跳任务回放）、`GraphView`（`@xyflow/react` + dagre 自动布局、严重度配色、类型与状态过滤、点击下钻详情抽屉）、`ReplayBar`（播放/暂停/单步/倍速/进度条 seek，接入 `TaskConsole` 回放页签）。
  - 新增依赖 `@xyflow/react`、`dagre`。测试 `tests/test_attack_flow.py` 12 项通过；全量 131 passed / 1 skipped。
  - **顺带消化 P3**：「资产覆盖图前端」的诉求（回答"测没测全""为什么成立"）已由关系图谱 + 时间轴覆盖，P3 不再单独立项。
- [x] **Agent 治理：可观测与审计增强 P0+P1（2026-09-07 完成）**：补全登录/目标变更/审批决策/护栏越权拦截审计覆盖；`actor` 强制真实责任人；Agent 高风险动作（高危工具闸门的创建/自动批准/人工决策、子 Agent 委派、越权拦截）逐条入审计 + 任务完成追加 `agent.run_summary` 汇总；`audit_events.tenant_id` 改 `SET NULL`；新增 `trace_id`/`span_id` 打通 OTel 关联；行级哈希链（`prev_hash`/`hash` + `verify_audit_chain`）+ PG append-only 触发器；迁移 `0021_audit_governance`；`tests/test_audit.py` 7 项通过。
  - **遗留（P2，未做）**：Prometheus `/metrics` 与成本看板、指标时序化（`task_metrics_agg` 现为按 target upsert 最新值）、统一脱敏层（prompt/tool_args/event payload）、Agent 健康端点与失败告警。
  - **顺带修复的既有缺陷（2026-09-07 随审计增强提交后单独修复）**：`engine/executor.py` 取消/越权/超时等返回分支引用不存在的 `task_id`（应为 `tid`），命中即 `NameError`；`_is_safe_shell` 黑名单用精确子串 `curl | sh`，`curl <url> | sh` 变体可绕过，改为正则拦截「管道喂给解释器」。全量测试 119 passed / 1 skipped。
- [ ] **自定义工具添加（下一步）**：在当前工具体系（`pobi_agent/tools/`，含 `tool_wrappers.py` 审批包裹、`models/registry.py` 注册、`context_engine.py`/`architecture.py` 接入点）上，支持用户自定义工具注入到 Agent 工具集。路线草案（待细化）：
  - T1 明确自定义工具的来源形态：用户提供脚本（Python 函数 / CLI 包装）还是配置声明（name + 描述 + 参数 schema + 执行入口）。
  - T2 设计注册入口：扩展 `models/registry.py` 或新增 `custom_tools` 注册表，支持运行时挂载而不改内核代码。
  - T3 复用 `tool_wrappers.py` 的 `ApprovalProvider` 包裹，自定义高危工具同样走审批护栏（fail-closed）。
  - T4 `ContextEngine` / `architecture.py` 在 Agent 装配工具集时并入自定义工具，并按 `agent_mode`（`hacker`/`yolo`）决定默认审批策略。
  - T5 持久化与多租户隔离：自定义工具定义按租户/任务归属存储（PG），不与 `pobi_agent` 内核硬编码工具混淆。
  - T6 前端：任务创建页提供"附加自定义工具"入口，列出可用工具并支持参数预览。
  - 约束：自定义工具执行须经沙箱（DooD `pobi_kali`）或受控边界；须服从授权范围护栏（`ScopePolicy`），越权调用失败关闭。

- [ ] M9 预研：模型按角色分流（payload 走本地无审核模型，规划推理走云端有审核模型），4 步路线：
  - M9-1 `llm/config.py:get_model_spec` 增加 `role` 参数，按 `POBI_V2_MODEL_{ROLE}`/`POBI_V2_LLM_{ROLE}_API_KEY`/`POBI_V2_LLM_{ROLE}_API_BASE` 解析
  - M9-2 引入 `ModelRouter(default, roles)`，`deadend_runner` 注入替代裸 `ModelSpec`
  - M9-3 `pobi_agent/agents/factory.py:AgentRunner` 兼容 `ModelRouter`，按 Agent 名取模型
  - M9-4 标记 payload 生成角色（shell/python_interpreter 绑定 payload），可选补前端"任务可选模型"UI
  - 约束：所有模型仍经 LiteLLM 统一路由与计费；本地 `api_base` 走内网，凭证与云端隔离

## 待办
- [ ] 工程治理（A1–A7，原 `docs/PROJECT_GOAL.md` §2.4——**该文件已不存在，本清单为唯一留存**）：
  - [x] **A1 分层倒置**（`routers/system.py` 脆弱写法 / engine→routers 反向依赖）——**2026-08-31 已修复**：`task_reconcile` 下沉 `engine/reconcile.py`（router 端点与 Worker cron 统一调用，`JOB_TIMEOUT`/`HEALTH_CHECK_KEY` 随迁）；`routers/tasks.py` 经 `engine/recon_access.py` 访问本地 recon 库与清理任务产物目录，不再直接 import `pobi_agent`
  - [x] **A7 `routers/system.py` 脆弱写法**——**2026-08-31 已改善**：对账逻辑与辅助函数（`_job_in_queue`/`_worker_online`/`_ACTIVE_STATUSES`）移出后，端点变薄
  - [x] **A3 `logs/` 入版本控制**——已通过 `.gitignore` 排除（`logs/`、`python_scripts/`、`scripts/` 均不入库）
  - [ ] **A2 `python_scripts/` 失序**——一次性调试脚本已被 gitignore 排除，但未归档清理（保留本地）
  - [x] **A4 `llm/` 孤儿模块**——**2026-08-31 已核实**：LLM 唯一入口落实，`pobi_agent` 内无 litellm 真实直连（仅 `models/registry.py` embedding 例外，属声明允许）
  - [ ] **A5 生产 CORS 收敛**——dev 暂 `*`（`allow_credentials=True` 禁止与 `*` 同用），生产须收敛为具体 origin
  - [x] **A6 `main.py:web_app` 分支矛盾**——**2026-08-31 已清理**：`index.exists()` 二次判断恒 False 的冗余分支去除，缺失时兜底返回 README 语义不变
- [ ] 扫描内核优化（S1–S6，原 `docs/PROJECT_GOAL.md` §2.5——**该文件已不存在，本清单为唯一留存**）：侦查产物结构化与向量化 / 上下文按需检索 / ~~指纹识别能力~~（**2026-09-01 已落地**为平台层 `pre_recon` 自动通道，见「已落地」首条）/ 漏洞利用工具补全 / Supervisor prompt 去靶场假设 / LLM 决策与工具执行分工
- [ ] **requester 抓页-重抓循环优化（2026-08-28 由任务 `535aee8e` 暴露）**：任务 23.5h 空转，requester 反复执行**同一** `run_python_file` 脚本抓取 DVWA sqli 整页 HTML 达 35 次，直到 iteration 44 才确认注入所需信息（security=low、`GET /vulnerabilities/sqli/` 的 `id` 参数），全程 0 次真实 SQLi 注入、0 findings。
  - 根因：`run_python_file` 输出整页 HTML 超长，`truncate_string` 截断至 20000 token，requester 每次"没看全"表单/认证细节 → 重抓重确认。
  - 方向（待评估）：
    - R1 工具层精炼输出：页面/脚本结果按"关键结构提取"（表单字段、端点、cookie 名、参数）输出，而非整页 HTML 原样透传。
    - R2 提示词引导：要求 requester 一次性提取并**显式落盘**已确认的表单/端点结论（写入 recon/memory），后续直接引用而非重抓。
    - R3 上下文去重：同源大块工具结果在进入模型前做摘要/去重，避免反复把整页 HTML 灌入上下文挤占窗口。
  - 验证：重跑 DVWA 类任务，从任务创建到首次真实注入请求的工具调用轮数应显著下降（目标 < 10 轮）。
- [ ] **加密站点支持（后续演进，尚未启动）**：当前请求通道（`pw_send_payload`/`raw_send` 底层 Playwright `APIRequestContext`，拦截 `response` 事件原样透传字节）对 payload 内容**协议层盲发**，验证强制要求证据 verbatim 出现在工具响应文本（`_anti_fabrication`）。当站点请求/响应全程加密时，requester 既无法构造合法密文、也无法对密文响应做语义比对，`confidence_score` 卡在 0.3 以下无法闭环上报。
  - 现状缺口：① `requester.instructions` 无「加密站点」分支，`browser_run_steps` 仅能填表/点击/取 DOM，无法介入前端 JS 加解密执行上下文；② supervisor 不会在响应全乱码 / 含固定密文封装字段（`{"data":...,"iv":...}`）时自动派发逆向任务；③ `python_interpreter_agent` 虽能复现加解密，但尚无工作流把「拉 JS→逆算法→产出可复用 encrypt/decrypt 工具」串起来喂给 requester。
  - 演进方向（待细化，最小改动不动内核）：
    - E1 加密嗅探：响应 body 非可打印 / 出现密文封装字段 / `Content-Type: application/octet-stream` → 标记 `encryption_detected=true`，进入加密处理分支。
    - E2 逆向通道：复用 `python_interpreter_agent`（沙箱），拉取 `/static` JS（当前提示词要求跳过 `.js`，加密站点应反例——必须拉取逆向），提取算法（AES-GCM + 接口下发 key / RSA 公钥 / 自定义异或 scheme），产出可复用 `encrypt()/decrypt()`。
    - E3 验证闭环：requester 在加密站点走「明文 → 加密 → 发送 → 解密响应 → 语义比对」闭环，使 `confidence_score` 真正可证，而非仅「发出去」。
    - E4 约束：逆向产物（密钥/IV）按任务钱包同等敏感级处理，不落 DB；前端加密算法可能含随机数/时间戳，需先固定随机源再比对。
  - 验证：构造一个前端 AES 加密回显的靶场，任务应能从 JS 逆向算法 → 发出合法密文 → 解密响应 → 确认注入命中并上报 finding。
- [ ] 白盒代码分析（可选）：`pobi_agent.code_indexer.SourceCodeIndexer`（Playwright/Embedder/RAG），默认关闭，依赖齐备后启用，缺失优雅降级黑盒
- [ ] **探索过程可检索化（P1–P4，2026-09-01 立项）**：源自下方「外部项目借鉴分析（ARTEX）」，P1 优先。
- [ ] **Agent 记忆与上下文分层优化（2026-09-03 立项，方案见 `docs/agent-memory-architecture-plan.md`）**：核心问题是上下文膨胀 + 三个真缺口（工作记忆 / 结构化摘要回流 / 威胁未落库）。
  - **M0 现状基线（已完成）**：`.ai/architecture.md` 新增「上下文与记忆分层」章节，固化注入入口与预算、SECTION 结构、已上线能力与已知缺口。**后续推导一律以该节为准**，勿再按「L0/L1/L2 待新增 / 威胁无人写」的旧描述推演。
  - [ ] **M1 上下文压缩**：SECTION 3/4（COMPLETE TEST HISTORY / KEY DISCOVERIES）加预算裁剪；`maybe_summarize_context` 阈值由 `200_000` 下调至 ~30k；`_add_agent_output_to_context` 把子 agent detailed_summary/proofs 摘要化后再入 fact。验收：同类任务 token 量对比下降，且 findings 不回归。
  - [ ] **M2 结构化摘要注入**：新增 `add_agent_summary` → `deque(K)` → `get_unified_context` 注入最近 K 条，降低「每轮 MemoryAgent LLM 重汇总」开销（保留原路径降级）。
  - [ ] **M3 工作记忆窗口**：`ContextEngine.working_memory = deque(maxlen=3)`，requester 读写当前 cookie/session_key/payload，与 `message_history` 协同不重复。
  - [ ] **M4 威胁闭环**：executor 旁路补威胁创建入口（**复用** `upsert_threat` / `record_threat_status`，不重建状态机），利用阶段注入「待验证威胁清单」逐条推进状态机 → PG 同步 → 前端威胁列表。验收：真实任务 `recon_threats` 有 agent 自产记录，前端态势条非空。
  - **明确不做（红线）**：① 另起一套 L0/L1/L2 注入逻辑（已上线，只扩展 `build_index_view`）；② 给 `recon_http_transactions` 加 `(uri_template, body_md5)` 唯一索引去重（破坏 append-only 流水语义，去重只在 PG `recon_http_transactions_agg` 层）；③ 引入 LanceDB/Chroma（沿用 `SqliteRagConnector`，保持零外部依赖 + 任务隔离）；④ 新建独立 `memory_storage/` 目录（破坏任务隔离与 PG 聚合闭环）。完整红线见 `.ai/constraints.md`「上下文与记忆分层」条目。

- [ ] **manual 认证分支残留清理（2026-09-01 依源码校正确认）**：手动登录分支已搁置，残留物未清理——① ~~`webapp/src/pages/Tasks.jsx` 的 `auth_mode === 'manual'` 渲染分支与表单选项~~ **已于 2026-09-02 注释**（hint 同步更新）；② `webapp/src/components/AuthPanel.jsx`（随搁置未启用，手动状态/函数/UI 已注释，保留状态展示）；③ `pobi_v2/engine/preauth.py` 的 `ManualAuthSession` 及相关函数注释块；④ `tests/test_preauth.py` 的 `test_manual_session_ttl_*` 注释用例。
  - **决策点**：永久放弃手动分支 → 删除 ①②③④；计划重启（MFA / 验证码场景）→ 保留 ③ 作设计参考，仅清理 ①② 死代码，且重启前须先解决多 worker 下进程内注册表失效问题。

## 外部项目借鉴分析（ARTEX，2026-09-01）

> 对象：`github.com/Autumn-27/ARTEX`（Go 单体 + Next.js + PostgreSQL，LLM 多 agent 自主渗透，1 planner + N worker）。
> 结论：**价值集中在「agent 间经验流转与探索过程建模」，不在其技术栈。** 以下为差异对照与候选落地项；实质启动 P1–P4 时再固化到 `constraints.md`。

### 架构差异对照

| 维度 | ARTEX | pobi_v2 | 差距性质 |
|---|---|---|---|
| 编排 | 1 planner + N worker goroutine，`claimNext` 领意图，图变更 debounce 唤醒 planner | 单 `DeadEndAgent`，supervisor 串行委派子 agent | 架构级 |
| 状态模型 | 双图：资产图（全局真值）+ 探索图（goal/intent/fact/finding/hint，靠 `spawns`/`derived_from`/`yields`/`proves` 连血缘）+ `exploration_anchors` 连两图 | 本地 sqlite recon 五表 + PG `*_agg` 聚合（事实平铺，无血缘） | 建模级 |
| 跨 worker 复用 | `search_all_worker_traces(q)` / `list_worker_traces` / `get_worker_trace(intent_id, step_ids)` 检索他人**执行过程**（自动排除自己） | `recon_facts`（结论）+ `memory/summaries`（摘要）+ `logs/requester.jsonl`（落盘但不可检索、不入模型上下文） | 能力缺口 |
| 多步攻击链 | planner 跨唤醒共享 **todolist**（DB 按任务常驻，前置 fact 满足才派下一步，已完成标 done） | ADaPT 会话内递归；续跑靠 `seed_from_pg` + `covered_block` 粗粒度跳过 | 能力缺口 |
| 覆盖度 | 资产覆盖图（力导向，已测高亮）+ 可量化 | `/recon/coverage`、`/recon/endpoints`、`/recon/facts`、`/recon/summary`、`/targets/{id}/assets` 接口已就绪，**前端 SPA 未渲染** | 前端补课 |
| 流量留痕 | 独立 MITM 代理端口，worker 的 Bash/HTTP 全程留痕 + CA 验证 | 仅 requester 工具内记 `requester.jsonl`，Kali shell 发出的 HTTP 不在内 | 可选 |
| 执行护栏 | guard/intercept 审批门（README 未强调执行沙箱） | Docker 沙箱 + fail-closed 审批 + ScopePolicy | **pobi 更强，勿反向退化** |

### 候选落地项（按 ROI 排序）

- [ ] **P1 过程级 trace 检索（优先）**：直接命中「requester 抓页-重抓循环优化」的 R1/R3。
  - 痛点：任务 `535aee8e` 中有效观察散落在子 agent 的**过程**里——不够格进 `recon_facts`、也没进 memory 摘要，后续轮次只能重抓。
  - 方案：新增本地表 `recon_traces`（`task_id, agent, endpoint, seq, tool, input_digest, output_digest`）；`requester.jsonl` / `python_interpreter.jsonl` 已落盘，仅缺结构化入库 + 可检索；暴露只读工具 `recon_trace_search(q)` 挂 supervisor/requester。
  - 验证：重跑 DVWA 类任务，从任务创建到首次真实注入的工具调用轮数 < 10（与 R1–R3 同一验收口径）。
- [ ] **P2 plan todolist 持久化（成本最低）**：把 `plan_step` 事件从「事件展示」升级为持久化 `plan_steps` 表（`status: pending/ready/done` + `depends_on`），`seed_from_pg` / `seed_local_artifacts` 续跑时一并读回，替代粗粒度 `covered_block`。
  - 验证：任务取消后续跑，planner 能定位攻击链当前位置，不重复已完成步骤。
- [ ] ~~**P3 资产覆盖图前端（后端零改动）**~~：**2026-09-07 由「攻击流三视图」消化**（见「进行中」首条），不再单独立项。
- [ ] **P4 探索图血缘（需迁移，最后做）**：`recon_techniques` 加 `intent_id` / `parent_fact_id`，`findings` 落库带 `evidence_technique_ids`（新增 alembic 迁移）。**禁止**一上来照搬 ARTEX 的 5 类节点 + 4 类边完整模型。

### 待判断的架构选项

- [ ] **只读侦察阶段并行（可选）**：planner + N worker 并行领意图是两者最大差异，也是 pobi 慢的结构性原因；但 AVFS memory 命名空间、子 agent 子超时、审批 gate 均围绕单链路设计，整体改造风险高。按分层迭代原则**仅取低风险子集**：只读侦察（fingerprint / 子域枚举 / 目录扫描）走 `asyncio.gather`，只写 recon 库、无审批无副作用；exploiting 保持串行。

### 明确不采纳

- **Go 单体 + `go:embed` 单二进制**：`pobi_agent` 内核绑死 Python 生态（litellm / playwright / instructor），换语言不可行。
- **启动时幂等跑 `schema.sql`（重启即迁移）**：省事但丢迁移可追溯性，已有 0014→0018 alembic 链，不退。仅可对齐其「升级只换程序不动数据」的文档口径（`setup.sh`）。
- **无沙箱直连 Kali/Bash**：pobi 的 Docker 沙箱 + fail-closed 审批更严格，保持现状。

### 对既有待办的交叉影响

- 「requester 抓页-重抓循环优化」R1–R3：P1 提供第三条更省的路——**不改工具输出格式，先让过程可检索**。
- 「自定义工具添加」T1 选型：ARTEX 用目录式 `skills/`（声明 + 脚本），对「用户自定义」场景门槛低于代码注册，T1 选型时优先评估。

## 方案取舍备忘（待固化 constraints.md）
- M9 模型路由需保持 token 用量真实、价格按内置表估算；本地 `ContentPolicyViolationError` 不入 payload Agent
- 生产 CORS 须收敛为具体 origin，解除 `*`+credentials 同用风险

## 已解除的阻塞项
- [x] ~~**BUG：PG 侦察同步 upsert 失败（JSON vs JSONB 操作符不匹配）**~~ **（2026-08-31 已解除）**
  - 原根因：`recon_models.py` 的 `source_tasks` 为 `JSON` 类型，`upsert_to_pg` 用 `||` 追加时报
    `operator does not exist: json || json`。
  - 实际落地方案为**方案 B**（非原推荐的方案 A）：`store.py:816`（ReconFactAgg）与 `store.py:839`
    （ReconThreatAgg）改为冲突时以 `stmt.excluded.source_tasks` 整体覆盖，不再做数组追加。
    行收敛（同一事实唯一一行）仍成立，代价是不累计历史 `source_tasks`。
  - **当前状态**：阻塞已解除，`recon_*_agg` 可正常落库。若后续需要跨任务来源累计，再走方案 A
    （`JSON` → `JSONB` + 新迁移）并恢复 `||` 追加。

## 阻塞项
- 无明确阻塞（截至 2026-08）。历史监控报告 A–D 的基建阻塞均已修复（事件可观测性 / 认证死循环 / 结构性受阻 / 协作式取消 / AVFS 命名空间 / function-call 序列化）。
