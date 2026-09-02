import json
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse
from pydantic_ai import RunContext
from pobi_agent.utils.structures import RequesterDeps
from pobi_agent.utils.functions import truncate_string
from pobi_agent.logging import logger
from pobi_agent.constants import CACHE_DEADEND_LOGS
from pobi_agent.storage_context import get_task_root

from .http_parser import is_valid_request_detailed, extract_host_port, autocorrect_http_request
from .auth_handler import replace_credential_placeholders
from .pw_requester import PlaywrightRequester
from .pw_session_manager import PlaywrightSessionManager
from pobi_agent.tools.tool_wrappers import with_tool_events
from pobi_agent.auth_resolver import (
    AuthContextHandler,
    inject_headers_into_raw_request,
    write_playwright_storage_state,
)
from pobi_agent.tools.browser.validate_refresh import auto_validate_before_consume

__all__ = ["is_valid_request_detailed", "PlaywrightRequester"]


def _extract_endpoint(raw_request: str) -> str:
    """从原始 HTTP 请求第一行提取 METHOD + path（用于足迹归类）。"""
    first = (raw_request or "").splitlines()[0] if raw_request else ""
    parts = first.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return first[:80]


# 认证重定向目标特征（302→login 判定 auth_required）。
_LOGIN_LOCATION_RE = re.compile(r"/(?:login|signin|auth|sso)(?:[/?#]|$)", re.IGNORECASE)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _request_url(raw_request: str, is_tls: bool) -> str:
    """从 raw HTTP 请求第一行 + Host header 组装完整 URL。"""
    lines = (raw_request or "").split("\r\n")
    first = lines[0] if lines else ""
    parts = first.split()
    target = parts[1] if len(parts) > 1 else "/"
    host = ""
    for line in lines[1:]:
        if line.lower().startswith("host:"):
            host = line.split(":", 1)[1].strip()
            break
    scheme = "https" if is_tls else "http"
    if host:
        return f"{scheme}://{host}{target}"
    return target


def _parse_response_text(raw: str) -> dict | None:
    """从原始 HTTP 响应文本解析 status_code/headers/body；无法解析返回 None。

    兼容 ``send_raw_data`` 的重定向拼接（``=== FOLLOWING REDIRECTS ===``），
    取最终响应段；请求失败的错误文本（非 HTTP 响应）返回 None 跳过。
    """
    if not raw:
        return None
    if "=== FOLLOWING REDIRECTS ===" in raw:
        raw = raw.rsplit("=== FOLLOWING REDIRECTS ===", 1)[-1]
    raw = raw.lstrip("\r\n")
    lines = raw.split("\r\n")
    status_line = lines[0] if lines else ""
    m = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})", status_line)
    if not m:
        return None
    status_code = int(m.group(1))
    headers: dict[str, str] = {}
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if not line.strip():
            break
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        idx += 1
    body = "\r\n".join(lines[idx + 1:]) if idx + 1 < len(lines) else ""
    return {"status_code": status_code, "headers": headers, "body": body}


