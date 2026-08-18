# 演进路线图（EVOLUTION ROADMAP）

> 维护约定：本文是项目各阶段技术选型的「决策记录 + 当前状态 + 后续路线」单一来源。
> 每个阶段标注 `[已落地]` / `[进行中]` / `[待启动]`，落地后只改状态与结论，不整篇重写。
> 最后更新：2026-08-18

---

## 阶段 1：授权目标（Target）模型落地  `[已落地]`

**决策**：按「授权目标」维度管理靶场/Web 资产（URL、范围、认证画像），任务挂载到目标。

**结论**：`Target` 模型 + 前端目标管理已实现；认证从「单 AuthenticatorAgent」演进为 `AuthResolver` + `auth_profiles`（支持 LDAP / 表单 / Cookie / 免认证等 9 类画像，见 `docs/PROJECT_GOAL.md` §认证画像体系）。

---

## 阶段 2：全面容器化沙箱（DooD）  `[已落地]`

**决策**：废弃「每任务 `--network host` 临时建 Kali 容器」模式，改为 docker-compose 常驻 `pobi_kali` service，API/Worker 经 DooD（挂载 `/var/run/docker.sock`）复用同一容器，按容器名 `pobi_kali` 解析复用。

**结论**：`sandbox.py` 已按容器名复用常驻沙箱，跨任务状态隔离由 `tasks/<task_id>/` 卷与 `task_root` 注入保证。配套：
- `Dockerfile.prod` 强制多阶段构建（AGENTS.md 红线）。
- `docker-compose.yml` 全部镜像接入 `${ACR_REGISTRY:-}` 前缀（含 Kali）。

---

## 阶段 3：产物目录统一归口（tasks/<task_id>/ 单级）  `[已落地]`

**决策**：内核仅持有 `task_id`（= `session_id`），所有任务级产物统一归口 `TASKS_ROOT/<task_id>/`，不再感知授权目标 slug。

**结论**：见 `docs/检查重构后的目录架构.md`。6 大散落点（SessionMetrics / python_interpreter / requester / ContextEngine / AuthManifestResolver / Playwright storage_state）全部 `get_task_root()` 优先归口；`ScanWorkflow` fallback 路径与 `probe_runner` 均已注入 `task_root`；`scope.<task_id>.yaml` 与 `validation.<task_id>.yaml` 落 `task_root`。

---

## 阶段 4：验证策略 per-task 生效 + 下沉任务级  `[已落地]`

**决策**：
1. 验证阶段必须读取 per-task 配置 `tasks/<task_id>/validation.<task_id>.yaml`（写而不读问题修复）；
2. 验证策略配置从「授权目标级」下沉到「任务级」，让同目标的不同任务可配置不同策略（CTF 用 flag、真实目标用 judge）。

**结论**：
- `ValidationGate` 改为 **lazy 模式**：首次 `check()` 经 `resolve_validation_config()` 按 `get_task_root()` 解析当前任务配置并缓存（`docs/VALIDATION_PER_TASK_ISSUE.md` 已落地）。
- `Task` 新增 `flag_regex` / `validation_format` / `confidence_threshold` / `max_tree_depth`（可空，回退目标级/默认），迁移 `0012_task_validation`（`docs/VALIDATION_REAL_TARGET_ISSUE.md` 已落地）。
- 当前 `validation_type` 仍由 `_write_validation_config` 按「flag 有无」推导（`"flag"` / `"security assessment"`）。

**待办（方案 B，部分已落地）**：
- ~~验证语义由 `flag_regex` 非空隐式推断 → 改为显式 `is_range` 开关（靶场必填 flag 正则，真实目标 judge-only）。已落地：迁移 `0013_task_is_range` + `TaskCreate/Update` 条件校验 + 前端勾选框。~~
- 仍待：`validation_type` 从「flag 有无」改为任务级 `preset`（如 `vuln`）；补 `judge.instructions.jinja2` 的 `vuln` 分支与 `_JudgeOutput.confirmed_vulns`，实现「真实漏洞确认」语义。

---

## 阶段 5：扫描完成判定语义（真实目标场景）  `[进行中 / 部分]`

**决策**：对非 flag 的 Web 目标，需要明确的「扫描完成」判据——而非含糊的「找漏洞」，也非「跑满 50 轮自然结束」。

**当前机制**（已落地，但语义有限）：
- `task.objective`（必填）作为 `root_goal` 注入 judge，是唯一验证标尺。
- 非 flag 目标只看 `JudgeAgentStrategy`：证据满足 objective → `ACHIEVED` → stop + 出报告；否则下一轮；跑满 `max_iterations=50`（supervisor）+ `max_depth=3`（ADaPT）自然结束。
- **未覆盖**：「找到一条或多条利用链」「尽可能找严重/高危漏洞」的多目标收敛；缺 wall-clock 熔断（`POBI_MAX_RUNTIME_SECONDS`）。

**待启动**：
- A：前端 `objective` 最小校验（缺失可验证成功判据时提示）。
- B：任务级 `preset=vuln` + judge 分支（见阶段 4 待办）。
- C：`executor.py` 加 `POBI_MAX_RUNTIME_SECONDS` wall-clock 熔断（默认 6h，超时强制 stop + 部分报告）。
- D：`_JudgeOutput` 增 `confirmed_vulns`，要求 judge 列具体漏洞与 POC。

---

## 阶段 6：认证链路（DVWA 多步登录）  `[已落地 / 监控]`

**决策**：用 `AuthResolver` + `auth_profiles` 替代已删除的 `AuthenticatorAgent`，支持多步/复杂登录态保持（Playwright 持久化页面 `_persistent_page`）。

**结论**：DVWA 认证死循环问题已通过「AuthResolver 智能探测 + 目标级 `auth_profiles` 显式声明登录步骤」解决。`_persistent_page` 初始化 Bug（旧 `检查重构后的目录架构.md` P0-1）已修复。

**监控项**：仍依赖用户/agent 在目标级正确配置 `auth_profiles`；`target_recon.auth_surface` 复用（见阶段 7）可进一步规避重复探测。

---

## 阶段 7：威胁建模信息按 Target 维度存储（重跑复用）  `[设计稿 / 待启动]`

**决策**：把接口/请求响应/侦察指纹按 `target_id` 结构化落库，重跑同目标可跳过重复侦察。

**结论**：设计稿见 `docs/THREAT_MODEL_STORAGE_DESIGN.md`（未落地代码）。现有 `MemoryAgent`(AVFS) 是任务级、非结构化、不可见、不自动复用；本设计补目标级、结构化、可复用资产库，二者互补。

**待启动**：三表 `target_assets` / `target_requests` / `target_recon` + `event_bus` 采集分支 + 重跑注入。

---

## 阶段 8：前端任务管理与实时性  `[已落地 / 演进中]`

**决策**：前端从「仅报告查看」演进为「目标/任务全生命周期管理 + 实时日志」。

**结论**：`web/static/js/app.js` 已支持目标 CRUD、任务创建（含任务级验证策略区块）、实时事件流。手动取消与重跑已可用（`docs/TASK_MANUAL_CANCEL_ISSUES.md` 记录部分解决与残余缺口）。

**待启动**：`active_tasks` 全局视图；重跑时预填上轮表单；事件流补全（部分子 Agent 仍走非事件总线路径）。

---

## 仓库与发布

- 仓库名已确定为 `pobi_v2`（旧「待定」记录已过期）。
- 发布遵循 AGENTS.md：改完同步源码 → 问是否更新 `version.txt` 并 commit → push 前问是否打 `v*` tag → 部署前确认内网镜像就绪。
