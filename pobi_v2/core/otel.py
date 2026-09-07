"""读取当前 OpenTelemetry span 上下文。

审计与任务事件需要关联到对应链路，这里提供唯一的取值入口：
API 侧通常没有活动 span，返回 (None, None)，属预期行为。
"""
from __future__ import annotations

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover - opentelemetry 为可选依赖
    trace = None  # type: ignore[assignment]


def current_trace_ids() -> tuple[str | None, str | None]:
    """返回当前 span 的 ``(trace_id, span_id)``，均为小写十六进制字符串。

    无活动 span、span 无效或 opentelemetry 不可用时返回 ``(None, None)``。
    """
    if trace is None:
        return None, None
    ctx = trace.get_current_span().get_span_context()
    if not ctx or not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
