"""nanobot 命令行接口 (CLI) 命令模块。"""

import asyncio
from contextlib import contextmanager, nullcontext

import os
import select
import signal
import sys
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
# 强制 Windows 控制台使用 UTF-8 编码
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        # 重新打开标准输出/错误流，使用 UTF-8 编码
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ============================================================================
# Typer 介绍
# ============================================================================
# Typer 是一个用于构建 CLI 应用的库，灵感来自 FastAPI。
# 核心概念：
#   - @app.command() 装饰器：将函数转换为 CLI 命令
#   - typer.Option()：定义命令行选项（类似 --flag）
#   - typer.Argument()：定义命令行参数（位置参数）
#   - typer.Callback：特殊函数，用于处理全局选项如 --version
#
# 示例：
#   @app.command()
#   def my_command(name: str = typer.Option("World", "--name", "-n")):
#       print(f"Hello, {name}!")
#   运行: python main.py my-command --name Alice 或 -n Alice
# ============================================================================

# ============================================================================
# Rich 介绍
# ============================================================================
# Rich 是一个用于在终端中输出富文本格式内容的库。
# 核心概念：
#   - Console：创建控制台实例，用于打印格式化内容
#   - [color]text[/color]：使用标签指定颜色（如 [red]红色[/red]）
#   - Table：创建表格
#   - Markdown：渲染 Markdown 文本
#   - Text：创建带有样式的文本对象
#
# 常用颜色标签：
#   [red], [green], [yellow], [blue], [cyan], [magenta], [white], [black]
#   [dim] - 暗淡文本
#   [bold] - 粗体
#   [b]text[/b] - 也可使用 b 标签表示粗体
#
# 示例：
#   console.print("[red]错误信息[/red]")
#   console.print("[bold]重要[/bold] [green]成功[/green]")
# ============================================================================

# ============================================================================
# Prompt Toolkit 介绍
# ============================================================================
# Prompt Toolkit 是一个用于构建交互式命令行应用的库。
# 核心概念：
#   - PromptSession：管理交互式输入会话
#   - prompt_async()：异步读取用户输入
#   - FileHistory：将输入历史保存到文件，支持上下箭头导航
#   - ANSI/HTML：格式化输出文本
#   - run_in_terminal()：在终端中执行打印操作（用于异步环境）
#   - patch_stdout()：在多线程/异步环境中正确处理标准输出
#
# 相比标准 input() 的优势：
#   - 支持多行粘贴（bracketed paste mode）
#   - 支持输入历史导航（上下箭头）
#   - 更好的光标控制和显示
#   - 支持自动补全（可扩展功能）
# ============================================================================

import typer
# Typer: CLI 应用框架
# from typer import Typer, Option, Argument, confirm
# Typer.Option() 参数：
#   - 第一个参数：默认值
#   - "--xxx"：长选项名
#   - "-x"：短选项名（单字符快捷方式）
#   - help：帮助文本
#   - is_eager=True：立即执行回调，不等待命令处理

from prompt_toolkit import PromptSession, print_formatted_text
# Prompt Toolkit: 交互式输入
# PromptSession: 管理交互式提示会话，包含历史记录、光标控制等
# print_formatted_text(): 在 prompt_toolkit 环境中安全打印格式化文本

from prompt_toolkit.application import run_in_terminal
# run_in_terminal(): 在终端中同步执行函数
# 用于在异步代码中安全地执行打印操作

from prompt_toolkit.formatted_text import ANSI, HTML
# ANSI: 解析 ANSI 转义序列（终端颜色代码）
# HTML: 解析 HTML 风格的标签（prompt_toolkit 专用格式）
# 注意：Rich 的 HTML 标签和 prompt_toolkit 的 HTML 标签语法略有不同

from prompt_toolkit.history import FileHistory
# FileHistory: 将用户输入历史保存到文件
# 支持跨会话保存历史记录，用户可以上下箭头浏览之前输入

