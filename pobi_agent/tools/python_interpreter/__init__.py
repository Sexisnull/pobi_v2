# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""Python interpreter tool for executing Python code in sandboxed environments.

This module provides functionality to execute Python code safely within
sandboxed environments, enabling AI agents to run Python scripts and
code snippets for security research and analysis tasks.
"""
from pobi_agent.constants import CACHE_DEADEND_LOGS, DEADEND_AGENTS_PATH
import asyncio
import io
import json
import shlex
import tarfile
from pathlib import Path
from typing import Any
from pydantic_ai import RunContext

from pobi_agent.logging import logger
from pobi_agent.utils.functions import truncate_string
from pobi_agent.sandbox.sandbox_manager import SandboxManager
from pobi_v2.core.config import settings
from pobi_agent.tools.tool_wrappers import with_tool_events
from pobi_agent.auth_resolver import AuthContextHandler, safe_auth_summary

# 全局共享 SandboxManager 单例：复用 compose 常驻的 Kali 容器（单实例共享）。
_shared_manager: SandboxManager | None = None


def _get_shared_manager() -> SandboxManager:
    """Lazily create a process-wide SandboxManager (Docker client) singleton."""
    global _shared_manager
    if _shared_manager is None:
        _shared_manager = SandboxManager()
    return _shared_manager


def _get_shared_kali_sandbox():
    """获取全局共享 Kali 沙箱（shell 与 python 共用同一容器）。"""
    return _get_shared_manager().get_or_create_shared_kali()


def _copy_file_to_container(sandbox, file_path: Path, filename: str) -> None:
    """将宿主机脚本文件以 tar 形式复制进 Kali 容器的 /pobi_scripts/ 目录。"""
    container = sandbox._docker_client.containers.get(sandbox.container_id)
    container.exec_run("mkdir -p /pobi_scripts")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        tar.add(str(file_path), arcname=filename)
    tar_stream.seek(0)
    container.put_archive("/pobi_scripts", tar_stream.read())



@with_tool_events("read_auth_storage")
async def read_auth_storage(
    ctx: Any,
    profile: str = "default",
    include_secrets: bool = False,
) -> str:
    """Return JSON metadata about a saved authentication context.

    By default this returns a *safe* summary (cookie names, storage keys,
    header names, final URL). Real cookie/token values are only returned when
    ``include_secrets=True`` and should be reserved for sandboxed code paths
    that strictly need the raw material.

    The function accepts either a ``RunContext`` (with ``deps.target``,
    ``deps.agent_id``, ``deps.session_id``) or a plain string treated as
    ``session_id`` for backward compatibility.
    """
    target: str | None = None
    agent_id: Any = None
    session_id: Any = None

    deps = getattr(ctx, "deps", None) if ctx is not None else None
    if deps is not None:
        target = getattr(deps, "target", None)
        agent_id = getattr(deps, "agent_id", None)
        session_id = getattr(deps, "session_id", None)
    elif isinstance(ctx, str):
        session_id = ctx
    elif ctx is None:
        return json.dumps({"available": False, "note": "No context provided"})

    if not target or agent_id is None or session_id is None:
        # Backward-compatible fallback: walk the legacy session-only directory
        # and report what we find without secrets.
        if isinstance(ctx, str):
            legacy_dir = DEADEND_AGENTS_PATH / ctx / "auth_context"
            index_file = legacy_dir / "index.json"
            if index_file.exists():
                try:
                    return json.dumps({
                        "available": True,
                        "legacy": True,
                        "index": json.loads(index_file.read_text(encoding="utf-8")),
                    })
                except json.JSONDecodeError:
                    pass
        return json.dumps({
            "available": False,
            "note": "target, agent_id and session_id are required to resolve auth context",
        })

    try:
        handler = AuthContextHandler(target=target, agent_id=agent_id, session_id=session_id)
        context = handler.load_context(profile)
        if context is None:
            return json.dumps({
                "available": False,
                "profile": profile,
                "target": target,
                "agent_id": str(agent_id),
                "session_id": str(session_id),
            })
        if include_secrets:
            return context.model_dump_json()
        return json.dumps(safe_auth_summary(context))
    except Exception as exc:
        logger.warning("read_auth_storage failed: %s", exc)
        return json.dumps({"available": False, "error": str(exc)})

@with_tool_events("run_python_file")
async def run_python_file(
    ctx: RunContext[Any],
    code: str,
    filename: str,
    packages: list[str]
) -> Any:
    """Write Python code to a file and execute it in the shared Kali sandbox.

    This function combines writing Python code to a cache directory and executing
    it inside the global shared Kali Docker container (the same container used for
    shell-based attack operations). The file is written to ./<filename> in the
    current working directory and then copied into the container and run with python3.

    Args:
        code: The Python source code to write and execute.
        filename: Target filename (e.g., "script.py").
        packages: List of package specifiers to install before execution.

    Returns:
        Any: Execution output (stdout/stderr) from the sandbox, truncated for display.

    Raises:
        RuntimeError: If the shared Kali sandbox cannot be reached.
    """
    # Write Python code to cache directory
    # TODO: needs to be reviewed and changed here, we might want to save it more
    # in the same place as the agents root or make it more easier
    python_output_dir = Path.cwd() / "python_scripts"
    python_output_dir.mkdir(parents=True, exist_ok=True)
    file_path = python_output_dir / filename
    file_path.write_text(code, encoding="utf-8")
    print(code)

    deps = getattr(ctx, "deps", None)
    if isinstance(deps, str):
        session_id = deps
    else:
        session_id = getattr(deps, "session_id", None) if deps is not None else None
    session_id = session_id or f"session_{id(file_path)}"

    # 获取全局共享 Kali 沙箱（与 shell 攻击操作共用同一容器）
    try:
        sandbox = _get_shared_kali_sandbox()
    except Exception as exc:
        raise RuntimeError(f"无法连接全局共享 Kali 沙箱：{exc}") from exc

    # 将脚本复制进 Kali 容器 /pobi_scripts/
    await asyncio.to_thread(_copy_file_to_container, sandbox, file_path, filename)

    # 安装依赖（若提供）——包名经 shlex.quote 防注入
    if packages:
        pkg_cmd = (
            "python3 -m pip install --quiet --disable-pip-version-check "
            + " ".join(shlex.quote(p) for p in packages)
        )
        install_res = await asyncio.to_thread(sandbox.execute_command, pkg_cmd, stream=False)
        if install_res.get("exit_code", 0) != 0:
            logger.warning("Python 依赖安装失败: %s", install_res.get("stderr"))

    # 在 Kali 容器内执行 python3
    run_cmd = f"python3 /pobi_scripts/{shlex.quote(filename)}"
    result = await asyncio.to_thread(sandbox.execute_command, run_cmd, stream=False)

    result_obj = {
        "result": result.get("stdout", ""),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
    }
    # Save result to python_interpreter.jsonl file
    await _save_result_to_file(session_id, result_obj)

    # 组合 stdout/stderr 供截断返回
    result_str = result.get("stdout", "") or ""
    if result.get("stderr"):
        result_str += ("\n[stderr]\n" + result["stderr"])

    # Pretty print result using rich
    # pprint(result)
    truncated_result = truncate_string(result_str)
    # Returning the results
    return truncated_result


async def _save_result_to_file(session_id: str, result: Any):
    """
    Save Python interpreter result to python_interpreter.jsonl file in the session directory.

    Args:
        session_id (str): Session identifier
        result (Any): Result object to save
    """
    try:
        # Create the directory path
        cache_dir = CACHE_DEADEND_LOGS / session_id
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create the file path
        file_path = cache_dir / "python_interpreter.jsonl"

        # Convert result to JSON-serializable format
        if isinstance(result, (dict, list, str, int, float, bool, type(None))):
            # Already JSON-serializable
            result_data = result
        else:
            # Convert to string if not directly serializable
            result_data = str(result)

        # Create JSON object for this result
        result_entry = {
            "result": result_data
        }

        # Append to file with pretty-printed JSON (indented for readability)
        with open(file_path, "a", encoding="utf-8") as f:
            json_line = json.dumps(result_entry, ensure_ascii=False, indent=2)
            f.write(json_line + "\n")

    except Exception as e:
        print(f"Warning: Could not save Python interpreter result to file: {e}")
