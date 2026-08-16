# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""Web application code retrieval-augmented generation (RAG) tool.

This module provides a tool for performing semantic search over indexed
web application source code, enabling AI agents to retrieve relevant
code snippets and documentation for security analysis and research.
"""
from typing import Union
from pydantic_ai import RunContext
from pobi_agent.utils.structures import RagDeps, WebappreconDeps, RequesterDeps

async def webapp_code_rag(
        context: RunContext[Union[RagDeps, WebappreconDeps, RequesterDeps]],
        search_query: str
    ) -> str:
    """Fetch relevant code snippets from the indexed target via semantic search.

    Args:
        context: Execution context providing dependencies such as RAG client,
            embeddings API, and target metadata.
        search_query: Natural-language prompt describing the desired code.

    Returns:
        Aggregated code chunks concatenated as a plain-text string.
    """
    res = ""
    if len(context.deps.target) > 1:
        search_query += '\n The target supplied is: ' + context.deps.target

    # 向量模型为增强能力：靶场环境通常不提供 embedder。缺失时优雅降级，
    # 引导 LLM 改用 ContextEngine 已沉淀的 facts / shell 检索，而非抛异常崩溃。
    embedder_client = context.deps.embedder_client
    if embedder_client is None:
        return (
            "代码语义检索不可用：当前环境未配置向量（embedding）模型。\n"
            "请改用以下方式获取侦查产物：\n"
            "1) ContextEngine 已沉淀的结构化 facts（技术栈、端点、参数、认证面）；\n"
            "2) shell 工具直接在目标上检索；\n"
            "3) requester 抓取页面源码后本地分析。"
        )

    try:
        embedding = await embedder_client.batch_embed(
            input_texts=[search_query],
        )
    except Exception as exc:
        return (
            f"代码语义检索失败（embedding 服务异常：{exc!r}）。\n"
            "请改用 ContextEngine facts 或 shell 工具检索，不要重试本工具。"
        )

    assert len(embedding) == 1, (
        f'Expected 1 embedding, got {len(embedding)}, doc query: {search_query!r}'
    )
    embedding = embedding[0]['embedding']

    # With SQLite-per-session, the connector is already scoped to the
    # session — no session_id parameter needed.
    results = await context.deps.rag.similarity_search_code_chunk(
        query_embedding=embedding,
        vector_dim=embedder_client.vector_dim,
        limit=5,
    )
    for chunk, similarity in results:
        res = res + '\n' + chunk.code_content

    return res