from prompt_toolkit.patch_stdout import patch_stdout
# patch_stdout(): 上下文管理器，用于在异步环境中正确处理 stdout
# 防止 prompt 输出和 print 输出混在一起

from rich.console import Console
# Rich Console: 控制台输出对象
# 负责渲染带颜色的文本、表格、进度条等富文本内容

from rich.markdown import Markdown
# Rich Markdown: 将 Markdown 文本渲染为终端格式
# 支持标题、粗体、斜体、代码块、列表等

from rich.table import Table
# Rich Table: 创建格式化表格
# 支持列样式、对齐方式、标题等

from rich.text import Text
# Rich Text: 创建可样式的文本对象
# 与 [color]text[/color] 标签不同，Text 对象在代码中构建样式

from nanobot import __logo__, __version__
from nanobot.cli.stream import StreamRenderer, ThinkingSpinner
from nanobot.config.paths import get_workspace_path, is_default_workspace
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates

# ============================================================================
# Typer 应用入口
# ============================================================================
# app = typer.Typer() 创建一个新的 Typer CLI 应用
# 参数说明：
#   - name: CLI 命令名称（运行时的命令名，如 nanobot）
#   - context_settings: 全局上下文设置
#   - help_option_names: 触发帮助的选项（-h, --help）
#   - help: 应用描述（显示在帮助信息中）
#   - no_args_is_help: 无参数时显示帮助（不运行命令）
# ============================================================================
app = typer.Typer(
    name="nanobot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,
)

# Rich Console 实例：用于所有控制台输出
# Console 类是 Rich 的核心，负责：
#   - 颜色输出
#   - 表格渲染
#   - Markdown 渲染
#   - 检测终端支持的颜色系统
console = Console()

# 退出命令集合：用户输入这些命令时会结束交互式会话
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

# ============================================================================
# Prompt Toolkit 会话管理
# ============================================================================
# 这些变量用于管理全局的 prompt_toolkit 会话和终端状态
# ============================================================================

_PROMPT_SESSION: PromptSession | None = None
# 全局 PromptSession 实例
# 在交互模式下用于读取用户输入
# 初始化为 None，在首次使用时创建

_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit
# 保存原始终端属性（termios 设置）
# 终端属性包括：回显模式、行缓冲设置、终端大小等
# 程序退出时恢复这些设置，确保终端状态正确


