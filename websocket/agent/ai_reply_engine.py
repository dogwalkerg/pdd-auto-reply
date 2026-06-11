# -*- coding: utf-8 -*-
"""
websocket.agent.ai_reply_engine —— AI 回复引擎与工具调用（复用 CustomerAgent）
============================================================================
本文件用途：复用改造 Customer-Agent 的 ``CustomerAgent._run_agent_loop`` Agent
循环，实现 websocket 服务的 AI 智能回复引擎。核心流程（需求 8）：

1. 校验 AI 配置可用（启用且密钥非空），不可用 / 未启用直接回退默认回复；
2. 以「系统 Prompt（含店铺指令）+ 历史 + 当前客户消息」构建 messages；
3. Agent 循环：调用 LLM → 若返回工具调用则执行知识库工具（get_product_knowledge /
   search_customer_service_knowledge，经 common kb.search）→ 把工具结果回传 →
   再次调用 LLM，直至无工具调用或达最大轮数（需求 8.2 / 8.3 / 8.4）；
4. 基于检索内容生成中文回复（需求 8.4 / 8.7）；
5. LLM 调用失败 / 超时（含未配置、密钥无效、网络异常、超时）→ 回退店铺配置的
   默认回复，并将处理结果记为「AI 回复失败」（process_result = ai_reply_failed，
   需求 8.5）。

输出 ``AIReplyResult`` 纯数据结构，供上层消息消费者据此发送回复并落库消息日志。
本模块不直接发消息、不直接写库（与 reply_engine 决策链一致，便于测试）。

实现约束（开发规范）：导入置顶（51）；中文注释（37）；单文件 ≤500 行（35）；
文件名用下划线（40）；全中文（50）；复用 common kb.search 与 Agent 循环思路（52）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.agent_config import AgentConfig
from agent.llm_client import LLMClient, LLMResponse
from agent.tools import TOOL_SCHEMAS, execute_tool

logger = logging.getLogger("websocket.agent.ai_reply_engine")

# 处理结果常量：与数据字典 process_result 的 dict_key 对齐
# （common.services.dict_seed_data.DICT_SEED_DATA["process_result"]）。
RESULT_AI_REPLY: str = "ai_reply"  # AI 回复成功
RESULT_AI_REPLY_FAILED: str = "ai_reply_failed"  # AI 回复失败（回退默认回复，需求 8.5）

# 默认系统 Prompt：约束 AI 使用中文回复（需求 8.7），并说明可用工具用途。
DEFAULT_SYSTEM_PROMPT: str = (
    "你是一名拼多多店铺的智能客服助手。请始终使用简体中文、礼貌专业地回复客户。"
    "请基于检索到的知识内容作答，不要编造不存在的信息。"
)


@dataclass
class AIReplyResult:
    """AI 回复引擎输出结果（纯数据结构）。

    Attributes:
        success: 是否成功生成 AI 回复（False 表示已回退默认回复）。
        content: 最终回复文本（成功为 AI 回复；失败为店铺默认回复，可能为空）。
        log_result: 处理结果（ai_reply / ai_reply_failed，与数据字典对齐）。
        used_default: 是否使用了默认回复回退（需求 8.5）。
        error: 失败原因（中文/异常文本，仅失败时非空，用于日志，不外泄密钥）。
        tool_calls_made: 本次回复期间实际执行的工具名列表（便于排查/日志）。
    """

    success: bool
    content: Optional[str] = None
    log_result: str = RESULT_AI_REPLY
    used_default: bool = False
    error: Optional[str] = None
    tool_calls_made: List[str] = field(default_factory=list)


def _build_messages(
    query: str,
    config: AgentConfig,
    history: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """构建发送给 LLM 的消息列表（系统 Prompt + 店铺指令 + 历史 + 当前消息）。

    Args:
        query: 当前客户消息文本。
        config: AI 运行时配置（含店铺自定义指令 instructions）。
        history: 历史对话消息（OpenAI 风格，可为空）。

    Returns:
        messages 列表。
    """
    system_content = DEFAULT_SYSTEM_PROMPT
    if config.instructions:
        # 追加店铺自定义指令（需求 8.6 配置的提示词指令）
        system_content = system_content + "\n" + "\n".join(config.instructions)

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": query})
    return messages


async def _run_agent_loop(
    client: LLMClient,
    messages: List[Dict[str, Any]],
    max_loops: int,
    tool_dependencies: Dict[str, Any],
    tool_calls_made: List[str],
) -> str:
    """Agent 循环核心：LLM ↔ 工具调用，直至无工具调用或达上限（需求 8.2-8.4）。

    Args:
        client: 已配置工具 schema 的 LLM 客户端。
        messages: 初始消息列表（会在循环中被追加 assistant / tool 消息）。
        max_loops: 最大循环轮数（防止无限工具调用）。
        tool_dependencies: 工具执行所需的注入依赖（如 shop_id），与 LLM 给出的
            参数合并后传入工具，确保店铺隔离参数始终可靠。
        tool_calls_made: 输出参数，记录实际执行的工具名。

    Returns:
        最终回复文本。

    Raises:
        Exception: LLM 调用异常向上抛出，由 generate_reply 统一捕获回退默认回复。
    """
    loop_count = 0
    while loop_count < max_loops:
        response: LLMResponse = await client.chat(messages, tool_choice="auto")

        # 无工具调用：返回最终文本内容
        if not response.has_tool_calls:
            return response.content or ""

        # 保存 assistant 的工具调用消息
        messages.append(
            {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
        )

        # 达到上限前的最后一轮：要求模型基于已有信息直接给出最终回复
        if loop_count >= max_loops - 1:
            messages.append(
                {
                    "role": "user",
                    "content": "[已达到最大工具调用次数，请基于已有信息给出最终中文回复。]",
                }
            )
            final = await client.chat(messages, tool_choice="none")
            return final.content or (response.content or "")

        # 执行工具调用并把结果回传
        for tc in response.tool_calls:
            params = tc.function.parse_arguments()
            # 注入店铺隔离参数（覆盖模型可能给错的 shop_id），确保数据隔离
            merged_params = {**params, **tool_dependencies}
            tool_calls_made.append(tc.function.name)
            # 工具内部为同步数据库检索（kb_service.search），丢入线程池执行，
            # 避免在事件循环线程内同步阻塞，拖垮同进程其它店铺连接的收发与心跳。
            tool_output = await asyncio.to_thread(
                execute_tool, tc.function.name, merged_params
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_output,
                }
            )

        loop_count += 1

    # 兜底：返回最后一条 assistant 文本（理论上不会走到这里）
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg["content"])
    return ""


def _describe_endpoint(config: AgentConfig) -> str:
    """生成用于排障的端点描述（provider / base_url / 模型，绝不含密钥）。

    "Connection error" 这类网络异常仅凭异常文本无法定位连的是哪个地址，故在失败
    日志中附带非敏感的端点信息，便于运维核对 base_url 在运行环境是否可达（规范 4
    要求错误可定位；不回显密钥，规范 37 / 需求 8.6）。

    Args:
        config: AI 运行时配置。

    Returns:
        形如 "provider=openai_compatible, base_url=https://x/v1, model=gpt-4o" 的描述。
    """
    return (
        f"provider={config.provider_type}, "
        f"base_url={config.api_base or '(默认)'}, "
        f"model={config.model_name}"
    )


def _log_prompt_debug(messages: List[Dict[str, Any]], config: AgentConfig) -> None:
    """打印发给 AI 的系统提示词与对话历史概览，便于核对店铺提示词是否生效。

    输出：系统提示词全文（含默认提示词 + 店铺指令）、店铺自定义指令条数、历史
    消息条数。不打印 API 密钥等敏感信息（规范 37 / 需求 8.6）。

    Args:
        messages: 已构建的 OpenAI 风格消息列表。
        config: AI 运行时配置。
    """
    # 系统提示词位于消息列表首位（_build_messages 约定）。
    system_content = ""
    if messages and messages[0].get("role") == "system":
        system_content = str(messages[0].get("content") or "")
    # 历史条数 = 总消息数 - 系统消息(1) - 当前用户消息(1)，最小为 0。
    history_count = max(0, len(messages) - 2)
    logger.info(
        "AI 提示词概览：店铺指令 %d 条，历史 %d 条，系统提示词=%s",
        len(config.instructions),
        history_count,
        system_content,
    )


async def generate_reply(
    query: str,
    config: AgentConfig,
    *,
    shop_id: Optional[int] = None,
    default_reply: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    client: Optional[LLMClient] = None,
) -> AIReplyResult:
    """生成 AI 回复；失败 / 超时回退默认回复并记「AI 回复失败」（需求 8.1-8.5/8.7）。

    Args:
        query: 当前客户消息文本。
        config: AI 运行时配置（AgentConfig，来自数据库 LlmConfig）。
        shop_id: 当前店铺主键 shop.id（作为工具的店铺隔离参数注入）。
        default_reply: 店铺配置的默认回复（AI 失败时回退，需求 8.5）。
        history: 历史对话消息（可选）。
        client: 可选注入的 LLM 客户端（便于测试 / 复用连接）；缺省按 config 构建。

    Returns:
        AIReplyResult：成功时含 AI 回复文本；失败时回退默认回复并记 ai_reply_failed。
    """
    # 1) 配置不可用（未启用 / 缺密钥）：直接回退默认回复，记「AI 回复失败」
    if not config or not config.validate():
        logger.warning("AI 配置不可用（未启用或缺少密钥），回退默认回复")
        return AIReplyResult(
            success=False,
            content=default_reply,
            log_result=RESULT_AI_REPLY_FAILED,
            used_default=True,
            error="AI 配置不可用（未启用或缺少 API 密钥）",
        )

    # 2) 构建 LLM 客户端（注入工具 schema）与消息
    owns_client = client is None  # 仅在自建客户端时负责关闭其连接池（避免泄漏）
    if client is None:
        client = LLMClient(
            api_key=config.api_key,
            api_base=config.api_base,
            model_name=config.model_name,
            temperature=config.temperature,
            timeout_seconds=config.timeout_seconds,
            tools=TOOL_SCHEMAS,
            provider_type=config.provider_type,
        )
    elif not client.tools:
        client.tools = TOOL_SCHEMAS

    messages = _build_messages(query, config, history)
    # 排障日志：打印实际发给 AI 的系统提示词与历史条数，便于核对店铺 AI 设置里
    # 配置的提示词是否已带上（system 内容 = 默认提示词 + 店铺指令）。不打印密钥。
    _log_prompt_debug(messages, config)
    tool_dependencies: Dict[str, Any] = {}
    if shop_id is not None:
        tool_dependencies["shop_id"] = int(shop_id)
    tool_calls_made: List[str] = []

    # 整体超时预算：Agent 循环可能含多轮「LLM 调用 + 工具调用」往返。若整体超时
    # 与单次调用超时取同一值，一旦发生工具调用就极易整体超时被误判。这里按最大
    # 轮数为整体循环分配预算（单次调用超时仍由 LLMClient 自身控制）。
    overall_timeout = config.timeout_seconds * (max(config.max_loops, 1) + 1)

    # 3) 执行 Agent 循环，统一捕获失败 / 超时回退默认回复（需求 8.5）
    try:
        content = await asyncio.wait_for(
            _run_agent_loop(
                client,
                messages,
                max_loops=config.max_loops,
                tool_dependencies=tool_dependencies,
                tool_calls_made=tool_calls_made,
            ),
            timeout=overall_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("AI 回复超时，回退默认回复")
        return AIReplyResult(
            success=False,
            content=default_reply,
            log_result=RESULT_AI_REPLY_FAILED,
            used_default=True,
            error="AI 回复超时",
            tool_calls_made=tool_calls_made,
        )
    except Exception as exc:  # noqa: BLE001 — 统一兜底回退，保证不中断会话
        # 附带非敏感端点信息，便于定位「Connection error」等网络异常连的是哪个地址。
        logger.warning(
            "AI 回复调用失败，回退默认回复：%s（%s）", exc, _describe_endpoint(config)
        )
        return AIReplyResult(
            success=False,
            content=default_reply,
            log_result=RESULT_AI_REPLY_FAILED,
            used_default=True,
            error=f"AI 回复调用失败：{exc}",
            tool_calls_made=tool_calls_made,
        )
    finally:
        # 释放自建客户端持有的连接池（注入的客户端由调用方负责关闭）。
        if owns_client:
            await client.aclose()

    # 4) LLM 返回空内容：同样视为失败，回退默认回复（需求 8.5）
    if not content or not str(content).strip():
        logger.warning("AI 返回空内容，回退默认回复")
        return AIReplyResult(
            success=False,
            content=default_reply,
            log_result=RESULT_AI_REPLY_FAILED,
            used_default=True,
            error="AI 返回空内容",
            tool_calls_made=tool_calls_made,
        )

    # 5) 成功：返回 AI 中文回复
    return AIReplyResult(
        success=True,
        content=str(content).strip(),
        log_result=RESULT_AI_REPLY,
        used_default=False,
        tool_calls_made=tool_calls_made,
    )


def generate_reply_sync(
    query: str,
    config: AgentConfig,
    *,
    shop_id: Optional[int] = None,
    default_reply: Optional[str] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    client: Optional[LLMClient] = None,
) -> AIReplyResult:
    """``generate_reply`` 的同步包装，便于非异步上下文调用。

    兼容「已有运行中事件循环」的场景：直接 ``asyncio.run`` 会在运行中的事件循环
    内抛 ``RuntimeError``，故当检测到当前线程已有运行循环时，切换到独立线程新建
    事件循环执行（与 channel_pdd.pdd_login._run_async 思路一致）。

    Args:
        见 generate_reply。

    Returns:
        AIReplyResult。
    """
    coro = generate_reply(
        query,
        config,
        shop_id=shop_id,
        default_reply=default_reply,
        history=history,
        client=client,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环：直接 run。
        return asyncio.run(coro)

    # 已在事件循环中：切换到独立线程运行新循环，避免「loop is already running」。
    result_box: Dict[str, Any] = {}

    def _runner() -> None:
        new_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(new_loop)
            result_box["value"] = new_loop.run_until_complete(coro)
        except Exception as exc:  # noqa: BLE001 - 捕获后在主线程重新抛出
            result_box["error"] = exc
        finally:
            new_loop.close()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result_box:
        raise result_box["error"]
    return result_box["value"]


__all__ = [
    "RESULT_AI_REPLY",
    "RESULT_AI_REPLY_FAILED",
    "DEFAULT_SYSTEM_PROMPT",
    "AIReplyResult",
    "generate_reply",
    "generate_reply_sync",
]
