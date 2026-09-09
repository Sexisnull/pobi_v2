# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""Security research tools for AI agent interactions and automation.

This module provides a collection of tools that AI agents can use for
security research, including shell execution, HTTP requests, code analysis,
and browser automation for comprehensive security assessments.
"""

from .avfs import (
    avfs_chdir,
    avfs_grep,
    avfs_list,
    avfs_mount,
    avfs_read,
    avfs_umount,
    avfs_write,
    chdir_memory_directory,
    chdir_workspace,
    grep_memory_files,
    grep_workspace_files,
    list_memory_files,
    list_workspace_files,
    mount_memory_workspace,
    mount_workspace,
    read_memory_file,
    read_workspace_file,
    umount_memory_workspace,
    umount_workspace,
    write_memory_file,
    write_workspace_file,
)
from .browser import (
    authenticate,
    browser_run_steps,
    observe_login_surface,
    refresh_auth_context,
    validate_auth_context,
)
from .browser_automation import (
    cleanup_playwright_session_for_target,
    cleanup_playwright_sessions,
    is_valid_request_detailed,
    pw_send_payload,
)
from .fingerprint import webapp_fingerprint  # noqa: F401 - 兼容层保留（引擎封装）
from .python_interpreter import read_auth_storage, run_python_file
from .recon_lookup import recon_lookup
from .shell import sandboxed_shell_tool
from .tool_wrappers import with_tool_events, wrap_tool_with_events
from .webapp_analyzer import webapp_analyzer
from .webapp_code_rag import webapp_code_rag

__all__ = [
    # Playwright
    "authenticate",
    # AVFS
    "avfs_chdir",
    "avfs_grep",
    "avfs_list",
    "avfs_mount",
    "avfs_read",
    "avfs_umount",
    "avfs_write",
    "browser_run_steps",
    "chdir_memory_directory",
    "chdir_workspace",
    "cleanup_playwright_session_for_target",
    "cleanup_playwright_sessions",
    "grep_memory_files",
    "grep_workspace_files",
    "is_valid_request_detailed",
    "list_memory_files",
    "list_workspace_files",
    "mount_memory_workspace",
    "mount_workspace",
    "observe_login_surface",
    "pw_send_payload",
    "read_auth_storage",
    "read_memory_file",
    "read_workspace_file",
    # RECON 本地物化库检索
    "recon_lookup",
    "refresh_auth_context",
    # Python interpreter
    "run_python_file",
    # Shell
    "sandboxed_shell_tool",
    "umount_memory_workspace",
    "umount_workspace",
    "validate_auth_context",
    # web app analyzer
    "webapp_analyzer",
    # Webapp Rag
    "webapp_code_rag",
    # Tool wrappers
    "with_tool_events",
    "wrap_tool_with_events",
    "write_memory_file",
    "write_workspace_file",
]