def _persist_http_tx(
    ctx: RunContext["RequesterDeps"],
    raw_request_anon: str,
    is_tls: bool,
    auth_used: bool,
    responses: list,
) -> None:
    """把 requester 每次请求结构化写入 recon_http_transactions（与 sitemap 对齐）。

    解析 raw 请求/响应文本拆出 method/url/status_code/headers/body 等字段，
    与 katana JSONL 字段同 schema 落库；recon_store 未注入时安全跳过，
    异常仅记 debug 不阻断请求主流程。
    """
    context = getattr(ctx.deps, "context", None)
    if context is None or not hasattr(context, "add_recon_http_transaction"):
        return
    try:
        from pobi_agent.utils.urls import extract_params, normalize_path

        req_url = _request_url(raw_request_anon, is_tls)
        req_host = urlparse(req_url).netloc or ""
        req_path = normalize_path(req_url)
        # 请求侧信息（method / headers / body）
        req_lines = (raw_request_anon or "").split("\r\n")
        req_first = req_lines[0].split() if req_lines else []
        req_method = req_first[0] if req_first else "GET"
        req_headers: dict[str, str] = {}
        for line in req_lines[1:]:
            if ":" in line and not line.strip().startswith(("http", "/")):
                k, _, v = line.partition(":")
                req_headers[k.strip().lower()] = v.strip()
        # 从 request 段末尾提取 body（Host 头之后的空行之后）
        req_body = ""
        for i, line in enumerate(req_lines[1:], start=1):
            if not line.strip():
                req_body = "\r\n".join(req_lines[i + 1:])
                break

        for response in responses:
            text = (
                response.decode("utf-8", errors="replace")
                if isinstance(response, bytes)
                else str(response)
            )
            parsed = _parse_response_text(text)
            if parsed is None:
                continue  # 非 HTTP 响应（连接错误等）不记事务
            status = int(parsed["status_code"])
            resp_headers = parsed["headers"]
            auth_required = status in (401, 403, 407) or (
                status in (301, 302, 303, 307, 308)
                and bool(_LOGIN_LOCATION_RE.search(urlparse(resp_headers.get("location", "")).path or ""))
            )
            body = parsed["body"]
            title_m = _HTML_TITLE_RE.search(body or "")
            title = re.sub(r"\s+", " ", title_m.group(1)).strip()[:512] if title_m else ""
            context.add_recon_http_transaction(
                host=req_host,
                path_normalized=req_path,
                url=req_url,
                method=req_method,
                status_code=status,
                source="agent:requester",
                request_headers=req_headers,
                request_body=req_body,
                response_headers=resp_headers,
                response_body=body,
                response_title=title,
                content_type=resp_headers.get("content-type", ""),
                response_size=len(body or ""),
                detected_params=extract_params(req_url),
                auth_used=auth_used,
                auth_required=auth_required,
            )
    except Exception as exc:  # noqa: BLE001 - 事务结构化写入失败不阻断请求
        logger.debug("HTTP 事务结构化写入失败（忽略）: %s", exc)


def _record_payload_footprint(
    ctx: RunContext[RequesterDeps],
    endpoint: str,
    payload: str,
    status: str,
    last_result: str = "",
    success: bool = False,
) -> None:
    """旁路记录 requester 每次 payload 尝试到 recon_techniques。

    供主控（supervisor）证据驱动收敛：同一 payload 的尝试次数 / 最新结果
    （如 connection reset / 404）在主控决策时可见，避免死磕已失败的攻击面。
    仅在 deps 注入 context 时生效；异常仅记 warning，绝不阻断请求主流程。
    """
    context = getattr(ctx.deps, "context", None)
    if context is None:
        return
    try:
        # 幂等键 (task_id, name)：同 payload 合并计数，不同 payload 各自成行。
        # name 前缀带 endpoint，便于主控按攻击面聚合失败足迹（UNION 变体归为同一面）。
        p = payload.strip().replace("\r\n", " ").replace("\n", " ")[:80]
        digest = hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:8]
        name = f"{endpoint} | {p} [{digest}]"
        context.add_recon_technique(
            name=name,
            category="http",
            status=status,
            success_count=1 if success else 0,
            tested_count=1,
            last_result=last_result[:200] or "",
            confidence=0.5,
        )
    except Exception as exc:  # noqa: BLE001 - 旁路写入失败不影响攻击执行
        logger.warning("RECON 足迹写入失败（已忽略）: %s", exc)