def _flush_pending_tty_input() -> None:
    """
    丢弃在模型生成输出期间用户输入的字符。

    当 AI 模型正在生成回复时，用户可能不小心输入了字符。
    这些字符会被"缓冲"起来，如果不清理，输入会被当作下一轮对话。

    技术实现：
    1. 首先尝试使用 termios.tcflush() 清除输入缓冲区（最可靠）
    2. 如果 termios 不可用，使用 select.select() 手动读取并丢弃缓冲区内容
    """
    try:
        fd = sys.stdin.fileno()
        # sys.stdin.fileno() 返回标准输入的文件描述符
        # 文件描述符是一个整数，代表打开的文件/设备
        if not os.isatty(fd):
            # os.isatty() 检查文件描述符是否连接到一个终端
            # 如果不是终端（如重定向输入），直接返回
            return
    except Exception:
        return

    try:
        # termios.tcflush() 清除输入缓冲区
        # TCIFLUSH: 清除收到的但是没有读取的输入
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        # select.select() 用于多路复用 I/O
        # ([fd], [], [], 0) 非阻塞检查是否有数据可读
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            # os.read() 读取并丢弃数据
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """
    恢复终端到原始状态（回显、行缓冲等）。

    重要：在程序退出时必须调用此函数！
    否则终端可能会保持禁用回显状态，用户看不到输入的内容。

    使用 termios.tcsetattr() 恢复之前保存的终端属性。
    """
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        # TCSADRAIN: 等所有输出都发送完毕后再改变属性
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """
    创建 prompt_toolkit 会话，设置持久化的文件历史记录。

    PromptSession 是 prompt_toolkit 的核心对象，管理：
      - 输入提示符的显示
      - 用户输入的读取
      - 输入历史的维护
      - 编辑器模式（如 Ctrl+Z 切换到 vim 编辑）

    FileHistory 将历史记录保存到文件：
      - 用户可以按上下箭头浏览之前输入的内容
      - 历史记录跨会话持久化
      - 通常保存在 ~/.nanobot/history 或类似位置
    """
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # 保存终端状态，以便退出时恢复
    try:
        import termios
        # termios.tcgetattr() 获取终端属性
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from nanobot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    # 创建历史文件所在的目录（如果不存在）
    history_file.parent.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # PromptSession 初始化参数说明
    # =========================================================================
    # history=FileHistory(str(history_file))
    #   - 启用历史记录功能，数据保存到指定文件
    #   - 用户可以上下箭头浏览历史
    #
    # enable_open_in_editor=False
    #   - 禁用 Ctrl+Z/Vim 模式（使用外部编辑器编辑输入）
    #   - 通常保持 False 以简化交互
    #
    # multiline=False
    #   - 单行模式：按 Enter 提交输入
    #   - 如果设为 True，需要连续按两次 Enter 提交
    # =========================================================================
    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _make_console() -> Console:
    """
    创建 Rich Console 实例。

    每次调用都会创建一个新的 Console 实例。
    这样可以在不同的渲染环境中（如捕获输出）使用。
    """
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """
    将 Rich 输出渲染为 ANSI 转义序列字符串。

    这是连接 Rich 和 prompt_toolkit 的桥梁：

    问题：Rich 的 Console.print() 直接打印到终端，
          但在 prompt_toolkit 的 prompt() 过程中，
          直接打印会干扰输入体验。

    解决：使用 Console.capture() 捕获输出为字符串，
          然后通过 prompt_toolkit 的 ANSI() 类安全打印。

    参数：
      render_fn: 接收 Console 参数的函数，用于生成要捕获的输出

    返回：ANSI 转义序列字符串，可以安全地在 prompt_toolkit 中打印
    """
    # 创建用于捕获输出的 Console
    # force_terminal=True: 强制输出 ANSI 转义序列
    # color_system: 保持原有颜色系统（256色、真彩色等）
    # width: 保持原有终端宽度
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    # capture() 上下文管理器会捕获所有 print 输出
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """
    渲染并打印助手（AI）的回复，使用一致的终端样式。

    格式：
      [空行]
      [logo] nanobot
      [回复内容]
      [空行]
    """
    console = _make_console()
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata)
    console.print()
    console.print(f"[cyan]{__logo__} nanobot[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """
    根据条件返回适当的渲染对象。

    逻辑：
      1. 如果不渲染 Markdown → 返回纯文本 Text
      2. 如果元数据指定 render_as=text → 返回纯文本 Text
      3. 否则 → 返回 Markdown 渲染对象

    为什么不总是用 Markdown？
      - 某些命令输出需要保留原始格式（如代码块的换行）
      - Markdown 会将连续换行压缩为段落
    """
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


async def _print_interactive_line(text: str) -> None:
    """
    在交互模式下异步打印一行内容。

    用于在 AI 生成回复时打印进度信息。
    例如："正在搜索..." "正在执行命令..."

    使用 run_in_terminal() 确保在异步环境中正确输出。
    """
    def _write() -> None:
        # _render_interactive_ansi 将 Rich 打印转换为 ANSI 字符串
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        # print_formatted_text() 是 prompt_toolkit 的打印函数
        # ANSI() 解析 ANSI 转义序列
        # end="" 避免添加额外的换行
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """
    在交互模式下异步打印完整的 AI 回复。

    与 _print_agent_response 类似，但用于异步环境。
    格式：[空行] [logo] nanobot [回复] [空行]
    """
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} nanobot[/cyan]"),
                c.print(_response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


def _print_cli_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """
    打印 CLI 进度信息行。

    参数：
      text: 要打印的文本（如 "正在搜索..."）
      thinking: 加载动画（旋转器），如果提供会暂停它

    使用 nullcontext() 如果 thinking 为 None。
    """
    with thinking.pause() if thinking else nullcontext():
        console.print(f"  [dim]↳ {text}[/dim]")


async def _print_interactive_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """
    在交互模式下异步打印进度信息行。
    """
    with thinking.pause() if thinking else nullcontext():
        await _print_interactive_line(text)


def _is_exit_command(command: str) -> bool:
    """
    检查命令是否为退出命令。

    支持的退出命令：exit, quit, /exit, /quit, :q
    """
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """
    使用 prompt_toolkit 异步读取用户输入。

    prompt_toolkit 的优势：
      1. 多行粘贴（bracketed paste mode）
         - 粘贴大段代码时自动处理缩进
      2. 历史导航
         - 上箭头：上一条输入
         - 下箭头：下一条输入
      3. 清爽显示
         - 不会显示"幽灵字符"
         - 光标控制精确

    HTML 提示符格式：
      <b fg='ansiblue'>You:</b>
      - b: 粗体
      - fg='ansiblue': 前景色为蓝色（prompt_toolkit 的 HTML 格式）

    patch_stdout() 确保在读取输入时正确处理多线程输出。
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


# ============================================================================
# 版本命令回调
# ============================================================================
# typer.Option 的 callback 参数：
#   - 这是一个回调函数，在选项被解析时立即执行
#   - is_eager=True 确保这个回调在命令处理之前执行
#   - 如果回调调用 typer.Exit()，命令不会执行
# ============================================================================
def version_callback(value: bool):
    """处理 --version/-v 选项的回调函数。"""
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """
    nanobot 主入口命令。

    @app.callback() 装饰器使这个函数成为"回调"命令，
    即所有其他命令执行前都会运行的全局命令。
    通常用于定义全局选项。
    """
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive wizard"),
):
    """
    初始化 nanobot 配置和工作区。

    这是 nanobot 的首次设置向导。

    命令格式：nanobot onboard [OPTIONS]

    选项：
      --workspace/-w: 指定工作区目录（可选）
      --config/-c: 指定配置文件路径（可选）
      --wizard: 启动交互式向导模式

    typer.Option() 参数详解：
      - 第一个参数：默认值（None 表示可选）
      - "--workspace": 长选项名称
      - "-w": 短选项名称
      - help: 帮助文本

    typer.confirm()：
      提示用户输入 y/n 确认，返回布尔值。
    """
    from nanobot.config.loader import get_config_path, load_config, save_config, set_config_path
    from nanobot.config.schema import Config

    if config:
        config_path = Path(config).expanduser().resolve()
        # expanduser(): 展开 ~ 为用户主目录
        # resolve(): 解析为绝对路径，跟随符号链接
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        """应用命令行指定的工作区覆盖配置。"""
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # Create or update config
    if config_path.exists():
        if wizard:
            config = _apply_workspace_override(load_config(config_path))
        else:
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
            console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
            # typer.confirm() 显示一个确认提示，等待用户输入 y 或 n
            if typer.confirm("Overwrite?"):
                config = _apply_workspace_override(Config())
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                config = _apply_workspace_override(load_config(config_path))
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    # Run interactive wizard if enabled
    if wizard:
        from nanobot.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'nanobot onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1)
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    agent_cmd = 'nanobot agent -m "Hello!"'
    gateway_cmd = "nanobot gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    if wizard:
        console.print(f"  1. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. Start gateway: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
        console.print("     Get one at: https://openrouter.ai/keys")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]")


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """
    递归合并配置，只填充缺失的值，不覆盖用户已有的配置。

    这是一个深度合并函数：
      - 如果配置中有某项，保留它
      - 如果配置中缺少某项，使用默认值填充
      - 对于嵌套字典，递归应用相同逻辑
    """
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """
    为所有已发现的频道（内置 + 插件）注入默认配置。

    这个函数：
      1. 发现所有可用的频道
      2. 读取当前配置
      3. 为缺失的频道添加默认配置
      4. 保存更新后的配置
    """
    import json

    from nanobot.channels.registry import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _make_provider(config: Config):
    """
    根据配置创建合适的 LLM（大型语言模型）提供商实例。

    路由逻辑基于 ProviderSpec.backend：
      - openai_codex: OpenAI Codex 提供商
      - azure_openai: Azure OpenAI
      - anthropic: Anthropic (Claude)
      - openai_compat: OpenAI 兼容 API（默认）

    参数：
      config: 已加载的配置对象

    返回：
      配置好的 Provider 实例
    """
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.registry import find_by_name

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    # --- validation ---
    if backend == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            console.print("Set them in ~/.nanobot/config.json under providers.azure_openai section")
            console.print("Use the model field to specify the deployment name.")
            raise typer.Exit(1)
    elif backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not (p and p.api_key)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            console.print("[red]Error: No API key configured.[/red]")
            console.print("Set one in ~/.nanobot/config.json under providers section")
            raise typer.Exit(1)

    # --- instantiation by backend ---
    if backend == "openai_codex":
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider
        provider = OpenAICodexProvider(default_model=model)
    elif backend == "azure_openai":
        from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    elif backend == "anthropic":
        from nanobot.providers.anthropic_provider import AnthropicProvider
        provider = AnthropicProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    else:
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider
        provider = OpenAICompatProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
        )

    # 配置生成参数（temperature、max_tokens 等）
    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """
    加载配置，并可选地覆盖工作区路径。

    参数：
      config: 配置文件路径（如果为 None，使用默认路径）
      workspace: 工作区目录路径（如果提供，会覆盖配置中的值）

    返回：
      加载并处理过的 Config 对象
    """
    from nanobot.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """
    提示用户移除配置文件中过时的键。

    当配置中有已废弃的选项时，打印提示信息。
    """
    import json
    from nanobot.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )


