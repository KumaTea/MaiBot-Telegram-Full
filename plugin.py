"""MaiBot entry point for the Telethon based Telegram adapter."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, ClassVar, cast

from maibot_sdk import API, CONFIG_RELOAD_SCOPE_SELF, HookHandler, MaiBotPlugin, MessageGateway, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .tg_full.config import TelegramFullConfig
from .tg_full.constants import GATEWAY_NAME, PLATFORM, PROTOCOL
from .tg_full.runtime import ActionError, AdapterRuntime

_RAW_TOOL = "telegram_raw_api"
_BUTTON_SCHEMA = {
    "type": "array",
    "description": "一行按钮",
    "items": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "按钮上显示的文字"},
            "data": {"type": "string", "description": "点击后回传的数据（最多 64 字节，默认同 text）"},
            "url": {"type": "string", "description": "点击后打开的链接（与 data 二选一）"},
        },
        "required": ["text"],
    },
}


class TelegramFullPlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = TelegramFullConfig

    def __init__(self) -> None:
        super().__init__()
        self._runtime: AdapterRuntime | None = None
        self._fingerprint: tuple[Any, ...] | None = None

    def _settings(self) -> TelegramFullConfig:
        return cast(TelegramFullConfig, self.config)

    async def on_load(self) -> None:
        await self._reconcile()

    async def on_unload(self) -> None:
        await self._stop()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del version
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self.set_plugin_config(config_data)
        await self._reconcile()

    async def _reconcile(self) -> None:
        """Start, restart or hot-apply depending on what changed in the config."""
        cfg = self._settings()
        await self._sync_raw_tool()
        fingerprint = cfg.connection_fingerprint()
        if self._runtime is not None and fingerprint == self._fingerprint:
            self._runtime.provide_login_code(cfg.account.login_code)
            return

        await self._stop()
        self._fingerprint = fingerprint
        if not cfg.plugin.enabled:
            self.ctx.logger.info("Telegram adapter is disabled (plugin.enabled = false)")
            return
        problems = cfg.account.validation_errors()
        if problems:
            for problem in problems:
                self.ctx.logger.error("Telegram adapter not started: %s", problem)
            return
        self._runtime = AdapterRuntime(self.ctx, self._settings, self.ctx.logger)
        await self._runtime.start()

    async def _stop(self) -> None:
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            await runtime.stop()

    async def _sync_raw_tool(self) -> None:
        """Show the raw MTProto tool to MaiBot only while ``advanced.raw_api_enabled`` is on."""
        cfg = self._settings()
        name = f"{self.ctx.plugin_id}.{_RAW_TOOL}"
        try:
            if cfg.plugin.enabled and cfg.advanced.raw_api_enabled:
                await self.ctx.component.enable_component(name, "tool")
            else:
                await self.ctx.component.disable_component(name, "tool")
        except Exception as exc:
            self.ctx.logger.warning("Could not update the %s tool state: %r", _RAW_TOOL, exc)

    async def _action(self, action: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        """Run a tool/API action, turning failures into a result the caller (often an LLM) can read."""
        try:
            return await action
        except ActionError as exc:
            return {"success": False, "content": str(exc), "error": str(exc)}

    def _require_runtime(self) -> AdapterRuntime:
        if self._runtime is None:
            raise ActionError("The Telegram adapter is not running")
        return self._runtime

    # ---- hooks -------------------------------------------------------------------------

    @HookHandler(
        "maisaka.replyer.before_request",
        name="telegram_typing_indicator",
        description="MaiBot 开始生成回复时，在 Telegram 显示一次“正在输入…”",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
    )
    async def hook_typing_indicator(self, session_id: str = "", attempt: int = 1, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if self._runtime is not None:
            await self._runtime.on_reply_started(str(session_id or ""), int(attempt or 1))
        return {"action": "continue"}

    # ---- tools (for the LLM) -----------------------------------------------------------

    @Tool(
        "telegram_react",
        description=(
            "给一条消息添加表情回应（reaction）。"
            "emoji 须是 Telegram 支持的标准回应表情，例如 👍 👎 ❤️ 🔥 🥰 👏 😁 🤔 🤯 😱 😢 🎉 🤩 🙏 👌 🤡 😍 "
            "💯 🤣 ⚡ 🏆 💔 🤨 😐 😈 😴 😭 🤓 👻 👀 🙈 😇 🤝 🤗 🫡 🤪 🗿 🆒 😘 😎 😡；群聊可能只允许其中一部分。"
            "emoji 传空字符串可取消回应。big=true 会播放全屏大动画，表示强烈情绪。"
        ),
        parameters=[
            ToolParameterInfo(name="msg_id", param_type=ToolParamType.STRING, description="要回应的消息的 msg_id",
                              required=True),
            ToolParameterInfo(name="emoji", param_type=ToolParamType.STRING, description="一个回应表情",
                              required=True),
            ToolParameterInfo(name="big", param_type=ToolParamType.BOOLEAN, description="是否播放大动画",
                              required=False, default=False),
        ],
        visibility="visible",
    )
    async def tool_react(
        self, msg_id: str = "", emoji: str = "", big: bool = False, stream_id: str = "", chat_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().react(stream_id or chat_id, str(msg_id), str(emoji), bool(big)))

    @Tool(
        "telegram_send_buttons",
        description=(
            "（仅 Bot 账号）在当前 Telegram 聊天发送一条带内联按钮的消息，适合让用户在几个选项中点选。"
            "buttons 是按钮行组成的数组，每行是按钮数组；每个按钮为 {\"text\": 显示文字, \"data\": 回传数据} "
            "或 {\"text\": 显示文字, \"url\": 链接}。用户点击带 data 的按钮后，你会收到一条"
            "“[点击了按钮「…」]”的消息。text 支持 markdown。"
        ),
        parameters=[
            ToolParameterInfo(name="text", param_type=ToolParamType.STRING, description="消息正文", required=True),
            ToolParameterInfo(name="buttons", param_type=ToolParamType.ARRAY, description="按钮行数组",
                              required=True, items_schema=_BUTTON_SCHEMA),
        ],
        visibility="deferred",
    )
    async def tool_send_buttons(
        self, text: str = "", buttons: Any = None, stream_id: str = "", chat_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().send_buttons(stream_id or chat_id, str(text), buttons))

    @Tool(
        "telegram_post_long_text",
        description=(
            "把长文发布到 Telegra.ph，并在当前 Telegram 聊天发送“消息过长，点击查看”和链接（链接预览会显示标题与开头）。"
            "适用于超过 Telegram 单条消息"
            "上限（约 4096 字）或明显过长的回复。注意：Telegraph 页面对任何拿到链接的人公开且无法删除，"
            "切勿写入隐私或敏感内容；发布后可用 telegram_edit_long_text 修改内容或清空页面。"
            "markdown 支持标题、列表、引用、代码块、链接和图片。"
        ),
        parameters=[
            ToolParameterInfo(name="title", param_type=ToolParamType.STRING, description="文章标题（留空取第一行）",
                              required=False, default=""),
            ToolParameterInfo(name="markdown", param_type=ToolParamType.STRING, description="文章正文（markdown）",
                              required=True),
        ],
        visibility="visible",
    )
    async def tool_post_long_text(
        self, title: str = "", markdown: str = "", stream_id: str = "", chat_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(
            self._require_runtime().post_long_text(stream_id or chat_id, str(title or ""), str(markdown or ""))
        )

    @Tool(
        "telegram_edit_long_text",
        description=(
            "修改或清空之前用 telegram_post_long_text 发布的 Telegraph 页面，链接保持不变。"
            "page 为页面链接或路径；markdown 为新的完整正文（整体替换，不是追加）；title 留空保留原标题。"
            "clear=true 时清空页面：Telegraph 无法删除页面，清空会把标题和正文替换为“已清空”的占位内容。"
            "只能修改本适配器发布的页面。"
        ),
        parameters=[
            ToolParameterInfo(name="page", param_type=ToolParamType.STRING, description="页面链接或路径", required=True),
            ToolParameterInfo(name="markdown", param_type=ToolParamType.STRING, description="新的完整正文",
                              required=False, default=""),
            ToolParameterInfo(name="title", param_type=ToolParamType.STRING, description="新标题，留空保留原标题",
                              required=False, default=""),
            ToolParameterInfo(name="clear", param_type=ToolParamType.BOOLEAN, description="是否清空页面",
                              required=False, default=False),
        ],
        visibility="deferred",
    )
    async def tool_edit_long_text(
        self, page: str = "", markdown: str = "", title: str = "", clear: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(
            self._require_runtime().edit_long_text(str(page), str(markdown or ""), str(title or ""), bool(clear))
        )

    @Tool(
        "telegram_find_stickers",
        description=(
            "查找在 Telegram 聊天中见过的贴纸，可按贴纸代表的 emoji 筛选（如 😂）。"
            "返回 sticker_id、emoji 和识别出的描述；再用 telegram_send_sticker 发送原生贴纸。"
        ),
        parameters=[
            ToolParameterInfo(name="emoji", param_type=ToolParamType.STRING, description="按这个 emoji 筛选，留空列出最近的贴纸",
                              required=False, default=""),
            ToolParameterInfo(name="limit", param_type=ToolParamType.INTEGER, description="最多返回几个（1–30）",
                              required=False, default=8),
        ],
        visibility="deferred",
    )
    async def tool_find_stickers(self, emoji: str = "", limit: int = 8, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().find_stickers(str(emoji or ""), int(limit or 8)))

    @Tool(
        "telegram_send_sticker",
        description="在当前 Telegram 聊天发送一个原生贴纸（不是图片）。sticker_id 来自 telegram_find_stickers。",
        parameters=[
            ToolParameterInfo(name="sticker_id", param_type=ToolParamType.STRING, description="贴纸的 sticker_id",
                              required=True),
        ],
        visibility="deferred",
    )
    async def tool_send_sticker(
        self, sticker_id: str = "", stream_id: str = "", chat_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().send_sticker(stream_id or chat_id, str(sticker_id)))

    @Tool(
        "telegram_get_message",
        description="读取当前 Telegram 聊天中某条消息的内容、发送者和时间，适合查看不在上下文里的旧消息（例如被引用的消息）。",
        parameters=[
            ToolParameterInfo(name="msg_id", param_type=ToolParamType.STRING, description="消息的 msg_id",
                              required=True),
        ],
        visibility="deferred",
    )
    async def tool_get_message(
        self, msg_id: str = "", stream_id: str = "", chat_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().get_message(stream_id or chat_id, str(msg_id)))

    @Tool(
        "telegram_chat_info",
        description="获取当前 Telegram 聊天的信息：名称、类型、用户名、成员数和简介。",
        parameters=[],
        visibility="deferred",
    )
    async def tool_chat_info(self, stream_id: str = "", chat_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().chat_info(stream_id or chat_id))

    @Tool(
        _RAW_TOOL,
        description=(
            "【高风险，仅在其他工具都做不到时使用】直接调用任意 Telegram MTProto 方法（Telethon 原始请求）。"
            "method 为 TL 方法名，例如 messages.getHistory；params 为 JSON 对象，参数名与 https://tl.telethon.dev "
            "上的一致（snake_case 或 camelCase 均可），嵌套的 TL 对象写成 {\"_\": \"类型名\", ...}，"
            "字符串 \"$chat\" 代表当前聊天。调用前务必先查阅 https://tl.telethon.dev 确认方法和全部必填参数；"
            "不确定时不要调用，错误的调用可能造成不可撤销的后果。默认只允许读取类方法（get / search / check / "
            "resolve），其他方法需用户在配置中放行；删除、退群、账号、支付等方法默认被禁止。"
        ),
        parameters=[
            ToolParameterInfo(name="method", param_type=ToolParamType.STRING, description="TL 方法名",
                              required=True),
            ToolParameterInfo(name="params", param_type=ToolParamType.OBJECT, description="方法参数（JSON 对象）",
                              required=False, additional_properties=True),
        ],
        visibility="deferred",
        # The SDK always registers tools as enabled; _sync_raw_tool disables this one on load unless
        # advanced.raw_api_enabled is on, and raw_invoke refuses to run while it is off.
    )
    async def tool_raw_api(
        self, method: str = "", params: Any = None, stream_id: str = "", chat_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().raw_invoke(str(method), params, stream_id or chat_id))

    # ---- APIs (for other plugins) ------------------------------------------------------

    @API(
        "post_long_text",
        description="发布长文到 Telegra.ph（公开、永久、不可删除）：stream_id='', title, markdown, send_link=True；"
                    "stream_id 为空或 send_link=False 时只返回链接",
        public=True,
    )
    async def api_post_long_text(
        self, title: str, markdown: str, stream_id: str = "", send_link: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().post_long_text(stream_id, title, markdown, send_link))

    @API(
        "edit_long_text",
        description="修改或清空本适配器发布的 Telegraph 页面：page, markdown='', title='', clear=False",
        public=True,
    )
    async def api_edit_long_text(
        self, page: str, markdown: str = "", title: str = "", clear: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().edit_long_text(page, markdown, title, clear))

    @API("list_long_texts", description="本适配器发布过的 Telegraph 页面（最新在前）", public=True)
    async def api_list_long_texts(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().list_long_texts())

    @API("find_stickers", description="查找见过的贴纸：emoji='', limit=8", public=True)
    async def api_find_stickers(self, emoji: str = "", limit: int = 8, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().find_stickers(emoji, limit))

    @API("send_sticker", description="发送原生贴纸：stream_id, sticker_id", public=True)
    async def api_send_sticker(self, stream_id: str, sticker_id: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().send_sticker(stream_id, sticker_id))

    @API("get_message", description="读取一条消息：stream_id, msg_id", public=True)
    async def api_get_message(self, stream_id: str, msg_id: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().get_message(stream_id, msg_id))

    @API("get_chat_info", description="聊天信息：stream_id", public=True)
    async def api_get_chat_info(self, stream_id: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().chat_info(stream_id))

    @API(
        "raw_invoke",
        description="直接调用 MTProto 方法（需开启 advanced.raw_api_enabled，受允许/禁止名单限制）：method, params={}, stream_id=''",
        public=True,
    )
    async def api_raw_invoke(self, method: str, params: Any = None, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().raw_invoke(method, params, stream_id))

    @API("react", description="给消息添加表情回应：stream_id, msg_id, emoji, big=False", public=True)
    async def api_react(self, stream_id: str, msg_id: str, emoji: str, big: bool = False, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().react(stream_id, msg_id, emoji, big))

    @API(
        "send_buttons",
        description="发送带内联按钮的消息（仅 Bot）：stream_id, text, buttons=[[{text, data|url}]], receive_presses=True",
        public=True,
    )
    async def api_send_buttons(
        self, stream_id: str, text: str, buttons: Any, receive_presses: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().send_buttons(stream_id, text, buttons, receive_presses))

    @API(
        "register_callback_pattern",
        description="登记一个正则：数据匹配的按钮回调会交给 MaiBot（空字符串接收全部，不推荐）",
        public=True,
    )
    async def api_register_callback_pattern(self, pattern: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().register_callback_pattern(pattern))

    @API("unregister_callback_pattern", description="移除一个已登记的按钮回调正则", public=True)
    async def api_unregister_callback_pattern(self, pattern: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return await self._action(self._require_runtime().unregister_callback_pattern(pattern))

    @MessageGateway(
        route_type="duplex",
        name=GATEWAY_NAME,
        platform=PLATFORM,
        protocol=PROTOCOL,
        description="Telegram (Telethon / MTProto) duplex message gateway",
    )
    async def telegram_gateway(
        self,
        message: dict[str, Any],
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del route, metadata, kwargs
        if self._runtime is None:
            return {"success": False, "error": "The Telegram adapter is not running"}
        return await self._runtime.send_outbound(message)


def create_plugin() -> TelegramFullPlugin:
    return TelegramFullPlugin()