@with_tool_events("pw_send_payload")
async def pw_send_payload(
    ctx: RunContext[RequesterDeps],
    target_host: str,
    raw_request: str,
    verify_ssl: bool = False,
    auth_profile: str | None = None,
    force_validate_auth: bool = False,
    skip_auth_validation: bool = False,
    auth_validation_ttl_s: float = 60.0,
):
    """
    Send HTTP payload using Playwright with enhanced capabilities and session persistence.

    This function provides the same interface as the original send_payload()
    but uses Playwright for improved functionality with persistent sessions
    that maintain cookies between requests.

    Auto-corrects common HTTP request malformations before sending:
    - Line endings (\\n -> \\r\\n)
    - Missing Host header (derived from target)
    - Malformed request line
    - Missing HTTP version

    Args:
        target_host (str): Target host in format "host:port" or URL
        raw_request (str): Raw HTTP request string
        verify_ssl (bool): Whether to verify SSL certificates

    Returns:
        Union[str, bytes]: HTTP response or error message
    """
    # Use the target_host parameter passed by the LLM, fallback to ctx.deps.target if empty
    effective_target = target_host if target_host and target_host.strip() else ctx.deps.target
    if not effective_target:
        return "Error: target_host must be provided either as parameter or in context"
    
    host, port = extract_host_port(target_host=effective_target)

    # Auto-correct malformed HTTP requests before processing
    try:
        corrected_request, corrections = autocorrect_http_request(
            raw_request=raw_request,
            target_host=effective_target
        )
        if corrections:
            logger.debug("Auto-corrected HTTP request: %s", ', '.join(corrections))
        raw_request = corrected_request
    except ValueError as e:
        return f"Error: Cannot auto-correct request - {str(e)}"

    is_tls = port == 443 or effective_target.startswith('https://')
    proxy_url = ctx.deps.proxy_url

    # Resolve optional saved auth profile -> Playwright storage_state file.
    auth_storage_state_path: str | None = None
    if auth_profile:
        if getattr(ctx.deps, "agent_id", None) is None or getattr(ctx.deps, "session_id", None) is None:
            return "Error: agent_id and session_id are required to use auth_profile"
        # Phase 13: validate the saved AuthContext before consuming it.
        validation_failure = await auto_validate_before_consume(
            target=effective_target,
            agent_id=ctx.deps.agent_id,
            session_id=ctx.deps.session_id,
            profile=auth_profile,
            validation_ttl_s=auth_validation_ttl_s,
            force_validate=force_validate_auth,
            skip_validation=skip_auth_validation,
            proxy_url=proxy_url,
            verify_ssl=verify_ssl,
        )
        if validation_failure is not None:
            reason = validation_failure.get("expired_reason") or validation_failure.get("error") or "validation failed"
            return (
                f"Error: saved auth_profile {auth_profile!r} is no longer valid "
                f"({reason}). Call refresh_auth_context (if a refresh_url exists) "
                "or re-run authenticate before retrying."
            )
        try:
            handler = AuthContextHandler(
                target=effective_target,
                agent_id=ctx.deps.agent_id,
                session_id=ctx.deps.session_id,
            )
            auth_context = handler.load_context(auth_profile)
            if auth_context is None:
                return f"Error: no saved auth context for profile {auth_profile!r}"
            playwright_path = handler.playwright_storage_path(auth_profile)
            # Always (re)materialise so JSON/Basic auth contexts that don't have
            # a saved Playwright snapshot still feed cookies into the session.
            write_playwright_storage_state(
                auth_context,
                playwright_path,
                target=effective_target,
            )
            auth_storage_state_path = str(playwright_path)
            # Also inject saved AuthContext.headers (e.g. ``Authorization: Bearer ...``
            # produced by JSON/API or Basic auth) into the raw request, unless
            # the model already set them explicitly.
            if auth_context.headers:
                raw_request = inject_headers_into_raw_request(
                    raw_request, dict(auth_context.headers)
                )
        except Exception as e:
            return f"Error preparing auth profile {auth_profile!r}: {e}"

    # Anonymisation process (run AFTER auth-header injection so saved headers
    # are still subject to credential placeholder substitution).
    # The function detects the dummy credentials given and replaces them with
    # the real ones, so the LLM will never see the true credentials.
    raw_request_anon = replace_credential_placeholders(raw_request)
    endpoint = _extract_endpoint(raw_request)
    # 防重复护栏：同会话内该 payload 已尝试且失败过（如 connection reset），
    # 直接提示换变体，避免子 agent 反复重发同一请求烧 token。
    context = getattr(ctx.deps, "context", None)
    if context is not None:
        task_key = str(ctx.deps.session_id)
        try:
            if context.was_already_attempted(raw_request_anon, task_key):
                return (
                    f"Notice: this exact payload was already attempted and failed "
                    f"earlier in this session (connection reset or error). "
                    f"Do NOT resend it; craft a different payload/variant instead."
                )
            if context.is_surface_dead(endpoint, threshold=10):
                return (
                    f"BLOCKED: attack surface {endpoint!r} is a dead end "
                    f"(>=10 failed attempts, no success, e.g. connection reset). "
                    f"Do NOT keep sending payloads to it. Switch to a different "
                    f"attack vector (blind SQLi / error-based / other endpoints)."
                )
        except Exception as _exc:  # noqa: BLE001 - 去重/死路检查失败不阻断请求
            logger.debug("去重/死路检查失败（忽略）: %s", _exc)
    # session_key = _build_session_key(
    #     host=host,
    #     port=port,
    #     proxy_url=proxy_url,
    #     verify_ssl=verify_ssl,
    # )

    # pw_requester session
    pw_session = await PlaywrightSessionManager.get_session(
        session_key=str(ctx.deps.session_id),
        agent_id=str(ctx.deps.agent_id),
        verify_ssl=verify_ssl,
        proxy_url=proxy_url,
        auth_storage_state_path=auth_storage_state_path,
        auth_profile=auth_profile,
        target=str(ctx.deps.target),
    )
    responses = []
    try:
        async for response in pw_session.send_raw_data(
            host=host,
            port=port,
            request_data=raw_request_anon,
            is_tls=is_tls,
        ):
            responses.append(response)

        # Save responses to requester.jsonl file
        await _save_responses_to_file(
            agent_id=str(ctx.deps.agent_id), 
            session_key=str(ctx.deps.session_id), 
            responses=responses)

        # 结构化写入 recon_http_transactions（与 sitemap:katana 对齐，供站点地图）
        _persist_http_tx(
            ctx,
            raw_request_anon,
            is_tls,
            auth_used=auth_profile is not None,
            responses=responses,
        )

        # Convert bytes responses to strings before truncation
        string_responses = []
        for response in responses:
            if isinstance(response, bytes):
                try:
                    response_str = response.decode('utf-8', errors='replace')
                except Exception:
                    response_str = str(response)
            else:
                response_str = str(response)
            string_responses.append(response_str)

        truncated_responses = [truncate_string(resp) for resp in string_responses]
        # 足迹：请求层结果（成功拿到响应），内容摘要供主控判断攻击语义
        _record_payload_footprint(
            ctx, endpoint, raw_request_anon,
            status="success", success=True,
            last_result=str(truncated_responses)[:200],
        )
        return str(truncated_responses)

    except Exception as e:
        # 足迹：请求失败（连接重置 / 超时等），主控可见该攻击面持续被断
        _record_payload_footprint(
            ctx, endpoint, raw_request_anon,
            status="failed", success=False,
            last_result=str(e),
        )
        # 防重复护栏：记录失败尝试，供后续同 payload 查重拦截
        if context is not None:
            try:
                context.record_attempt(
                    task=str(ctx.deps.session_id),
                    payload=raw_request_anon,
                    result="failed",
                    reason=str(e),
                )
            except Exception as _exc:  # noqa: BLE001
                logger.debug("失败尝试记录（忽略）: %s", _exc)
        return f"Error when sending payload: {str(e)}"


