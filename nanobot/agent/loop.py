"""
nanobot Agent Loop 模块 - AI 代理的核心运行循环。

本模块实现了 nanobot 的主代理循环，负责：
  1. 接收来自各个渠道的消息（CLI、TG、微信等）
  2. 调度斜杠命令
  3. 调用 AI 模型处理消息
  4. 管理工具调用
  5. 处理流式响应
  6. 管理会话历史

核心概念：
  - Message Bus（消息总线）：所有消息通过总线传递
  - AgentLoop：主循环类，协调各个组件
  - Session（会话）：管理对话历史和上下文
  - Tool（工具）：AI 可以调用的外部能力
  - Hook（钩子）：在特定时机插入自定义逻辑
"""

from __future__ import annotations

import asyncio
import json
import re
import os
import time
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

# ============================================================================
# 顶层导入说明
# ============================================================================
# 这些是从 nanobot 各模块导入的核心类：
#
# nanobot.agent.context.ContextBuilder
#   负责根据当前消息和历史构建完整的 AI prompt
#
# nanobot.agent.hook.AgentHook / AgentHookContext
#   钩子类：在 AI 处理的各个阶段插入自定义逻辑（流式回调、工具执行前等）
#
# nanobot.agent.memory.MemoryConsolidator
#   记忆整合器：定期将短期记忆整合到长期记忆
#
# nanobot.agent.runner.AgentRunSpec / AgentRunner
#   AI 运行规格和执行器：负责实际的 LLM 调用
#
# nanobot.agent.tools.*
#   各种内置工具（搜索、文件操作、命令执行、定时任务等）
#
# nanobot.command.CommandContext / CommandRouter
#   斜杠命令处理器
#
# nanobot.bus.events.InboundMessage / OutboundMessage
#   消息事件：消息总线传递的数据结构
#
# nanobot.session.manager.Session / SessionManager
#   会话管理器：管理对话历史和上下文
# ============================================================================

from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.memory import MemoryConsolidator
from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, WebSearchConfig
    from nanobot.cron.service import CronService


# ============================================================================
# 类型别名：让代码更易读
# ============================================================================
# Callable[[str], Awaitable[None]] 表示：
#   - 输入：一个字符串
#   - 输出：一个 awaitable（协程）
#   - 无返回值（None）
# ============================================================================
ProgressCallback = Callable[[str], Awaitable[None]]
"""进度回调：打印工具执行信息、思考过程等"""
StreamCallback = Callable[[str], Awaitable[None]]
"""流式回调：每个 token 到达时触发"""
StreamEndCallback = Callable[..., Awaitable[None]]
"""流结束回调：流式响应完成时触发"""


# ============================================================================
# AgentLoop 类 - 核心循环
# ============================================================================
# 这是 nanobot 的大脑，负责协调所有组件：
#   - 消息接收和分发
#   - 会话管理
#   - 工具调用
#   - AI 模型交互
#
# 架构：
#   ┌─────────────────────────────────────────────────────────┐
#   │                     Message Bus                         │
#   │  (消息总线：接收所有渠道的消息，分发给对应的处理器)       │
#   └─────────────────────────────────────────────────────────┘
#                           │
#                           ▼
#   ┌─────────────────────────────────────────────────────────┐
#   │                    AgentLoop.run()                      │
#   │  (主循环：持续消费消息队列，异步处理每个消息)             │
#   └─────────────────────────────────────────────────────────┘
#                           │
#             ┌─────────────┼─────────────┐
#             ▼             ▼             ▼
#       ┌──────────┐  ┌──────────┐  ┌──────────┐
#       │ Commands │  │ Session  │  │  Tools   │
#       │ (斜杠命令) │  │ (会话管理) │  │ (工具调用) │
#       └──────────┘  └──────────┘  └──────────┘
#             │             │             │
#             └─────────────┼─────────────┘
#                           ▼
#       ┌─────────────────────────────────────────────────────────┐
#       │              AgentRunner (AI 模型交互)                  │
#       │  - 构建 prompt                                          │
#       │  - 调用 LLM                                             │
#       │  - 解析工具调用                                         │
#       │  - 执行工具                                             │
#       └─────────────────────────────────────────────────────────┘
# ============================================================================


