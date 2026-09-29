"""Plugin configuration model.

The model drives the default ``config.toml``, validation and the MaiBot WebUI form.
Labels default to zh-CN (MaiBot's default locale) with en-US translations.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

from .constants import CONFIG_VERSION


def _ui(label: str, en_label: str, hint: str = "", en_hint: str = "", **extra: Any) -> dict[str, Any]:
    """Build ``json_schema_extra`` UI metadata with an en-US translation."""
    meta: dict[str, Any] = {"label": label, **extra}
    en: dict[str, str] = {"label": en_label}
    if hint:
        meta["hint"] = hint
        en["hint"] = en_hint or hint
    meta["i18n"] = {"en_US": en}
    return meta


def _section_i18n(title: str, description: str = "") -> dict[str, dict[str, str]]:
    return {"en_US": {"title": title, "description": description}}


class PluginSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件"
    __ui_order__: ClassVar[int] = 0
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = _section_i18n("Plugin")

    enabled: bool = Field(
        default=False,
        description="是否启用 Telegram 适配器",
        json_schema_extra=_ui("启用适配器", "Enable adapter", order=0),
    )
    config_version: str = Field(
        default=CONFIG_VERSION,
        description="配置结构版本",
        json_schema_extra={"label": "配置版本", "hidden": True, "disabled": True, "order": 99},
    )


class AccountSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "账号"
    __ui_order__: ClassVar[int] = 1
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = _section_i18n("Account")

    type: Literal["bot", "user"] = Field(
        default="bot",
        description="账号类型：bot 使用 Bot Token 登录；user 使用手机号登录（用户号 / userbot）",
        json_schema_extra=_ui(
            "账号类型", "Account type",
            "bot：Bot Token 登录；user：手机号登录（非官方客户端，存在封号风险）",
            "bot: log in with a bot token; user: log in with a phone number (unofficial client, may get limited)",
            order=0,
        ),
    )
    api_id: int = Field(
        default=0,
        description="Telegram API ID，Bot 与用户账号均必填",
        json_schema_extra=_ui(
            "API ID", "API ID",
            "在 https://my.telegram.org 申请。MTProto 协议要求 Bot 也必须提供",
            "Get it from https://my.telegram.org. MTProto requires it for bots too",
            order=1,
        ),
    )
    api_hash: str = Field(
        default="",
        description="Telegram API Hash，Bot 与用户账号均必填",
        json_schema_extra=_ui("API Hash", "API Hash", input_type="password", order=2),
    )
    bot_token: str = Field(
        default="",
        description="Bot Token（账号类型为 bot 时必填）",
        json_schema_extra=_ui(
            "Bot Token", "Bot token", "从 @BotFather 获取", "Get it from @BotFather",
            input_type="password", placeholder="123456:ABC-DEF...", order=3,
            depends_on="type", depends_value="bot",
        ),
    )
    phone: str = Field(
        default="",
        description="手机号（账号类型为 user 时必填，含国家码）",
        json_schema_extra=_ui(
            "手机号", "Phone number", "含国家码，例如 +8613800000000", "With country code, e.g. +15550000000",
            placeholder="+8613800000000", order=4, depends_on="type", depends_value="user",
        ),
    )
    password: str = Field(
        default="",
        description="两步验证密码（如账号开启了两步验证则必填）",
        json_schema_extra=_ui(
            "两步验证密码", "2FA password",
            "账号开启两步验证但此处留空时，登录会直接失败",
            "If the account has 2FA enabled and this is empty, login fails",
            input_type="password", order=5, depends_on="type", depends_value="user",
        ),
    )
    login_code: str = Field(
        default="",
        description="登录验证码：首次登录时适配器会请求验证码，收到后填入此处并保存",
        json_schema_extra=_ui(
            "登录验证码", "Login code",
            "首次登录时，Telegram 会把验证码发到你的其他设备。填入后保存即可继续登录；登录成功后可清空",
            "On first login Telegram sends a code to your other devices. Enter it here and save to continue; "
            "you may clear it after login succeeds",
            order=6, depends_on="type", depends_value="user",
        ),
    )
    session_name: str = Field(
        default="telegram_full",
        description="会话文件名，保存在插件数据目录中",
        json_schema_extra=_ui(
            "会话名", "Session name",
            "更换账号时请改名或删除旧会话文件", "Change it (or delete the old session file) when switching accounts",
            order=7,
        ),
    )

    @field_validator("api_hash", "bot_token", "phone", "password", "login_code", "session_name", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("api_id", mode="before")
    @classmethod
    def _to_int(cls, value: Any) -> int:
        try:
            return int(str(value).strip() or 0)
        except (TypeError, ValueError):
            return 0

    def validation_errors(self) -> list[str]:
        errors = []
        if self.api_id <= 0 or not self.api_hash:
            errors.append("account.api_id / account.api_hash are required (https://my.telegram.org)")
        if self.type == "bot" and not self.bot_token:
            errors.append("account.bot_token is required for bot accounts")
        if self.type == "user" and not self.phone:
            errors.append("account.phone is required for user accounts")
        if not self.session_name:
            errors.append("account.session_name must not be empty")
        return errors


class ConnectionSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "连接"
    __ui_order__: ClassVar[int] = 2
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = _section_i18n("Connection")

    proxy: str = Field(
        default="",
        description="代理地址，支持 socks5:// socks4:// http://，留空不使用",
        json_schema_extra=_ui(
            "代理", "Proxy", "例如 socks5://user:pass@127.0.0.1:1080", "e.g. socks5://user:pass@127.0.0.1:1080",
            placeholder="socks5://127.0.0.1:1080", order=0,
        ),
    )
    reconnect_max_delay: int = Field(
        default=300,
        ge=5,
        description="断线重连的最大退避间隔（秒）。Telethon 自身会先做快速重连，失败后由适配器按指数退避继续重试",
        json_schema_extra=_ui("最大重连间隔（秒）", "Max reconnect delay (s)", order=1),
    )
    flood_sleep_threshold: int = Field(
        default=60,
        ge=0,
        description="遇到 FloodWait 时自动等待的最长秒数，超过则直接报错",
        json_schema_extra=_ui("FloodWait 自动等待上限（秒）", "FloodWait auto-sleep limit (s)", order=2),
    )
    telethon_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="WARNING",
        description="Telethon 库自身的日志级别",
        json_schema_extra=_ui("Telethon 日志级别", "Telethon log level", order=3),
    )


class InboundSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "接收"
    __ui_order__: ClassVar[int] = 3
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = _section_i18n("Inbound")

    ignore_bot_messages: bool = Field(
        default=True,
        description="丢弃其他 Bot 发送的消息（无论 Telegram 的 Bot 间通信设置如何）",
        json_schema_extra=_ui("忽略 Bot 消息", "Ignore bot messages", order=0),
    )
    command_filter: Literal["off", "other_bots", "all"] = Field(
        default="other_bots",
        description="命令过滤：off 不过滤；other_bots 丢弃 /cmd@其他bot；all 丢弃所有 / 开头的命令（会影响 MaiBot 自身命令）",
        json_schema_extra=_ui(
            "命令过滤", "Command filter",
            "other_bots：仅丢弃发给其他 Bot 的命令；all：丢弃全部命令，MaiBot 插件命令也会失效",
            "other_bots: drop only commands addressed to other bots; all: drop every command, "
            "including MaiBot plugin commands",
            order=1,
        ),
    )
    reply_preview_length: int = Field(
        default=200,
        ge=0,
        description="被回复消息的内容预览最大长度（字符）",
        json_schema_extra=_ui("回复预览长度", "Reply preview length", order=2),
    )


class OutboundSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "发送"
    __ui_order__: ClassVar[int] = 4
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = _section_i18n("Outbound")

    text_format: Literal["markdown", "plain"] = Field(
        default="markdown",
        description="发送文本的格式：markdown 渲染为 Telegram 格式；plain 去除全部格式",
        json_schema_extra=_ui("文本格式", "Text format", order=0),
    )
    quote_policy: Literal["core", "never"] = Field(
        default="core",
        description="引用回复：core 由 MaiBot 决定是否引用；never 从不引用",
        json_schema_extra=_ui("引用回复", "Quote replies", order=1),
    )
    reply_to_bots: Literal["normal", "no_quote", "drop"] = Field(
        default="no_quote",
        description="回复其他 Bot 的消息时：normal 正常引用；no_quote 发送但不引用；drop 直接丢弃这条回复（不推荐）",
        json_schema_extra=_ui(
            "回复 Bot 消息", "Replying to bots",
            "no_quote 可避免 Bot 之间互相触发", "no_quote avoids bots triggering each other",
            order=2,
        ),
    )
    link_preview: bool = Field(
        default=False,
        description="发送的消息中是否显示链接预览",
        json_schema_extra=_ui("链接预览", "Link previews", order=3),
    )
    soft_length_warning: int = Field(
        default=1000,
        ge=0,
        description="文本宽度超过此值时记录警告（中日韩字符按 2、emoji 按 3 计）。硬上限为 Telegram 的 4096，超出将发送失败",
        json_schema_extra=_ui(
            "长文本警告阈值", "Long text warning threshold",
            "0 为关闭。超过 4096（UTF-16）会直接报错", "0 disables. Over 4096 (UTF-16) is always an error",
            order=4,
        ),
    )


class TelegramFullConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    account: AccountSection = Field(default_factory=AccountSection)
    connection: ConnectionSection = Field(default_factory=ConnectionSection)
    inbound: InboundSection = Field(default_factory=InboundSection)
    outbound: OutboundSection = Field(default_factory=OutboundSection)

    def connection_fingerprint(self) -> tuple[Any, ...]:
        """Fields whose change requires reconnecting the Telegram client."""
        a, c = self.account, self.connection
        return (
            self.plugin.enabled, a.type, a.api_id, a.api_hash, a.bot_token, a.phone, a.password,
            a.session_name, c.proxy, c.flood_sleep_threshold,
        )