def _migrate_cron_store(config: "Config") -> None:
    """
    一次性迁移：将旧的全局 cron 存储迁移到工作区。

    这确保每个工作区有独立的定时任务存储。
    """
    from nanobot.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(legacy_path), str(new_path))


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """
    启动 nanobot 网关服务。

    网关是 nanobot 的核心服务，负责任务调度和消息路由。
    它启动：
      - AgentLoop: 处理 AI 交互
      - ChannelManager: 管理各种聊天渠道（Telegram、微信等）
      - CronService: 执行定时任务
      - HeartbeatService: 执行心跳任务

    命令格式：nanobot gateway [OPTIONS]

    选项：
      --port/-p: 指定网关端口
      --workspace/-w: 指定工作区目录
      --verbose/-v: 启用详细日志输出
      --config/-c: 指定配置文件路径
    """
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    from nanobot.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace)
    port = port if port is not None else config.gateway.port

    console.print(f"{__logo__} Starting nanobot gateway version {__version__} on port {port}...")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.message import MessageTool
        from nanobot.utils.evaluator import evaluate_response

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            resp = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        response = resp.content if resp else ""

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            should_notify = await evaluate_response(
                response, job.payload.message, provider, agent.model,
            )
            if should_notify:
                from nanobot.bus.events import OutboundMessage
                await bus.publish_outbound(OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response,
                ))
        return response
    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        resp = await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

        # Keep a small tail of heartbeat history so the loop stays bounded
        # without losing all short-term context between runs.
        session = agent.sessions.get_or_create("heartbeat")
        session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
        agent.sessions.save(session)

        return resp.content if resp else ""

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from nanobot.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        timezone=config.agents.defaults.timezone,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback
            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show nanobot runtime logs during chat"),
):
    """
    直接与 AI 助手交互。

    支持两种模式：
      1. 单消息模式：使用 -m 指定消息，发送后立即返回结果
      2. 交互模式：不指定消息，进入对话循环

    命令格式：
      nanobot agent -m "你好"                    # 单消息模式
      nanobot agent                                # 交互模式

    选项：
      --message/-m: 发送的消息
      --session/-s: 会话 ID（默认 cli:direct）
      --workspace/-w: 工作区目录
      --config/-c: 配置文件路径
      --markdown/--no-markdown: 是否渲染 Markdown（默认启用）
      --logs/--no-logs: 是否显示运行时日志（默认关闭）

    typer.Option() 的 --markdown/--no-markdown 语法：
      这是一个布尔选项，前缀定义正向选项名，后缀定义负向选项名
      --markdown=True 表示启用 Markdown（默认）
      --no-markdown=True 表示禁用 Markdown
    """
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.cron.service import CronService

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
    )

    # Shared reference for progress callbacks
    _thinking: ThinkingSpinner | None = None

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        """CLI 进度回调函数。"""
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        _print_cli_progress_line(content, _thinking)

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            renderer = StreamRenderer(render_markdown=markdown)
            response = await agent_loop.process_direct(
                message, session_id,
                on_progress=_cli_progress,
                on_stream=renderer.on_delta,
                on_stream_end=renderer.on_end,
            )
            if not renderer.streamed:
                await renderer.close()
                _print_agent_response(
                    response.content if response else "",
                    render_markdown=markdown,
                    metadata=response.metadata if response else None,
                )
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from nanobot.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        # =========================================================================
        # 信号处理
        # =========================================================================
        # signal.signal() 用于注册信号处理器
        # 当进程收到特定信号时，会调用注册的函数
        #
        # SIGINT (Ctrl+C): 中断信号，优雅退出
        # SIGTERM: 终止信号，优雅退出
        # SIGHUP: 挂起信号，通常表示终端关闭
        # SIGPIPE: 写入已关闭的管道时触发
        # =========================================================================
        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            """交互模式主循环。"""
            # 创建后台任务运行 agent
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()  # 标记当前轮次是否完成
            turn_done.set()
            turn_response: list[tuple[str, dict]] = []  # 当前轮次的响应
            renderer: StreamRenderer | None = None

            async def _consume_outbound():
                """消费出站消息（AI 响应）。"""
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

                        if msg.metadata.get("_stream_delta"):
                            # 流式响应片段
                            if renderer:
                                await renderer.on_delta(msg.content)
                            continue
                        if msg.metadata.get("_stream_end"):
                            # 流式响应结束
                            if renderer:
                                await renderer.on_end(
                                    resuming=msg.metadata.get("_resuming", False),
                                )
                            continue
                        if msg.metadata.get("_streamed"):
                            # 标记为已流式完成
                            turn_done.set()
                            continue

                        if msg.metadata.get("_progress"):
                            # 进度消息（如"正在搜索..."）
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                await _print_interactive_progress_line(msg.content, _thinking)
                            continue

                        if not turn_done.is_set():
                            # 收集响应
                            if msg.content:
                                turn_response.append((msg.content, dict(msg.metadata or {})))
                            turn_done.set()
                        elif msg.content:
                            # 额外消息（多轮响应）
                            await _print_interactive_response(
                                msg.content,
                                render_markdown=markdown,
                                metadata=msg.metadata,
                            )

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        # 读取用户输入
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()
                        renderer = StreamRenderer(render_markdown=markdown)

                        # 发布入站消息到总线
                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                            metadata={"_wants_stream": True},
                        ))

                        await turn_done.wait()

                        if turn_response:
                            content, meta = turn_response[0]
                            if content and not meta.get("_streamed"):
                                if renderer:
                                    await renderer.close()
                                _print_agent_response(
                                    content, render_markdown=markdown, metadata=meta,
                                )
                        elif renderer and not renderer.streamed:
                            await renderer.close()
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