class AgentLoop:
    """
    nanobot 的主代理循环类。

    负责：
      - 从消息总线消费消息
      - 管理会话生命周期
      - 协调 AI 模型和工具
      - 处理流式响应
      - 内存整合

    关键设计：
      - 每个会话有独立的锁，确保同一会话的消息串行处理
      - 不同会话可以并发处理
      - 支持流式响应和回调机制
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str,
        max_iterations: int = 30,
        context_window_tokens: int = 100_000,
        web_search_config: Any = None,
        web_proxy: str | None = None,
        exec_config: Any = None,
        cron_service: Any = None,
        restrict_to_workspace: bool = True,
        session_manager: SessionManager | None = None,
        mcp_servers: Any = None,
        channels_config: Any = None,
        timezone: str = "UTC",
    ):
        """
        初始化 AgentLoop。

        参数：
          bus: 消息总线，用于接收和发送消息
          provider: LLM 提供商（如 OpenAI、Anthropic）
          workspace: 工作区路径
          model: 使用的 AI 模型名称
          max_iterations: 工具调用的最大迭代次数
            - 防止 AI 在工具调用中陷入无限循环
            - 达到限制后会返回当前的中间结果
          context_window_tokens: 上下文窗口大小（token 数）
            - 用于决定保留多少历史消息
            - 超过这个限制的消息会被截断或遗忘
          web_search_config: 网页搜索配置
          web_proxy: 网页请求代理
          exec_config: 命令执行配置
          cron_service: 定时任务服务
          restrict_to_workspace: 是否限制文件操作在工作区内
          session_manager: 会话管理器（如果为 None，会创建新的）
          mcp_servers: MCP 服务器配置（Model Context Protocol）
          channels_config: 渠道配置
          timezone: 时区
        """
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.timezone = timezone

        # =========================================================================
        # 会话管理
        # =========================================================================
        # SessionManager 负责：
        #   - 创建和获取会话
        #   - 保存和加载会话历史
        #   - 管理会话元数据
        #
        # 每个会话由 session_key 标识，格式通常是 "channel:chat_id"
        # 例如：cli:direct, tg:123456, weixin:openid
        # =========================================================================
        self._session_manager = session_manager
        # 动态导入避免循环依赖
        from nanobot.session.manager import SessionManager as _SM
        if self._session_manager is None:
            self._session_manager = _SM(workspace / "sessions")

        # 会话锁：确保每个会话的消息串行处理
        # 结构：{session_key: asyncio.Lock()}
        # 为什么需要锁？
        #   - 如果同一个会话快速收到多条消息
        #   - 不加锁会导致历史记录混乱
        #   - 加锁确保消息按顺序处理
        self._session_locks: dict[str, asyncio.Lock] = {}

        # =========================================================================
        # 上下文构建器
        # =========================================================================
        # ContextBuilder 负责根据当前消息和历史构建完整的 prompt
        # 包含：
        #   - 系统提示词
        #   - 工具定义
        #   - 历史消息
        #   - 当前消息
        # =========================================================================
        self.context = self._build_context_builder(
            provider, model, workspace, max_iterations,
            context_window_tokens, web_search_config, web_proxy,
            exec_config, restrict_to_workspace, mcp_servers,
        )

        # =========================================================================
        # 工具注册
        # =========================================================================
        # tools 是一个字典：{工具名: 工具实例}
        # 每个工具实现特定的外部能力（搜索、代码执行、文件操作等）
        # =========================================================================
        self.tools: dict[str, Any] = {}

        # 命令处理器
        from nanobot.command import CommandHandler
        self.commands = CommandHandler(self)

        # MCP 服务器栈（Model Context Protocol）
        self._mcp_stack: Any | None = None

        # 记忆整合器：定期将短期记忆整合到长期记忆
        self.memory_consolidator = MemoryConsolidator(
            self._session_manager,
            self.provider,
            model,
            workspace / "memory",
            timezone,
        )

        # 渠道配置
        self._channels_config = channels_config

        # 运行状态标志
        self._running = False

        # 并发控制门（用于限制全局并发数）
        self._concurrency_gate: Any | None = None

        # 最后一次 AI 调用的 usage 信息（token 消耗等）
        self._last_usage: dict[str, Any] | None = None

        # 后台任务列表（程序退出时需要清理）
        self._background_tasks: list[asyncio.Task] = []

        # LLM 响应中的 think 标签最大字符数（用于截断过长输出）
        self._THINK_TRUNCATE = 2000
        # 工具结果最大字符数（超过会截断）
        self._TOOL_RESULT_MAX_CHARS = 8000

    # =========================================================================
    # 工具管理
    # =========================================================================

    def set_tools(self, tools: dict[str, Any]) -> None:
        """
        设置可用工具字典。

        工具是 AI 可以调用的外部函数，每个工具有：
          - name: 工具名称
          - description: 工具描述（AI 根据描述决定何时调用）
          - parameters: 参数定义（JSON Schema 格式）
          - execute: 执行函数

        参数：
          tools: {工具名: 工具实例} 的字典
        """
        self.tools = tools

    def _set_tool_context(
        self, channel: str, chat_id: str, message_id: str | None,
    ) -> None:
        """
        设置工具执行上下文。

        某些工具需要知道当前的消息上下文（如 Telegram 频道 ID）。
        这个函数将上下文信息传递给需要它的工具。

        参数：
          channel: 消息来源渠道
          chat_id: 聊天 ID
          message_id: 消息 ID
        """
        if tool := self.tools.get("message"):
            tool.set_context(channel=channel, chat_id=chat_id, message_id=message_id)
        if tool := self.tools.get("cron"):
            tool.set_context(channel=channel, chat_id=chat_id)

    @property
    def sessions(self) -> SessionManager:
        """获取会话管理器。"""
        return self._session_manager

    # =========================================================================
    # MCP 服务器
    # =========================================================================
    # MCP (Model Context Protocol) 是一种标准化的工具协议
    # 允许动态加载外部工具服务

    async def _connect_mcp(self) -> None:
        """
        连接到已配置的 MCP 服务器。

        MCP 服务器提供额外的工具能力，通过标准协议调用。
        """
        if self._mcp_stack is not None or not self.mcp_servers:
            return

        try:
            from mcp import ClientSession as MCPClientSession
            from mcp.client.stdio import stdio_client

            # 解析 MCP 服务器配置
            servers = self.mcp_servers
            if not isinstance(servers, list):
                servers = [servers]

            # 构建 MCP 客户端栈
            self._mcp_stack = StdioServerStack()  # type: ignore[name-defined]
            for srv in servers:
                if srv.get("enabled", True):
                    self._mcp_stack.add_server(srv)

            await self._mcp_stack.connect()

            # 从 MCP 服务器获取可用工具
            # 这里简化了，实际需要更完整的 MCP 协议实现
        except ImportError:
            logger.debug("MCP not available")
        except Exception:
            logger.exception("Failed to connect MCP")

    # =========================================================================
    # 上下文构建器
    # =========================================================================
    # ContextBuilder 负责将各种来源的信息组装成完整的 AI prompt

    def _build_context_builder(self, provider, model, workspace, *args) -> ContextBuilder:
        """
        创建上下文构建器实例。

        上下文构建器根据配置构建 AI 需要的完整上下文，包括：
          - 系统提示词（System Prompt）
          - 可用工具定义
          - 内存上下文
          - 当前消息
        """
        from nanobot.agent.context import ContextBuilder
        from nanobot.agent.tools import (
            BashTool, BrowserTool, CronTool, DocsSearchTool,
            FileReadTool, FileWriteTool, GlobTool, GrepTool,
            HttpTool, ImageGenTool, InspectTool, MessageTool,
            NavTool, ThinkTool, WebSearchTool,
        )

        # 注册内置工具
        tools: dict[str, Any] = {}
        config = args[7] if len(args) > 7 else None  # exec_config
        restrict = args[8] if len(args) > 8 else True  # restrict_to_workspace

        # 网页搜索工具
        web_search = args[4] if len(args) > 4 else None
        web_proxy = args[5] if len(args) > 5 else None
        if web_search:
            tools["web_search"] = WebSearchTool(web_search, web_proxy)

        # 文件操作工具
        tools["file_read"] = FileReadTool(workspace, restrict)
        tools["glob"] = GlobTool(workspace, restrict)
        tools["grep"] = GrepTool(workspace, restrict)
        tools["nav"] = NavTool(workspace, restrict)
        tools["file_write"] = FileWriteTool(workspace, restrict)
        tools["inspect"] = InspectTool(workspace, restrict)

        # 命令执行工具
        if config:
            tools["bash"] = BashTool(config, workspace, restrict)

        # 其他内置工具
        tools["think"] = ThinkTool()
        tools["message"] = MessageTool()
        tools["http"] = HttpTool()

        # 定时任务工具
        cron_svc = args[6] if len(args) > 6 else None
        if cron_svc:
            tools["cron"] = CronTool(cron_svc)

        # 文档搜索
        tools["docs_search"] = DocsSearchTool()

        # 图片生成
        tools["image_gen"] = ImageGenTool()

        # 浏览器工具
        if browser_cfg := getattr(config, "browser", None):
            tools["browser"] = BrowserTool(browser_cfg, workspace)

        # 设置工具并创建上下文构建器
        self.tools = tools
        return ContextBuilder(provider, model, tools, workspace, *args)

    @property
    def runner(self) -> AgentRunner:
        """
        获取 AgentRunner 实例。

        AgentRunner 负责实际的 AI 模型调用：
          - 发送请求到 LLM
          - 处理流式响应
          - 解析工具调用
          - 管理重试逻辑
        """
        from nanobot.agent.runner import AgentRunner
        return AgentRunner(self.provider, self.model)

    # =========================================================================
    # 流式响应处理
    # =========================================================================
    # 流式响应（Streaming）是指 AI 边生成边返回
    # 而不是等全部生成完毕再返回
    # 这样用户体验更好，可以实时看到 AI 的思考过程

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """
        移除文本中的 <think>...</think> 标签块。

        某些 AI 模型（如 o1、Claude）会在响应中嵌入思考过程，
        这些内容不应该显示给用户，或者需要特殊处理。

        <think> 标签的作用：
          - 模型在标签内展示推理过程
          - 最终回复不包含这些内容
          - 但 API 返回时可能包含这些标签
        """
        if not text:
            return None
        # 查找 <think>...</think> 标签并移除
        # 使用简单的字符串处理，而不是正则表达式
        import re
        return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """
        将工具调用列表格式化为简洁的提示文本。

        用于在 AI 执行工具时向用户显示：
          "正在调用 web_search("query")..."

        格式：工具名("第一个参数的前40字符...")

        参数：
          tool_calls: 工具调用列表

        返回：
          格式化后的提示文本
        """
        def _fmt(tc):
            # 获取第一个参数值
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            # 截断过长的参数
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    # =========================================================================
    # 核心循环
    # =========================================================================

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """
        运行代理迭代循环。

        这是 AI 处理的实际执行函数，负责：
          1. 调用 LLM 获取响应
          2. 解析工具调用
          3. 执行工具
          4. 将工具结果反馈给 AI
          5. 重复直到 AI 返回最终答案或达到最大迭代次数

        参数：
          initial_messages: 初始消息列表（包含历史和当前消息）
          on_progress: 进度回调（如打印"正在搜索..."）
          on_stream: 流式回调（每个 token 到达时）
          on_stream_end: 流结束回调（流式响应完成时）
          channel: 当前渠道
          chat_id: 当前聊天 ID
          message_id: 当前消息 ID

        返回：
          (最终内容, 使用的工具列表, 所有消息)

        回调机制：
          - on_stream(delta): 每个 token 到达时调用
          - on_stream_end(resuming): 流结束，resuming=True 表示还要执行工具
          - before_execute_tools(): 工具执行前调用
        """
        loop_self = self

        # =========================================================================
        # AgentHook - 钩子类
        # =========================================================================
        # 钩子是一种在特定时机插入自定义逻辑的模式
        # AgentHook 定义了多个钩子点：
        #   - wants_streaming(): 是否启用流式处理
        #   - on_stream(): 每个 token 到达时
        #   - on_stream_end(): 流结束时
        #   - before_execute_tools(): 工具执行前
        #   - finalize_content(): 完成后处理内容
        # =========================================================================

        class _LoopHook(AgentHook):
            """
            代理循环钩子实现。

            这个内部类定义了与 AI 交互的各个阶段的回调。
            """

            def __init__(self) -> None:
                self._stream_buf = ""  # 流式缓冲区，累积已接收的 token

            def wants_streaming(self) -> bool:
                """是否启用流式回调。"""
                return on_stream is not None

            async def on_stream(self, context: AgentHookContext, delta: str) -> None:
                """
                每个 token 到达时调用。

                技术细节：
                  - 增量更新：只传递新增的 token，而不是全部
                  - 过滤 think 标签：移除 AI 的思考过程
                """
                from nanobot.utils.helpers import strip_think

                prev_clean = strip_think(self._stream_buf)
                self._stream_buf += delta
                new_clean = strip_think(self._stream_buf)
                # 计算增量（过滤后的新内容）
                incremental = new_clean[len(prev_clean):]
                if incremental and on_stream:
                    await on_stream(incremental)

            async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
                """
                流式响应结束时调用。

                参数：
                  resuming: True 表示后面还有工具调用（AI 需要工具结果）
                            False 表示这是最终回复
                """
                if on_stream_end:
                    await on_stream_end(resuming=resuming)
                self._stream_buf = ""  # 清空缓冲区

            async def before_execute_tools(self, context: AgentHookContext) -> None:
                """
                工具执行前调用。

                做什么：
                  1. 通过 on_progress 通知用户正在执行什么
                  2. 记录工具调用日志
                  3. 设置工具上下文
                """
                if on_progress:
                    if not on_stream:
                        # 非流式模式：显示 AI 的思考过程
                        thought = loop_self._strip_think(context.response.content if context.response else None)
                        if thought:
                            await on_progress(thought)
                    # 显示工具调用提示
                    tool_hint = loop_self._strip_think(loop_self._tool_hint(context.tool_calls))
                    await on_progress(tool_hint, tool_hint=True)
                for tc in context.tool_calls:
                    args_str = json.dumps(tc.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tc.name, args_str[:200])
                loop_self._set_tool_context(channel, chat_id, message_id)

            def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
                """
                最终内容后处理。

                移除 think 标签，确保返回给用户的内容干净。
                """
                return loop_self._strip_think(content)

        # =========================================================================
        # AgentRunSpec - 运行规格
        # =========================================================================
        # 定义一次 AI 运行的所有参数
        # =========================================================================
        result = await self.runner.run(AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.tools,
            model=self.model,
            max_iterations=self.max_iterations,
            hook=_LoopHook(),
            error_message="Sorry, I encountered an error calling the AI model.",
            concurrent_tools=True,  # 允许多个工具同时执行
        ))
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages

    # =========================================================================
    # 主循环 - 消息消费
    # =========================================================================

    async def run(self) -> None:
        """
        运行代理主循环。

        这是一个无限循环，持续：
          1. 从消息总线消费入站消息
          2. 优先处理高优先级命令
          3. 将普通消息分发给处理任务

        架构设计：
          - 使用 asyncio.create_task() 创建异步任务
          - 每个消息创建一个任务，实现并发处理
          - 同一会话的任务共享锁，确保串行处理
        """
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                # 等待下一条消息，超时 1 秒用于检查 _running 标志
                # 这样可以实现优雅退出，而不需要关闭整个程序
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                # 超时正常，继续循环检查状态
                continue
            except asyncio.CancelledError:
                # 任务取消
                # 检查是否是真的取消（shutdown）还是从集成库泄漏的
                if not self._running or asyncio.current_task().cancelling():
                    raise  # 重新抛出，交由上层处理
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            # =========================================================================
            # 优先命令处理
            # =========================================================================
            # 某些命令需要立即处理，不经过正常的话流程
            # 例如：/stop（停止当前处理）、/clear（清除会话）
            if self.commands.is_priority(raw):
                ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=self)
                result = await self.commands.dispatch_priority(ctx)
                if result:
                    await self.bus.publish_outbound(result)
                continue

            # =========================================================================
            # 普通消息分发
            # =========================================================================
            # 为每条消息创建一个异步任务
            # 这样可以同时处理多条消息
            task = asyncio.create_task(self._dispatch(msg))
            # 将会话与任务关联，用于追踪和清理
            self._active_tasks.setdefault(msg.session_key, []).append(task)
            # 任务完成时的回调：清理已完成的任務
            task.add_done_callback(
                lambda t, k=msg.session_key: (
                    self._active_tasks.get(k, []) and
                    self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None
                )
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """
        分发消息到对应的处理器。

        设计原则：
          - 同一会话的消息串行处理（通过锁）
          - 不同会话的消息可以并发处理
          - 支持流式响应

        参数：
          msg: 入站消息
        """
        # 获取或创建会话锁
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        # 锁保护同一会话，并发门控制全局并发
        async with lock, gate:
            try:
                on_stream = on_stream_end = None
                if msg.metadata.get("_wants_stream"):
                    # =========================================================================
                    # 流式响应处理
                    # =========================================================================
                    # 流式响应的元数据约定：
                    #   _stream_delta: 每个 token 的片段
                    #   _stream_end: 流结束信号
                    #   _resuming: 流结束后是否还有内容（执行工具）
                    #   _stream_id: 分段 ID，用于客户端组装
                    # =========================================================================
                    stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                    stream_segment = 0

                    def _current_stream_id() -> str:
                        return f"{stream_base_id}:{stream_segment}"

                    async def on_stream(delta: str) -> None:
                        """发布每个 token 片段。"""
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content=delta,
                            metadata={
                                "_stream_delta": True,
                                "_stream_id": _current_stream_id(),
                            },
                        ))

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        """发布流结束信号。"""
                        nonlocal stream_segment
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="",
                            metadata={
                                "_stream_end": True,
                                "_resuming": resuming,
                                "_stream_id": _current_stream_id(),
                            },
                        ))
                        stream_segment += 1

                # 调用消息处理器
                response = await self._process_message(
                    msg, on_stream=on_stream, on_stream_end=on_stream_end,
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    # CLI 渠道即使没有响应也要发送确认
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    # =========================================================================
    # MCP 和后台任务清理
    # =========================================================================

    async def close_mcp(self) -> None:
        """
        清理 MCP 连接和后台任务。

        在程序退出前调用，确保：
          1. 所有后台任务完成
          2. MCP 连接正确关闭
        """
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK 取消清理时会有噪音，但无害

    def _schedule_background(self, coro) -> None:
        """
        调度协程为追踪的后台任务。

        后台任务的特点：
          - 不阻塞主流程
          - 在后台默默执行
          - 程序退出时会被清理

        参数：
          coro: 要执行的协程
        """
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """
        停止代理循环。

        设置 _running = False 后，主循环的 wait_for 超时会自然退出。
        """
        self._running = False
        logger.info("Agent loop stopping")

    # =========================================================================
    # 消息处理核心
    # =========================================================================

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """
        处理单条入站消息并返回响应。

        消息处理流程：
          1. 解析消息来源（CLI、TG、微信等）
          2. 获取或创建会话
          3. 处理斜杠命令
          4. 可能进行记忆整合
          5. 构建 AI 上下文
          6. 调用 _run_agent_loop 执行 AI
          7. 保存响应到会话历史
          8. 返回出站消息

        参数：
          msg: 入站消息
          session_key: 可选的会话键（覆盖 msg.session_key）
          on_progress: 进度回调
          on_stream: 流式回调
          on_stream_end: 流结束回调

        返回：
          出站消息，如果不需要响应则返回 None
        """
        # =========================================================================
        # 系统消息处理
        # =========================================================================
        # 系统消息来自内部组件（如子代理、定时任务）
        # 需要特殊处理：消息来源在 chat_id 中（"channel:chat_id"）
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)

            # 可能触发记忆整合
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=0)

            # 系统消息的角色取决于发送者
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                current_role=current_role,
            )
            final_content, _, all_msgs = await self._run_agent_loop(
                messages, channel=channel, chat_id=chat_id,
                message_id=msg.metadata.get("message_id"),
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # =========================================================================
        # 斜杠命令处理
        # =========================================================================
        # 斜杠命令以 / 开头，如 /help, /clear, /model
        # 这些命令不经过 AI，直接在本地处理
        raw = msg.content.strip()
        ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        if result := await self.commands.dispatch(ctx):
            return result

        # 可能触发记忆整合
        await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        # 设置工具上下文
        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))

        # 如果有消息工具，标记新的一轮开始
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        # =========================================================================
        # 构建 AI 上下文
        # =========================================================================
        # build_messages 会根据配置组装完整的消息列表：
        #   - 系统提示词（可能包含动态上下文）
        #   - 历史消息（根据 context_window_tokens 截断）
        #   - 当前消息（带媒体内容）
        # =========================================================================
        history = session.get_history(max_messages=0)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            """通过消息总线发送进度更新。"""
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        # 执行 AI 循环
        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=msg.channel, chat_id=msg.chat_id,
            message_id=msg.metadata.get("message_id"),
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # 保存到会话历史
        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)
        self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))

        # 如果消息工具已经处理了发送，不需要返回
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None:
            meta["_streamed"] = True
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=meta,
        )

    # =========================================================================
    # 会话历史管理
    # =========================================================================

    @staticmethod
    def _image_placeholder(block: dict[str, Any]) -> dict[str, str]:
        """
        将内联图片块转换为紧凑的文本占位符。

        原因：图片数据（base64）太大，不能存储在会话历史中。
        用文本占位符替代，保留图片存在的信息。
        """
        path = (block.get("_meta") or {}).get("path", "")
        return {"type": "text", "text": f"[image: {path}]" if path else "[image]"}

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """
        在写入会话历史前，清理敏感的多模态内容。

        清理策略：
          1. 移除运行时上下文标签（_RUNTIME_CONTEXT_TAG）
          2. 将 base64 图片转为文本占位符
          3. 截断过长的文本

        参数：
          content: 消息内容块列表
          truncate_text: 是否截断超长文本
          drop_runtime: 是否移除运行时上下文

        返回：
          清理后的内容块列表
        """
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            # 移除运行时上下文
            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            # 处理 base64 图片
            if (
                block.get("type") == "image_url"
                and block.get("image_url", {}).get("url", "").startswith("data:image/")
            ):
                filtered.append(self._image_placeholder(block))
                continue

            # 处理普通文本
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if truncate_text and len(text) > self._TOOL_RESULT_MAX_CHARS:
                    text = text[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """
        保存新一轮的消息到会话中。

        处理逻辑：
          1. 跳过已保存的历史消息（skip 参数）
          2. 跳过空的助手消息（会污染上下文）
          3. 截断超长的工具结果
          4. 清理图片等敏感内容
          5. 移除运行时上下文标签
          6. 添加时间戳

        参数：
          session: 目标会话
          messages: 所有消息（包括历史和新的）
          skip: 跳过的消息数（历史消息）
        """
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")

            # 跳过空的助手消息
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # 这些消息会污染上下文

            if role == "tool":
                # 工具结果可能很长，需要截断
                if isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                    entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                # 移除运行时上下文前缀
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered

            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    # =========================================================================
    # 直接处理接口
    # =========================================================================

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """
        直接处理消息并返回响应（不通过消息总线）。

        适用于：
          - 单次调用场景（如命令行 -m 参数）
          - 需要同步等待结果的场景

        与 run() + _dispatch() 的区别：
          - 这个是直接调用，立即返回
          - run() 是启动后台循环，持续处理消息

        参数：
          content: 消息内容
          session_key: 会话键
          channel: 渠道
          chat_id: 聊天 ID
          on_progress: 进度回调
          on_stream: 流式回调
          on_stream_end: 流结束回调

        返回：
          出站消息
        """
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(
            msg, session_key=session_key, on_progress=on_progress,
            on_stream=on_stream, on_stream_end=on_stream_end,
        )
