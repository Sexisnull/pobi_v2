#!/usr/bin/env python3
"""Agent 执行监控与 API/Worker 差异对比脚本（dev docker 环境）。

用途：
  通过 API（PAT 鉴权）驱动并观测一个渗透测试任务，同时并行采集三个数据源：
    1. API 侧展示内容      —— 轮询 /live 与 /plan（这是"人通过界面/接口能看到的内容"）
    2. Redis 全量事件流     —— 订阅 pobi_v2:events:{task_id}（agent 真实"在做什么"的明细，
                              含 agent_thought / llm_response / llm_input 等仅经 SSE 出现、
                              不落库的事件）
    3. Worker 容器日志      —— docker logs -f pobi_v2-worker-1（运行期错误/异常/堆栈）
  任务终态后生成差异报告，重点记录：
    - API 实际展示了什么、缺失了什么（"看 agent 在做什么"的鸿沟）
    - 执行过程中的错误与异常（三源汇总）

依赖：仅标准库 + 本地 docker 命令行（docker compose 容器已启动）。
配置：API base=http://127.0.0.1:8000，容器名 pobi_v2-worker-1 / pobi_v2-redis-1。

用法：
  python3 scripts/monitor_agent.py                       # 新建任务并监控
  python3 scripts/monitor_agent.py --task-id <UUID>      # 监控一个已存在的任务
  python3 scripts/monitor_agent.py --target-id <UUID> --objective "..." --max-turns 20
  python3 scripts/monitor_agent.py --timeout 600         # 最长监控秒数（默认 900）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone

# ───────────────────────────── 配置 ─────────────────────────────
API_BASE = "http://127.0.0.1:8000"
API_TOKEN = "pk_3758d20e_3gw3hnJ0YWmQYb0sa11k3FmtNOUNZRkOdKsV1Q1Otv9TJ4Pk"
WORKER_CONTAINER = "pobi_v2-worker-1"
REDIS_CONTAINER = "pobi_v2-redis-1"
EVENT_CHANNEL_PREFIX = "pobi_v2:events:"

# 落库（API /plan /live 可查）的事件类型；其余仅经 SSE/redis 实时流出现一次。
PERSISTED_EVENT_TYPES = {
    "plan_step", "phase_changed", "agent_start", "agent_end",
    "tool_call_start", "tool_call_end", "report_task_event",
}
# 这些事件描述的是"agent 正在/已经做了什么"，是观测性的核心，却不在 API 落库里。
RICH_DETAIL_EVENT_TYPES = {
    "agent_thought", "llm_response", "llm_input", "agent_routed",
    "validation_result", "confidence_update", "llm_iteration", "log",
    "task_created", "task_expanded", "task_status_changed", "agent_error",
}


# ───────────────────────────── 工具函数 ─────────────────────────────
def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def api_get(path: str) -> tuple[int, dict]:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, {"_error": body}
    except Exception as e:  # noqa: BLE001
        return -1, {"_error": str(e)}


def api_post(path: str, payload: dict) -> tuple[int, dict]:
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json",
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, {"_error": body}
    except Exception as e:  # noqa: BLE001
        return -1, {"_error": str(e)}


def docker_exec_redis(subscribe_channel: str, out_lines: list, stop_evt: threading.Event) -> None:
    """在 redis 容器内用 redis-cli 订阅事件频道，逐行写入 out_lines（线程安全由调用方负责）。"""
    cmd = [
        "docker", "exec", "-i", REDIS_CONTAINER,
        "redis-cli", "subscribe", subscribe_channel,
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
    except Exception as e:  # noqa: BLE001
        log(f"[redis] 启动订阅失败: {e}")
        return
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                out_lines.append(("redis", line))
            if stop_evt.is_set():
                break
    except Exception as e:  # noqa: BLE001
        out_lines.append(("redis", f"__ERR__ {e}"))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def docker_follow_worker(task_id: str, out_lines: list, stop_evt: threading.Event) -> None:
    """docker logs -f 跟踪 worker 日志；只保留该任务相关行 + 所有 error/traceback 行。"""
    cmd = ["docker", "logs", "-f", WORKER_CONTAINER]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except Exception as e:  # noqa: BLE001
        log(f"[worker] 启动日志跟踪失败: {e}")
        return
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            low = line.lower()
            relevant = (task_id[:8] in line) or ("error" in low) or ("traceback" in low) \
                or ("exception" in low) or ("critical" in low)
            if relevant and line:
                out_lines.append(("worker", line))
            if stop_evt.is_set():
                break
    except Exception as e:  # noqa: BLE001
        out_lines.append(("worker", f"__ERR__ {e}"))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


# ───────────────────────────── 主流程 ─────────────────────────────
def parse_redis_message(raw: str) -> dict | None:
    """解析 redis-cli subscribe 输出：形如
       message\r\n<channel>\r\n<json>  三行一组。
    我们用简单状态机从 out_lines 提取，这里处理单行 JSON。
    """
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="监控 agent 执行并对比 API/Worker 差异")
    parser.add_argument("--task-id", default=None,
                        help="【监控模式】传入已存在的任务 ID，脚本只监控不创建。必填。")
    parser.add_argument("--timeout", type=int, default=1800, help="最长监控秒数（默认 30 分钟）")
    parser.add_argument("--poll-interval", type=int, default=5, help="API 轮询间隔秒")
    args = parser.parse_args()

    # 1) 取得 task_id（监控模式：必须手动提供已存在的任务）
    if not args.task_id:
        log("[错误] 请通过 --task-id 传入你要监控的已存在任务 ID。本脚本不自动创建任务。")
        log("用法： python3 scripts/monitor_agent.py --task-id <UUID> [--timeout 600]")
        return 2
    task_id = args.task_id
    log(f"监控模式：复用已有任务 {task_id}")
    st, task = api_get(f"/api/v1/tasks/{task_id}")
    if st != 200:
        log(f"[错误] 获取任务失败 HTTP {st}: {task}")
        return 1

    channel = EVENT_CHANNEL_PREFIX + task_id

    # 2) 启动三个数据源采集线程
    stop_evt = threading.Event()
    redis_lines: list[tuple[str, str]] = []
    worker_lines: list[tuple[str, str]] = []
    redis_thread = threading.Thread(
        target=docker_exec_redis, args=(channel, redis_lines, stop_evt), daemon=True)
    worker_thread = threading.Thread(
        target=docker_follow_worker, args=(task_id, worker_lines, stop_evt), daemon=True)
    redis_thread.start()
    worker_thread.start()
    log(f"已开始采集：redis 频道 {channel} + worker 日志 {WORKER_CONTAINER}")

    # 3) 轮询 API，并缓冲 redis 全量事件
    start = time.time()
    api_snapshots: list[tuple[str, dict]] = []          # (ts, live_state)
    api_plan_snapshots: list[tuple[str, dict]] = []     # (ts, plan)
    redis_events: list[dict] = []                        # 全量事件（去 redis-cli 包装）
    redis_counter: Counter = Counter()                  # 各事件类型计数
    api_event_types_seen: set = set()                   # API /live 暴露的事件类型
    errors: list[tuple[str, str]] = []                  # (source, text)
    last_status = None

    # 把 redis_lines 定期并入 redis_events
    def drain_redis() -> None:
        while redis_lines:
            tag, line = redis_lines.pop(0)
            if tag == "redis":
                ev = parse_redis_message(line)
                if ev and isinstance(ev, dict) and ev.get("type"):
                    redis_events.append(ev)
                    redis_counter[ev["type"]] += 1
                elif line.startswith("__ERR__"):
                    errors.append(("redis", line))

    log(f"开始轮询 API（每 {args.poll_interval}s），最长 {args.timeout}s ...")
    try:
        while time.time() - start < args.timeout:
            drain_redis()
            st, live = api_get(f"/api/v1/tasks/{task_id}/live")
            if st == 200:
                ts = datetime.now().strftime("%H:%M:%S")
                api_snapshots.append((ts, live))
                for ev in live.get("recent_events", []):
                    api_event_types_seen.add(ev.get("type"))
                cur = live.get("status")
                if cur != last_status:
                    log(f"[API] status -> {cur}  (phase={live.get('current_phase')}, "
                        f"agent={live.get('current_agent')})")
                    last_status = cur
                if cur in ("completed", "failed", "cancelled"):
                    log(f"[API] 任务进入终态 {cur}，再采集 8s 后结束")
                    time.sleep(8)
                    break
            else:
                log(f"[API] /live HTTP {st}: {live}")
                if st == 401:
                    errors.append(("api", f"/live 鉴权失败: {live}"))
                    break
            # plan 接口
            st2, plan = api_get(f"/api/v1/tasks/{task_id}/plan")
            if st2 == 200:
                api_plan_snapshots.append((datetime.now().strftime("%H:%M:%S"), plan))
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        log("用户中断")
    finally:
        drain_redis()
        stop_evt.set()
        time.sleep(1)

    # 4) 生成差异报告
    generate_report(task_id, task, api_snapshots, api_plan_snapshots,
                    redis_events, redis_counter, api_event_types_seen,
                    worker_lines, errors)
    return 0


def generate_report(task_id, task, api_snaps, api_plans, redis_events,
                    redis_counter, api_event_types_seen, worker_lines, errors):
    out_path = f"scripts/monitor_report_{task_id}.md"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = []
    w = lines.append

    w(f"# Agent 执行监控对比报告")
    w("")
    w(f"- 生成时间：{now}")
    w(f"- 任务 ID：`{task_id}`")
    w(f"- 任务名：{task.get('name')}")
    w(f"- 目标：{task.get('objective')}")
    w(f"- 运行模式：{task.get('agent_mode')} / max_turns={task.get('max_turns')}")
    w(f"- 最终状态：{task.get('status')}")
    w(f"- 监控脚本：scripts/monitor_agent.py（dev docker 环境）")
    w("")

    # ── 数据源概览 ──
    w("## 1. 三数据源采集概览")
    w("")
    w("| 数据源 | 内容 | 采集量 |")
    w("|---|---|---|")
    w(f"| API `/live` 轮询 | 控制台实际展示给人的内容 | {len(api_snaps)} 次快照 |")
    w(f"| API `/plan` 轮询 | 执行计划步骤 | {len(api_plans)} 次快照 |")
    w(f"| Redis 事件流 `{EVENT_CHANNEL_PREFIX}{task_id}` | agent 真实运行事件全量 | {len(redis_events)} 条 |")
    w(f"| Worker 日志 | 运行期错误/异常 | {len(worker_lines)} 行 |")
    w("")

    # ── 核心差异：事件类型覆盖 ──
    w("## 2. 核心差异：API 展示了什么 vs Agent 实际在做什么")
    w("")
    w("> 结论速览：API（/live、/plan）只能展示**落库事件**；"
      "而描述\"agent 思考/LLM 输入输出/路由决策\"的明细事件仅经 Redis/SSE 实时流出现一次，**不落库**，"
      "因此 API 接口与控制台看不到。下面按事件类型量化差异。")
    w("")
    w("| 事件类型 | 是否落库(API可见) | Redis 流捕获数 | 说明 |")
    w("|---|---|---|---|")
    all_types = sorted(set(redis_counter) | PERSISTED_EVENT_TYPES | api_event_types_seen)
    for t in all_types:
        persisted = "✅ 是" if t in PERSISTED_EVENT_TYPES else "❌ 否（仅实时流）"
        cnt = redis_counter.get(t, 0)
        note = ""
        if t in RICH_DETAIL_EVENT_TYPES:
            note = "观测核心：API 缺失"
        elif t in ("agent_start", "agent_end"):
            note = "运行视图可见，但无思考内容"
        elif t == "tool_call_start":
            note = "API 仅含工具名+截断参数(200字)"
        elif t == "tool_call_end":
            note = "API 仅含结果前200字"
        w(f"| `{t}` | {persisted} | {cnt} | {note} |")
    w("")

    # API 看到的事件类型 vs redis 全量
    api_missing = set(redis_counter) - api_event_types_seen - PERSISTED_EVENT_TYPES
    w(f"**API `/live` 暴露的事件类型（{len(api_event_types_seen)} 种）**："
      f"{', '.join(sorted(api_event_types_seen)) or '（无）'}")
    w("")
    w(f"**Redis 全量中存在、但 API 接口/控制台完全看不到的事件类型（{len(api_missing)} 种）**：")
    if api_missing:
        for t in sorted(api_missing):
            w(f"- `{t}` —— 共 {redis_counter.get(t, 0)} 条（如 agent_thought 思考、"
              f"llm_response 模型输出等）")
    else:
        w("- （无，本次运行中 redis 流未出现额外类型，可能任务偏短）")
    w("")

    # ── 关键明细样本：API 缺失的"在做什么" ──
    w("## 3. Agent 真实在做什么（API 缺失的明细样本）")
    w("")
    w("以下来自 Redis 全量事件流，API 接口无法返回，是\"看 agent 在做什么\"的主要鸿沟：")
    w("")
    samples = defaultdict(list)
    for ev in redis_events:
        t = ev.get("type")
        if t in RICH_DETAIL_EVENT_TYPES:
            samples[t].append(ev)
    if not samples:
        w("_（本次运行未捕获到 rich 明细事件，可能 agent 尚未进入思考/LLM 调用阶段）_")
    else:
        for t in ["agent_thought", "llm_response", "llm_input", "agent_routed",
                  "validation_result", "confidence_update", "llm_iteration", "log"]:
            evs = samples.get(t, [])
            if not evs:
                continue
            w(f"### `{t}`（{len(evs)} 条，API 不可见）")
            w("")
            for ev in evs[:8]:
                p = {k: v for k, v in ev.items() if k not in ("type", "session_id")}
                txt = json.dumps(p, ensure_ascii=False, default=str)
                if len(txt) > 500:
                    txt = txt[:500] + "…"
                w(f"- {txt}")
            if len(evs) > 8:
                w(f"- …（其余 {len(evs) - 8} 条省略）")
            w("")

    # ── 工具调用对比：API 截断 vs 全量 ──
    w("## 4. 工具调用：API 截断 vs 全量")
    w("")
    tool_starts = [e for e in redis_events if e.get("type") == "tool_call_start"]
    tool_ends = [e for e in redis_events if e.get("type") == "tool_call_end"]
    w(f"Redis 流记录工具调用：start {len(tool_starts)} 次 / end {len(tool_ends)} 次。")
    w("API `/live` 的 `agent_work` 仅保留每个 agent 最近若干条、参数/结果截断至 200 字，"
      "且不保证覆盖全部调用。")
    w("")
    if tool_starts:
        w("| # | agent | tool | 参数(前120字, 全量) |")
        w("|---|---|---|---|")
        for i, e in enumerate(tool_starts[:20], 1):
            args_txt = str(e.get("args", ""))[:120]
            w(f"| {i} | {e.get('agent_name')} | {e.get('tool_name')} | {args_txt} |")
        if len(tool_starts) > 20:
            w(f"| … | | | 其余 {len(tool_starts) - 20} 次省略 |")
    w("")

    # ── 错误与异常 ──
    w("## 5. 执行过程中的错误与异常")
    w("")
    # worker 日志里挑 error/traceback
    err_worker = [ln for _, ln in worker_lines if any(
        k in ln.lower() for k in ("error", "traceback", "exception", "critical", "__err__"))]
    agent_err_events = [e for e in redis_events if e.get("type") == "agent_error"]
    api_err = [e for e in errors if e[0] == "api"]
    if not err_worker and not agent_err_events and not api_err:
        w("_（未捕获到明显错误/异常）_")
    else:
        if agent_err_events:
            w(f"### agent_error 事件（{len(agent_err_events)} 条，来自 Redis 流）")
            for e in agent_err_events:
                w(f"- {e.get('agent_name')}: {e.get('error_type')} - "
                  f"{str(e.get('error_message'))[:300]}")
            w("")
        if err_worker:
            w(f"### Worker 日志异常（{len(err_worker)} 行）")
            for ln in err_worker[:40]:
                w(f"```\n{ln}\n```")
            w("")
        if api_err:
            w(f"### API 调用错误（{len(api_err)} 条）")
            for src, txt in api_err:
                w(f"- {src}: {txt[:300]}")
            w("")

    # ── 完整 Redis 事件时间线（末尾） ──
    w("## 6. 完整事件时间线（Redis 全量，按捕获顺序）")
    w("")
    if redis_events:
        w("| # | 类型 | agent/tool | 摘要 |")
        w("|---|---|---|---|")
        for i, e in enumerate(redis_events, 1):
            t = e.get("type")
            who = e.get("agent_name") or e.get("tool_name") or e.get("selected_agent") or ""
            summary = ""
            for key in ("thought", "response_text", "summary", "reasoning",
                        "new_phase", "status", "error_type", "new_status"):
                if key in e:
                    summary = str(e[key])[:80]
                    break
            w(f"| {i} | `{t}` | {who} | {summary} |")
    else:
        w("_（未捕获到 Redis 事件，可能订阅启动晚于任务开始，或任务未真正运行）_")
    w("")

    w("## 7. 结论与建议")
    w("")
    w("1. **API 展示内容确实偏少**：/live、/plan 只能呈现落库的 7 类事件"
      "（plan_step/phase_changed/agent_start/agent_end/tool_call_start/tool_call_end/report_task_event），"
      "而 agent_thought、llm_response、llm_input、agent_routed、validation_result、confidence_update、"
      "llm_iteration、log 等\"在做什么\"的明细事件不落库，API 与控制台完全不可见。")
    w("2. **实时流是唯一完整来源**：Redis 频道 `pobi_v2:events:{task_id}`（即 SSE 后端）"
      "承载全部事件，但只在连接期间推送一次，断连即丢失，无法回看。")
    w("3. **建议**：若要让\"人看到 agent 在做什么\"，应在 `persist_event_worker` 中扩围落库"
      "（至少 agent_thought / llm_response / llm_input / agent_routed / validation_result），"
      "或新增一个\"实时事件回放\"接口从 Redis 流/新表读取历史。")
    w("")

    content = "\n".join(lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"报告已写入 {out_path}（{len(content)} 字节）")
    # 终端摘要
    log(f"── 摘要：API快照 {len(api_snaps)} 次 / Redis事件 {len(redis_events)} 条 / "
        f"Worker异常行 {len(err_worker)} / agent_error {len(agent_err_events)} ──")


if __name__ == "__main__":
    sys.exit(main())
