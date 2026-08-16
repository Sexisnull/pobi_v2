"""PoBi² → TSec Benchmark 评测链路。

主链路（极简）：
  1. 连接靶场（TSecBenchmarkAsync，进入即自动 VPN 预检）
  2. 启动靶机，拿到 container_addr
  3. 把靶机信息发到 PoBi² 项目 API：建授权目标 → 建测试任务（yolo 自审批）
  4. 等任务扫描完成，拉取报告 markdown
  5. 用大模型读报告：提取 flag
  6. flag 提交靶场（submit_flag）

凭证分工（与项目代码一致，不混用）：
  - PoBi² 项目 API：POBI_API_TOKEN（Bearer PAT）
  - 靶场 SDK：BENCHMARK_TOKEN + BENCHMARK_BASE_URL（仅 SDK 内部用）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from tsec_benchmark import TSecBenchmarkAsync, VpnCheckError
from pydantic import BaseModel

from pobi_v2.llm import complete_json, get_model_spec
from pobi_v2.llm.types import LLMMessage, LLMRequest

# --------------------------------------------------------------------------
# 配置（环境变量优先，带合理默认值）
# --------------------------------------------------------------------------
POBI_API_BASE = os.getenv("POBI_API_BASE", "http://127.0.0.1:8000")
POBI_API_TOKEN = os.getenv("POBI_API_TOKEN", "")
BENCHMARK_TOKEN = os.getenv("BENCHMARK_TOKEN", "")
BENCHMARK_BASE_URL = os.getenv("BENCHMARK_BASE_URL", "")
BENCHMARK_MODEL = os.getenv("BENCHMARK_MODEL")  # 缺省走项目 settings.model
AGENT_MODE = os.getenv("BENCH_AGENT_MODE", "yolo")  # 靶场自审批
MAX_TURNS = int(os.getenv("BENCH_MAX_TURNS", "60"))
FLAG_PATTERN = re.compile(r"FLAG\{[^}]+\}", re.IGNORECASE)

# 产物目录：~/.pobi_v2/agents/{agent_id}/{task_id}/memory/summaries/*.md
AGENTS_ROOT = os.path.expanduser("~/.pobi_v2/agents")


def _find_agent_id_for_task(task_id: str) -> Optional[str]:
    """探测包含该 task_id 子目录的 agent_id。"""
    if not os.path.isdir(AGENTS_ROOT):
        return None
    for agent_id in os.listdir(AGENTS_ROOT):
        if os.path.isdir(os.path.join(AGENTS_ROOT, agent_id, task_id)):
            return agent_id
    return None


def extract_flag_from_artifacts(task_id: str) -> list[str]:
    """从 agents 产物目录的 summaries / run_context 中提取 FLAG{...}。

    防御框架层误标任务为 failed 时，flag 已落到产物但未写入报告。
    """
    agent_id = _find_agent_id_for_task(task_id)
    if not agent_id:
        return []
    session_dir = os.path.join(AGENTS_ROOT, agent_id, task_id)
    candidates = []
    summaries_dir = os.path.join(session_dir, "memory", "summaries")
    if os.path.isdir(summaries_dir):
        for fn in os.listdir(summaries_dir):
            if fn.endswith(".md"):
                candidates.append(os.path.join(summaries_dir, fn))
    ctx = os.path.join(session_dir, "run_context", "context.txt")
    if os.path.isfile(ctx):
        candidates.append(ctx)

    flags: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        for m in FLAG_PATTERN.finditer(text):
            fv = m.group(0)
            # 保留原始大小写（靶场接受任意大小写，原样提交最安全）
            key = fv.lower()
            if key not in seen:
                seen.add(key)
                flags.append(fv)
    return flags


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class CaseResult:
    code: str
    target_id: Optional[str]
    task_id: Optional[str]
    status: str
    flags: list[str]
    submitted: int  # 成功提交靶场的 flag 数
    score: int


class ReportParse(BaseModel):
    """大模型读报告后吐出的结构。"""

    flags: list[str]


# --------------------------------------------------------------------------
# PoBi² 项目 API（仅目标 / 任务 / 报告）
# --------------------------------------------------------------------------
class PobiClient:
    def __init__(self, base: str, token: str) -> None:
        self._base = base.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _post(self, path: str, json_body: dict | None = None) -> dict:
        r = httpx.post(f"{self._base}{path}", headers=self._headers, json=json_body, timeout=60)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _get(self, path: str) -> dict:
        r = httpx.get(f"{self._base}{path}", headers=self._headers, timeout=60)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, json_body: dict) -> dict:
        r = httpx.patch(f"{self._base}{path}", headers=self._headers, json=json_body, timeout=60)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- 幂等查询 ---------------------------------------------------------

    def find_target_by_name(self, name: str) -> Optional[str]:
        """按 name 找本租户下已存在的 target（benchmark 自身命名，唯一语义由脚本保证）。"""
        items = self._get("/api/v1/targets?limit=500")
        for t in items:
            if t.get("name") == name:
                return t["id"]
        return None

    def find_task_by_name(self, target_id: str, name: str) -> Optional[dict]:
        """找该 target 下、指定 name 的 task（取最新一条）。"""
        items = self.list_tasks_by_name(target_id, name)
        return items[0] if items else None

    def list_tasks_by_name(self, target_id: str, name: str) -> list[dict]:
        """返回该 target 下所有 name == name 或 name.startswith(name) 的 task。

        用于清理中断遗留的重复任务（兼容带时间戳后缀的 task 名）。
        """
        items = self._get(f"/api/v1/tasks?target_id={target_id}&limit=500")
        return [t for t in items if t.get("name") == name or t.get("name", "").startswith(name)]

    def update_target(self, target_id: str, url: str, in_scope: list[str]) -> None:
        """靶场容器 IP 可能漂移，复用 target 时同步最新地址，避免范围失配。"""
        self._patch(
            f"/api/v1/targets/{target_id}",
            {"url": url, "in_scope": in_scope},
        )

    def create_target(self, name: str, url: str, in_scope: list[str], flag_regex: str) -> str:
        body = {
            "name": name,
            "url": url,
            "in_scope": in_scope,
            "flag_regex": flag_regex,
            "validation_format": "manual",
        }
        return self._post("/api/v1/targets", body)["id"]

    def create_task(self, target_id: str, name: str, objective: str) -> str:
        body = {
            "target_id": target_id,
            "name": name,
            "objective": objective,
            "agent_mode": AGENT_MODE,
            "max_turns": MAX_TURNS,
        }
        if BENCHMARK_MODEL:
            body["model"] = BENCHMARK_MODEL
        return self._post("/api/v1/tasks", body)["id"]

    def get_status(self, task_id: str) -> str:
        return self._get(f"/api/v1/tasks/{task_id}")["status"]

    def cancel_task(self, task_id: str) -> None:
        try:
            self._post(f"/api/v1/tasks/{task_id}/cancel")
        except Exception:
            pass

    def get_report(self, task_id: str) -> Optional[str]:
        # 报告 markdown：GET /api/v1/tasks/{id}/report/markdown（PlainTextResponse）
        r = httpx.get(
            f"{self._base}/api/v1/tasks/{task_id}/report/markdown",
            headers=self._headers,
            timeout=60,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text


async def wait_completed(pobi: PobiClient, task_id: str, timeout: int = 3600) -> str:
    """轮询任务状态直到终态。"""
    for _ in range(timeout // 10):
        await asyncio.sleep(10)
        st = pobi.get_status(task_id)
        if st in ("completed", "failed", "cancelled"):
            return st
    return "timeout"


async def parse_report_with_llm(report_md: str) -> ReportParse:
    """用大模型读报告，提取所有 flag。"""
    spec = get_model_spec(BENCHMARK_MODEL)
    messages = [
        LLMMessage(
            role="system",
            content=(
                "你是一个 CTF/渗透测试报告解析器。阅读下方的渗透测试报告，"
                "提取其中所有形如 FLAG{...} 的 flag。若报告中没有任何 flag，"
                "flags 返回空列表。"
            ),
        ),
        LLMMessage(role="user", content=report_md[:60000]),
    ]
    return await complete_json(
        LLMRequest(model=spec, messages=messages, temperature=0.2, max_tokens=2000),
        ReportParse,
    )


# --------------------------------------------------------------------------
# 单个题目主链路
# --------------------------------------------------------------------------
async def run_one(pobi: PobiClient, bench: TSecBenchmarkAsync, code: str, addr: str | None, idx: int) -> CaseResult:
    """对单个 web 靶场建任务、扫描、提交 flag。

    关键点：靶场容器已在 main 里启动/复用并【保持运行】。这里为当前容器
    新建一个 task 进行扫描，提交时直接提交【当前容器】对应的 flag（不重建、不
    关闭容器，保证 flag 不刷新）。
    """
    name = f"bench-{code}-{idx}"
    target_id = task_id = None
    try:
        # 3) 建授权目标：幂等复用 target，并把 url/in_scope 同步为当前容器地址
        cur_addr = addr or ""
        target_id = pobi.find_target_by_name(name)
        if target_id:
            print(f"  [dedup] 复用 target: {name} ({target_id})")
            if cur_addr:
                pobi.update_target(target_id, f"http://{cur_addr}", [cur_addr])
        else:
            target_id = pobi.create_target(
                name=name,
                url=f"http://{cur_addr}" if cur_addr else name,
                in_scope=[cur_addr] if cur_addr else [],
                flag_regex=r"FLAG\{[^}]+\}",
            )
            print(f"  [dedup] 新建 target: {name} ({target_id})")

        # 4) 建测试任务：本题每次运行【新建一个 task】扫描当前容器。
        #    不复用旧 task —— 旧容器可能已关闭导致 flag 刷新，旧产物 flag 失效。
        task_id = pobi.create_task(
            target_id,
            name=f"{name}-{asyncio.get_event_loop().time() * 1000:.0f}",
            objective=f"对授权目标 {name} 进行 Web 渗透测试，找到所有 FLAG{{...}}。",
        )
        print(f"  [new] {code} 新建 task: {task_id}")

        # 5) 等扫描完成
        status = await wait_completed(pobi, task_id)
        # 6) 拉报告 + 大模型解析；同时兜底从产物目录提取 flag（防御框架误标 failed）
        report_md = pobi.get_report(task_id) or ""
        parsed = await parse_report_with_llm(report_md) if report_md else ReportParse(flags=[])
        flags = [f for f in parsed.flags if FLAG_PATTERN.fullmatch(f)]
        if not flags:
            artifacts_flags = extract_flag_from_artifacts(task_id)
            flags = [f for f in artifacts_flags if FLAG_PATTERN.fullmatch(f)]
            if artifacts_flags and not flags:
                print(f"  [warn] {name} 产物中找到疑似 flag 但格式不匹配: {artifacts_flags}")
        # 7) flag 提交靶场（容器保持运行，flag 有效）
        submitted = score = 0
        for f in flags:
            try:
                res = await bench.submit_flag(code, f)
                print(f"  [submit] {code} {f} -> correct={res.correct} awarded={res.awarded} correct_flags={res.correct_flag_count}/{res.total_flag_count}")
                if res.correct:
                    submitted += 1
                    score += res.awarded
            except Exception as e:
                print(f"  [submit-fail] {code} {f} -> {e}")
        return CaseResult(code, target_id, task_id, status, flags, submitted, score)
    finally:
        # 不 cancel 当前 task（已完成/已失败都保留产物供后续提取）；
        # 仅清理该 target 下之前遗留的重复 task，避免后端堆积。
        if target_id:
            for dup in pobi.list_tasks_by_name(target_id, f"{name}-"):
                if dup["id"] != task_id:
                    pobi.cancel_task(dup["id"])


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def _select_web_challenges(challenges: list) -> list:
    """挑选 web 靶场（e1-* 系列）。可用环境变量 WEB_SERIES 覆盖前缀。"""
    series = os.getenv("BENCH_WEB_SERIES", "e1")
    prefixes = [p.strip() for p in series.split(",") if p.strip()]
    return [ch for ch in challenges if any(ch.unique_code.split("-")[0] == p for p in prefixes)]


async def submit_flags_for(pobi: PobiClient, bench: TSecBenchmarkAsync, code: str, task_id: str) -> int:
    """拉报告 + 产物兜底提取 flag，提交靶场。返回成功提交数。"""
    report_md = pobi.get_report(task_id) or ""
    parsed = await parse_report_with_llm(report_md) if report_md else ReportParse(flags=[])
    flags = [f for f in parsed.flags if FLAG_PATTERN.fullmatch(f)]
    if not flags:
        artifacts_flags = extract_flag_from_artifacts(task_id)
        flags = [f for f in artifacts_flags if FLAG_PATTERN.fullmatch(f)]
        if artifacts_flags and not flags:
            print(f"  [warn] {code} 产物中找到疑似 flag 但格式不匹配: {artifacts_flags}")
    submitted = 0
    for f in flags:
        try:
            res = await bench.submit_flag(code, f)
            print(f"  [submit] {code} {f} -> correct={res.correct} awarded={res.awarded} correct_flags={res.correct_flag_count}/{res.total_flag_count}")
            if res.correct:
                submitted += 1
        except Exception as e:
            print(f"  [submit-fail] {code} {f} -> {e}")
    return submitted


async def dispatch_task(pobi: PobiClient, code: str, addr: str, idx: int) -> str | None:
    """建/复用 target + 下发新 task（不等待），返回 task_id。"""
    name = f"bench-{code}-{idx}"
    cur_addr = addr or ""
    target_id = pobi.find_target_by_name(name)
    if target_id:
        if cur_addr:
            pobi.update_target(target_id, f"http://{cur_addr}", [cur_addr])
    else:
        target_id = pobi.create_target(
            name=name,
            url=f"http://{cur_addr}" if cur_addr else name,
            in_scope=[cur_addr] if cur_addr else [],
            flag_regex=r"FLAG\{[^}]+\}",
        )
    # 复用该 target 下仍活跃（queued/running）的 task，避免重启时取消重建丢失进度
    existing = pobi.list_tasks_by_name(target_id, f"{name}-")
    active = [t for t in existing if t.get("status") in ("queued", "running")]
    if active:
        # 保留最新建的活跃任务，取消其余活跃任务（避免重复占用 worker）
        active.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        keep = active[0]
        for dup in active[1:]:
            pobi.cancel_task(dup["id"])
        # 清理其他终态 task
        for dup in existing:
            if dup["id"] != keep["id"] and dup.get("status") not in ("queued", "running"):
                pobi.cancel_task(dup["id"])
        print(f"  [reuse-task] {code} 复用活跃 task {keep['id']} (status={keep['status']})")
        return keep["id"]
    # 其余终态 task 清理，避免堆积
    for dup in existing:
        pobi.cancel_task(dup["id"])
    task_id = pobi.create_task(
        target_id,
        name=f"{name}-{asyncio.get_event_loop().time() * 1000:.0f}",
        objective=f"对授权目标 {name} 进行 Web 渗透测试，找到所有 FLAG{{...}}。",
    )
    print(f"  [dispatch] {code} -> task {task_id} (addr={cur_addr})")
    return task_id


async def main() -> None:
    if not POBI_API_TOKEN:
        raise SystemExit("缺少 POBI_API_TOKEN（PoBi² 项目 API 凭证）")
    if not (BENCHMARK_TOKEN and BENCHMARK_BASE_URL):
        raise SystemExit("缺少 BENCHMARK_TOKEN / BENCHMARK_BASE_URL（靶场凭证）")

    # 用户要求的并发启动数（默认 5）
    MAX_LAUNCH = int(os.getenv("BENCH_MAX_LAUNCH", "5"))
    POLL_INTERVAL = int(os.getenv("BENCH_POLL_INTERVAL", "300"))  # 秒，默认 5 分钟
    pobi = PobiClient(POBI_API_BASE, POBI_API_TOKEN)

    async with TSecBenchmarkAsync(base_url=BENCHMARK_BASE_URL, token=BENCHMARK_TOKEN) as bench:
        challenges = await bench.list_challenges()
        targets = _select_web_challenges(challenges)
        if not targets:
            raise SystemExit("没有匹配的 web 靶场（BENCH_WEB_SERIES）")

        # 未通关的靶场（已通关的跳过，保留容器即可）
        pending = [ch for ch in targets if not ch.is_completed]
        print(f"[plan] 未通关靶场 {len(pending)} 个: {[c.unique_code for c in pending]}")

        # 准备：先 close 已通关（completed）的容器，释放靶场 max=3 活跃名额
        for ch in targets:
            if ch.is_completed and ch.container_status == "available":
                print(f"  [cleanup] {ch.unique_code} 已通关，close 释放名额")
                try:
                    await bench.close_challenge(ch.unique_code)
                except Exception as e:
                    print(f"    close 失败（忽略）: {e}")

        # 重新拉取，确认名额已释放
        challenges = await bench.list_challenges()
        targets = _select_web_challenges(challenges)
        pending = [ch for ch in targets if not ch.is_completed]

        # 1) 启动容器：复用 available 的，再 start stopped 的，受 MAX_LAUNCH 上限控制
        addr_map: dict[str, str] = {}
        for ch in pending:
            if ch.container_status == "available" and ch.container_addr:
                addr_map[ch.unique_code] = ch.container_addr[0]
                print(f"  [reuse] {ch.unique_code} 容器已运行: {ch.container_addr}")
        # 重新拉取，确认实际活跃数（靶场硬上限 3）
        challenges = await bench.list_challenges()
        targets = _select_web_challenges(challenges)
        all_active = sum(1 for ch in targets if ch.container_status == "available")
        capacity = max(0, 3 - (all_active - len([c for c in pending if c.unique_code in addr_map])))
        to_start = [ch for ch in pending if ch.unique_code not in addr_map]
        pending_start: list[str] = []  # 没名额启动的，留待后续轮次补启
        for ch in to_start:
            if len(addr_map) >= MAX_LAUNCH:
                print(f"  [skip] {ch.unique_code} 已达本批启动上限 {MAX_LAUNCH}，后续轮次再启")
                pending_start.append(ch.unique_code)
                continue
            if capacity <= 0:
                print(f"  [skip-no-quota] {ch.unique_code} 靶场 max=3 名额已满，等名额释放后启")
                pending_start.append(ch.unique_code)
                continue
            try:
                started = await bench.start_challenge(ch.unique_code)
                addr_map[ch.unique_code] = started.container_addr[0] if started.container_addr else ""
                capacity -= 1
                print(f"  [start] {ch.unique_code} -> {started.container_addr}")
            except Exception as e:
                print(f"  [start-fail] {ch.unique_code}: {e}")
                pending_start.append(ch.unique_code)

        if not addr_map:
            raise SystemExit("没有可启动/复用的靶场容器")

        # 2) 下发任务（每题一个 task，独立 target）
        task_of: dict[str, str] = {}
        for code, addr in addr_map.items():
            tid = await dispatch_task(pobi, code, addr, 0)
            if tid:
                task_of[code] = tid

        async def try_backfill() -> None:
            """名额释放后补启未启动的靶场并下发任务。"""
            if not pending_start:
                return
            chs = await bench.list_challenges()
            web = _select_web_challenges(chs)
            active = sum(1 for c in web if c.container_status == "available")
            cap = max(0, 3 - active)
            still_pending: list[str] = []
            for code in pending_start:
                if cap <= 0 or len(task_of) >= MAX_LAUNCH:
                    still_pending.append(code)
                    continue
                ch = next((c for c in web if c.unique_code == code), None)
                if not ch or ch.is_completed:
                    still_pending.append(code)
                    continue
                try:
                    if ch.container_status != "available":
                        started = await bench.start_challenge(code)
                        addr = started.container_addr[0] if started.container_addr else ""
                    else:
                        addr = ch.container_addr[0] if ch.container_addr else ""
                    tid = await dispatch_task(pobi, code, addr, 0)
                    if tid:
                        task_of[code] = tid
                    cap -= 1
                except Exception as e:
                    print(f"  [backfill-fail] {code}: {e}")
                    still_pending.append(code)
            pending_start[:] = still_pending

        # 3) 每 POLL_INTERVAL 秒查询一次任务结果；有结果就去提交 flag
        done: set[str] = set()
        total_submitted = 0
        round_no = 0
        while task_of or pending_start:
            round_no += 1
            print(f"\n=== 轮询第 {round_no} 轮（{POLL_INTERVAL}s 间隔）=== 待处理 {len(task_of) - len(done)} 题，待启动 {len(pending_start)} 题")
            for code, tid in list(task_of.items()):
                if code in done:
                    continue
                st = pobi.get_status(tid)
                print(f"  {code} task {tid} status={st}")
                if st in ("completed", "failed", "cancelled"):
                    sub = await submit_flags_for(pobi, bench, code, tid)
                    total_submitted += sub
                    done.add(code)
                    # 提交后检查是否通关，通关则 close 释放名额
                    try:
                        chs = await bench.list_challenges()
                        ch = next((c for c in chs if c.unique_code == code), None)
                        if ch and ch.is_completed:
                            await bench.close_challenge(code)
                            print(f"  [close] {code} 已通关，释放名额")
                    except Exception:
                        pass
            # 名额释放后补启剩余靶场
            await try_backfill()
            if not task_of and not pending_start:
                break
            if len(done) < len(task_of) or pending_start:
                print(f"  ... 等待 {POLL_INTERVAL}s 后再次查询")
                await asyncio.sleep(POLL_INTERVAL)

        print(f"\n[done] 本轮处理 {len(task_of)} 题，成功提交 flag {total_submitted} 个")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except VpnCheckError as e:
        raise SystemExit(f"VPN 预检失败：{e}")