# ============================================================================
# Typer 子命令
# ============================================================================
# app.add_typer() 用于添加子命令组
# 这创建一个命令层级：主命令 + 子命令
#
# 示例：
#   channels_app = typer.Typer(help="管理渠道")
#   app.add_typer(channels_app, name="channels")
#   # 创建命令：nanobot channels status
# ============================================================================

channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """
    显示所有渠道的状态。

    命令：nanobot channels status

    使用 Rich Table 展示渠道信息：
      - Channel: 渠道名称
      - Enabled: 是否启用
    """
    from nanobot.channels.registry import discover_all
    from nanobot.config.loader import load_config

    config = load_config()

    # =========================================================================
    # Rich Table 用法
    # =========================================================================
    # Table 类用于创建格式化表格
    #
    # Table 参数：
    #   - title: 表格标题
    #
    # table.add_column() 添加列：
    #   - 第一个参数：列标题
    #   - style: 列样式（颜色）
    #
    # table.add_row() 添加行：
    #   - 参数数量需与列数匹配
    #   - 可以使用 Rich 标签（如 [green]）
    #
    # console.print(table) 打印整个表格
    # =========================================================================
    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]✓[/green]" if enabled else "[dim]✗[/dim]",
        )

    console.print(table)


def _get_bridge_dir() -> Path:
    """
    获取或构建桥接目录。

    桥接是用于某些渠道（Telegram 等）的 Node.js 服务。
    如果尚未构建，会自动安装依赖并编译。
    """
    import shutil
    import subprocess

    # User's bridge location
    from nanobot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # nanobot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall nanobot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(..., help="Channel name (e.g. weixin, whatsapp)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication even if already logged in"),
):
    """
    通过二维码或其他方式对渠道进行身份验证。

    命令：nanobot channels login <channel_name> [OPTIONS]

    参数：
      channel_name: 渠道名称（必需）
        - typer.Argument(...) 表示必需参数

    选项：
      --force/-f: 即使已登录也强制重新认证
    """
    from nanobot.channels.registry import discover_all
    from nanobot.config.loader import load_config

    config = load_config()
    channel_cfg = getattr(config.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        available = ", ".join(all_channels.keys())
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {available}")
        raise typer.Exit(1)

    console.print(f"{__logo__} {all_channels[channel_name].display_name} Login\n")

    channel_cls = all_channels[channel_name]
    channel = channel_cls(channel_cfg, bus=None)

    success = asyncio.run(channel.login(force=force))

    if not success:
        raise typer.Exit(1)


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """
    列出所有已发现的渠道（内置和插件）。

    命令：nanobot plugins list

    表格显示：
      - Name: 渠道名称
      - Source: 来源（builtin 内置 或 plugin 插件）
      - Enabled: 是否启用
    """
    from nanobot.channels.registry import discover_all, discover_channel_names
    from nanobot.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled", style="green")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """
    显示 nanobot 状态。

    命令：nanobot status

    显示信息：
      - 配置文件位置和状态
      - 工作区位置和状态
      - 当前使用的模型
      - 已配置的 API 提供商
    """
    from nanobot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from nanobot.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


# ============================================================================
# 装饰器模式：注册登录处理器
# ============================================================================
# _register_login 是一个装饰器工厂
# 用于将登录函数注册到全局字典中
#
# 用法：
#   @_register_login("provider_name")
#   def my_login():
#       ...
#
# 效果：
#   _LOGIN_HANDLERS["provider_name"] = my_login
# ============================================================================

_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    """装饰器：注册提供商登录处理器。"""
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """
    通过 OAuth 对提供商进行身份验证。

    命令：nanobot provider login <provider>

    参数：
      provider: OAuth 提供商名称
        - openai-codex
        - github-copilot
    """
    from nanobot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    """
    OpenAI Codex OAuth 登录处理器。

    使用 oauth_cli_kit 库进行 OAuth 交互式登录。
    """
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    """
    GitHub Copilot OAuth 登录处理器。

    使用设备流进行 OAuth 认证。
    """
    import asyncio

    from openai import AsyncOpenAI

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        client = AsyncOpenAI(
            api_key="dummy",
            base_url="https://api.githubcopilot.com",
        )
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