async def cleanup_playwright_sessions():
    """
    Clean up all Playwright sessions.
    
    This function should be called when the application exits or when
    you want to clear all session data (cookies, etc.).
    """
    await PlaywrightSessionManager.cleanup_all_sessions()

async def _save_responses_to_file(agent_id: str, session_key: str, responses: list):
    """
    Save responses to requester.jsonl file in the session directory.
    
    Args:
        session_key (str): Session identifier
        responses (list): List of response objects to save
    """
    try:
        # 优先归口到统一任务根 tasks/<task_id>/logs；未注入时回退旧 cache/logs 路径
        task_root = get_task_root()
        if task_root is not None:
            cache_dir = Path(task_root) / "logs" / session_key
        else:
            cache_dir = CACHE_DEADEND_LOGS / agent_id / session_key
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Create the file path (convert to string for regular open())
        file_path_str = str(cache_dir / "requester.jsonl")

        # Convert responses to JSON-serializable format and append to file
        with open(file_path_str, "a", encoding="utf-8") as f:
            for response in responses:
                # Handle bytes responses
                if isinstance(response, bytes):
                    try:
                        response_str = response.decode('utf-8', errors='replace')
                    except Exception:
                        response_str = str(response)
                else:
                    response_str = str(response)

                # Create JSON object for this response
                response_data = {
                    "response": response_str
                }

                # Append to file with pretty-printed JSON (indented for readability)
                json_line = json.dumps(response_data, ensure_ascii=False, indent=2)
                f.write(json_line + "\n")
    except Exception as e:
        logger.warning("Could not save responses to file: %s", e)


async def cleanup_playwright_session_for_target(
    target_host: str,
    proxy_url: str | None = None,
    verify_ssl: bool = False,
):
    """
    Clean up a specific Playwright session for a target.
    
    Args:
        target_host (str): Target host to clean up session for
        proxy_url (str | None): Proxy URL used for the session
        verify_ssl (bool): Whether SSL verification was used
    """
    host, port = extract_host_port(target_host)
    session_key = _build_session_key(
        host=host,
        port=port,
        proxy_url=proxy_url,
        verify_ssl=verify_ssl,
    )
    await PlaywrightSessionManager.cleanup_session(session_key)


def _build_session_key(host: str, port: int, proxy_url: str | None, verify_ssl: bool) -> str:
    proxy_digest = hashlib.sha256((proxy_url or "").encode("utf-8")).hexdigest()[:12]
    tls_mode = "verify" if verify_ssl else "insecure"
    return f"{host}_{port}_{tls_mode}_{proxy_digest}"
