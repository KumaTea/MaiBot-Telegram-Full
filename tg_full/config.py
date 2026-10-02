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
        description="账号类型：bot 使用 Bot Token 登录；user 使用手机号登录",
        json_schema_extra=_ui(
            "账号类型", "Account type",
            "bot：Bot Token 登录；user：手机号登录",
            "bot: log in with a bot token; user: log in with a phone number",
            order=0,
        ),
    )
    api_id: int = Field(
        default=0,
        description="Telegram API ID，Bot 与用户账号均必填",
        json_schema_extra=_ui(
            "API ID", "API ID",
            "在 https://my.telegram.org 申请。Bot 也必须提供",
            "Get it from https://my.telegram.org. Required for bots too",
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
        description="手机号（账号类型为 user 且尚未登录时必填，含国家码）",
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
            "账号开启两步验证时需填写",
            "Must be filled if the account has 2FA enabled",
            input_type="password", order=5, depends_on="type", depends_value="user",
        ),
    )
    login_code: str = Field(
        default="",
        description="登录验证码：首次登录时适配器会请求验证码，收到后填入此处并保存",
        json_schema_extra=_ui(
            "登录验证码", "Login code",
            "首次登录时，Telegram 会把验证码发到你的其他设备。填入后点击保存即可继续登录",
            "On first login Telegram sends a code to your other devices. Enter it here and save to continue",
            order=6, depends_on="type", depends_value="user",
        ),
    )
    session_name: str = Field(
        default="telegram_full",
        description="会话文件名，保存在插件数据目录中",
        json_schema_extra=_ui(
            "会话名", "Session name",
            "更换账号时请改名或删除旧会话文件", "Change it or delete the old session file when switching accounts",
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
    catch_up: bool = Field(
        default=True,
        description="断线重连或重启后，补收离线期间错过的消息（类似 Bot API 的 update offset）",
        json_schema_extra=_ui(
            "补收离线消息", "Catch up on missed messages",
            "接收进度保存在会话文件中", "Progress is kept in the session file",
            order=4,
        ),
    )
    catch_up_max_age: int = Field(
        default=10,
        ge=0,
        description=(
            "补收到的、发送时间早于这么多分钟前的消息不单独触发回复：有新消息时随之一起投递，否则只作为上下文；"
            "0 表示不限"
        ),
        json_schema_extra=_ui(
            "补收消息回复时限（分钟）", "Catch-up reply limit (min)",
            "避免长时间离线后对很久以前的消息逐一回复",
            "Older missed messages join the next new message instead of each getting a reply",
            order=5,
        ),
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
    command_filter_mode: Literal["drop", "stack"] = Field(
        default="drop",
        description="被过滤的命令如何处理：drop 直接丢弃；stack 保留在上下文中，但不单独触发回复（随下一条普通消息一起交给 MaiBot）",
        json_schema_extra=_ui("命令过滤方式", "Filtered commands", order=2),
    )
    own_messages: Literal["context", "drop"] = Field(
        default="context",
        description="用户账号：你在其他设备上用这个账号亲自发的消息。context 作为麦麦自己说过的话交给 MaiBot；drop 丢弃",
        json_schema_extra=_ui(
            "本账号手动发送的消息", "Messages you send yourself",
            "仅用户账号有效。适配器自己发出的消息不会重复回传",
            "User accounts only. Messages sent by the adapter itself are never echoed back",
            order=3,
        ),
    )
    reply_preview_length: int = Field(
        default=200,
        ge=0,
        description="被回复消息的内容预览最大长度（字符）",
        json_schema_extra=_ui("回复预览长度", "Reply preview length", order=4),
    )
    reactions: Literal["off", "context", "smart", "notice"] = Field(
        default="smart",
        description=(
            "表情回应：off 忽略；context 只写入上下文（不触发回复）；"
            "smart 对本账号消息的回应和长按大表情以通知告知（可能引发回复），其余写入上下文；notice 全部以通知告知"
        ),
        json_schema_extra=_ui(
            "表情回应", "Reactions",
            "Bot 账号需为群管理员才能收到回应；长按大表情仅用户账号可见",
            "Bots only receive reactions as group admins; long-press big reactions are visible to user accounts only",
            order=5,
        ),
    )
    callback_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "按钮回调：按下的按钮数据匹配这些正则时，交给 MaiBot；其余一律丢弃。"
            "MaiBot 通过工具发送的按钮会自动登记，无需填写。填空字符串会接收全部回调（不推荐）"
        ),
        json_schema_extra=_ui("接收的按钮回调", "Accepted button callbacks", order=6),
    )


class DispatchSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "投递节奏"
    __ui_order__: ClassVar[int] = 4
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = _section_i18n("Delivery timing")

    edit_debounce: float = Field(
        default=3.0,
        ge=0,
        le=120,
        description="消息被编辑后，等待多少秒没有再次编辑才把修改告知 MaiBot（连续快速编辑只告知最后一次）",
        json_schema_extra=_ui("编辑合并等待（秒）", "Edit debounce (s)", order=0),
    )
    edit_notice: bool = Field(
        default=True,
        description="已交给 MaiBot 的消息被编辑时，以通知形式告知（可能引发回复）。尚未交出的消息被编辑时直接替换内容",
        json_schema_extra=_ui("告知消息编辑", "Report edits", order=1),
    )
    deletion_notice: bool = Field(
        default=True,
        description="已交给 MaiBot 的消息被删除时，以通知形式告知。尚未交出的消息被删除时直接丢弃",
        json_schema_extra=_ui("告知消息删除", "Report deletions", order=2),
    )
    silence_window: float = Field(
        default=0.0,
        ge=0,
        le=300,
        description="静默窗口：聊天中连续多少秒没有新消息、编辑或删除后才投递。0 为关闭（MaiBot 自身已有约 1 秒的等待）",
        json_schema_extra=_ui("静默窗口（秒）", "Silence window (s)", order=3),
    )
    typing_hold: bool = Field(
        default=True,
        description="有人正在输入时暂缓投递，等对方输入结束（仅用户账号能收到输入状态）",
        json_schema_extra=_ui("等待对方输入完成", "Wait while others type", order=4),
    )
    typing_max_hold: float = Field(
        default=20.0,
        ge=0,
        le=300,
        description="因对方正在输入而暂缓投递的最长时间（秒）",
        json_schema_extra=_ui("输入等待上限（秒）", "Typing hold limit (s)", order=5,
                              depends_on="typing_hold", depends_value=True),
    )
    lazy_mode: Literal["off", "count", "interval", "adaptive"] = Field(
        default="off",
        description=(
            "攒批投递：off 立即投递；count 攒够 N 条再投递；interval 每隔固定秒数投递一次；"
            "adaptive 按群聊活跃度自动调整批量（越热闹攒得越多）。MaiBot 自身会对积压消息评分后再决定是否回复"
        ),
        json_schema_extra=_ui("攒批投递", "Lazy push", order=6),
    )
    lazy_count: int = Field(
        default=3,
        ge=1,
        le=100,
        description="count 模式的条数；adaptive 模式在还不了解群聊节奏时的初始条数",
        json_schema_extra=_ui("攒批条数", "Batch size", order=7),
    )
    lazy_interval: float = Field(
        default=10.0,
        ge=1,
        le=600,
        description="interval 模式的投递间隔（秒）",
        json_schema_extra=_ui("投递间隔（秒）", "Push interval (s)", order=8,
                              depends_on="lazy_mode", depends_value="interval"),
    )
    lazy_max_count: int = Field(
        default=8,
        ge=1,
        le=100,
        description="adaptive 模式的批量上限",
        json_schema_extra=_ui("自适应批量上限", "Adaptive batch cap", order=9,
                              depends_on="lazy_mode", depends_value="adaptive"),
    )
    max_wait: float = Field(
        default=60.0,
        ge=1,
        le=3600,
        description="任何消息在适配器中最多等待多少秒，到时无论如何都会投递",
        json_schema_extra=_ui("最长等待（秒）", "Max wait (s)", order=10),
    )
    urgent_bypass: bool = Field(
        default=True,
        description="@ 我、回复我的消息以及私聊消息跳过静默窗口与攒批，尽快投递（仍会等待对方输入完成）",
        json_schema_extra=_ui("提及与私聊优先", "Mentions and DMs skip batching", order=11),
    )
    history_poll: Literal["auto", "on", "off"] = Field(
        default="auto",
        description=(
            "定期重读最近消息，发现编辑与删除。auto 仅 Bot 账号开启（Bot 收不到删除通知）。"
            "轮询发现的变化只更新 MaiBot 的上下文，不会触发回复"
        ),
        json_schema_extra=_ui("历史轮询", "History polling", order=12),
    )
    poll_interval: float = Field(
        default=60.0,
        ge=10,
        le=3600,
        description="历史轮询间隔（秒）。每条消息在发出后约 1、3、7、15、31 个间隔时各重读一次，间隔逐次翻倍",
        json_schema_extra=_ui(
            "轮询间隔（秒）", "Poll interval (s)",
            "消息未变化时重读间隔逐次翻倍，一小时内每条约重读 5 次",
            "Gaps between re-reads double, so each message is read about 5 times in its hour",
            order=13,
        ),
    )
    poll_count: int = Field(
        default=20,
        ge=1,
        le=100,
        description="每个聊天每次轮询重读的最近消息条数（仅最近一小时内交给 MaiBot 的消息）",
        json_schema_extra=_ui("轮询条数", "Messages per poll", order=14),
    )


class OutboundSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "发送"
    __ui_order__: ClassVar[int] = 5
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
    telegraph_author_name: str = Field(
        default="",
        description="发布到 Telegra.ph 的文章署名，留空使用本账号名称",
        json_schema_extra=_ui("Telegraph 署名", "Telegraph author name", order=6),
    )
    telegraph_author_url: str = Field(
        default="",
        description="文章署名的链接，留空时使用本账号的 t.me 链接（若有用户名）",
        json_schema_extra=_ui("Telegraph 署名链接", "Telegraph author link", order=7),
    )
    long_text_notice: str = Field(
        default="消息过长，点击查看：",
        description="发布长文后在聊天中发送的提示语，后面紧跟 Telegraph 链接（链接预览会显示标题与开头）",
        json_schema_extra=_ui("长文提示语", "Long text notice", order=8),
    )
    long_text_ai_summary: bool = Field(
        default=True,
        description=(
            "（仅用户账号）发布长文时，用 Telegram 的 AI 摘要（Cocoon）代替提示语作为消息正文：把正文开头发到本账号的"
            "收藏夹（Saved Messages），读取一次摘要后立即删除。非会员额度很少，用尽或失败时自动改用提示语"
        ),
        json_schema_extra=_ui(
            "长文使用 AI 摘要", "AI summary for long texts",
            "（Bot 无法使用）始终发送提示语", "(Not available to bots) Always send the notice",
            order=9,
        ),
    )
    typing_indicator: bool = Field(
        default=True,
        description="MaiBot 开始生成回复时，在 Telegram 显示一次“正在输入…”（不会反复刷新）",
        json_schema_extra=_ui("显示正在输入", "Show typing status", order=5),
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


USER_AGENT_PRESETS = ("googlebot", "browser", "curl", "default", "none")


class MediaSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "媒体"
    __ui_order__: ClassVar[int] = 6
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = _section_i18n("Media")

    recognition_cache: bool = Field(
        default=True,
        description="同一张图片或贴纸再次出现时，直接复用 MaiBot 已有的识别结果，不再下载和识别",
        json_schema_extra=_ui(
            "识别结果复用", "Reuse recognition results",
            "按 Telegram 文件判断是否相同；命中时既不下载也不消耗识图额度",
            "Matched by Telegram file; a hit skips both the download and image recognition",
            order=0,
        ),
    )
    native_resend: bool = Field(
        default=True,
        description="MaiBot 发出的图片或表情若来自 Telegram（例如收到过的贴纸），直接引用原文件发送，保留原生贴纸/动图效果且无需重新上传",
        json_schema_extra=_ui("原生转发已知媒体", "Resend known media natively", order=1),
    )
    sticker_emoji_hint: bool = Field(
        default=True,
        description="在贴纸前附上它代表的 emoji，例如 [贴纸 😂]，帮助 MaiBot 理解贴纸含义",
        json_schema_extra=_ui("贴纸 emoji 提示", "Sticker emoji hint", order=2),
    )
    animation: Literal["gif", "thumbnail", "drop"] = Field(
        default="gif",
        description="GIF 动图与视频贴纸：gif 转成真正的 GIF，MaiBot 能看到多帧；thumbnail 只用 Telegram 提供的静态缩略图；drop 只保留 [动图] 标记",
        json_schema_extra=_ui(
            "动图处理", "Animations",
            "Telegram 的 GIF 实际是 MP4 视频，gif 模式用 PyAV 转换，转换失败时退回缩略图",
            "Telegram GIFs are MP4 videos. gif mode converts them with PyAV and falls back to the thumbnail on failure",
            order=3,
        ),
    )
    video_thumbnail: bool = Field(
        default=False,
        description="为视频与视频消息附上缩略图供 MaiBot 识别（会消耗识图额度）",
        json_schema_extra=_ui("视频缩略图", "Video thumbnails", order=5),
    )
    link_preview: Literal["off", "telegram", "fetch"] = Field(
        default="fetch",
        description="链接信息：telegram 只用 Telegram 自带的网页预览；fetch 在没有预览时由适配器自行读取网页标题与简介；off 关闭",
        json_schema_extra=_ui("链接信息", "Link information", order=6),
    )
    link_user_agents: list[str] = Field(
        default_factory=lambda: ["googlebot", "browser", "curl", "default"],
        description="读取网页时依次尝试的 User-Agent。预设：googlebot / browser / curl / default（HTTP 库默认）/ none（不发送）；也可填写完整的 UA 字符串",
        json_schema_extra=_ui("User-Agent 顺序", "User-Agent order", order=7,
                              depends_on="link_preview", depends_value="fetch"),
    )
    link_timeout: float = Field(
        default=4.0,
        ge=1.0,
        le=30.0,
        description="读取网页时每次请求的超时时间（秒）",
        json_schema_extra=_ui("网页请求超时（秒）", "Page request timeout (s)", order=8,
                              depends_on="link_preview", depends_value="fetch"),
    )
    link_allow_private: bool = Field(
        default=False,
        description=(
            "允许读取解析到本机、局域网、链路本地等非公网地址的链接。关闭时这类链接不读取，"
            "避免群成员借适配器探测内网与云服务器元数据"
        ),
        json_schema_extra=_ui(
            "允许读取内网地址", "Allow private addresses",
            "fake-ip 代理（Clash / mihomo / sing-box）分配的 198.18.0.0/15 地址始终允许，无需为此开启",
            "Fake-ip proxy addresses (Clash / mihomo / sing-box, 198.18.0.0/15) are always allowed; "
            "no need to turn this on for them",
            order=9, depends_on="link_preview", depends_value="fetch",
        ),
    )


class AdvancedSection(PluginConfigBase):
    __ui_label__: ClassVar[str] = "高级"
    __ui_order__: ClassVar[int] = 7
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = _section_i18n("Advanced")

    raw_api_enabled: bool = Field(
        default=False,
        description=(
            "允许 MaiBot（工具 telegram_raw_api）与其他插件（API raw_invoke）直接调用任意 Telegram MTProto 方法。"
            "风险自负：错误的调用可能删除消息、修改账号或导致账号受限"
        ),
        json_schema_extra=_ui(
            "开放原始 MTProto 调用", "Allow raw MTProto calls",
            "默认关闭。开启后工具才会出现在 MaiBot 的工具列表中；下方名单用于限制可调用的方法",
            "Off by default. The tool only appears in MaiBot's tool list while this is on; the lists below "
            "limit which methods can be called",
            order=0,
        ),
    )
    raw_api_allow: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RAW_ALLOW),
        description=(
            "允许调用的方法（通配符，按 TL 名称匹配，例如 messages.* 或 users.getFullUser）。"
            "默认只允许读取类方法（get / search / check / resolve）"
        ),
        json_schema_extra=_ui(
            "允许的方法", "Allowed methods",
            "需要写操作时在此添加，例如 messages.sendMessage；填 * 允许全部（仍受禁止名单限制）",
            "Add write methods here when needed, e.g. messages.sendMessage; * allows everything not denied below",
            order=1, depends_on="raw_api_enabled", depends_value=True,
        ),
    )
    raw_api_deny: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RAW_DENY),
        description="禁止调用的方法（优先于允许名单）。默认禁止登录/账号/支付/通话相关方法以及删除、退群、举报、拉黑、修改权限等操作",
        json_schema_extra=_ui("禁止的方法", "Denied methods", order=2,
                              depends_on="raw_api_enabled", depends_value=True),
    )
    raw_api_max_result_chars: int = Field(
        default=8000,
        ge=500,
        le=100000,
        description="返回结果（JSON）的最大字符数，超出部分截断",
        json_schema_extra=_ui("结果长度上限", "Result size limit", order=3,
                              depends_on="raw_api_enabled", depends_value=True),
    )


# Methods raw MTProto calls may use by default: read-only ones.
DEFAULT_RAW_ALLOW = ("*.get*", "*.search*", "*.check*", "contacts.resolve*")

# Methods raw MTProto calls may not use unless the user edits advanced.raw_api_deny.
DEFAULT_RAW_DENY = (
    "auth.*", "account.*", "payments.*", "phone.*", "stickers.*",
    "*.delete*", "*.leave*", "*.report*", "*.block*", "*.editAdmin", "*.editBanned", "*.editCreator",
    "channels.togglePreHistoryHidden", "messages.deleteChat", "contacts.resetSaved",
    "messages.getBotCallbackAnswer",  # presses an inline button
)


class TelegramFullConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    account: AccountSection = Field(default_factory=AccountSection)
    connection: ConnectionSection = Field(default_factory=ConnectionSection)
    inbound: InboundSection = Field(default_factory=InboundSection)
    dispatch: DispatchSection = Field(default_factory=DispatchSection)
    outbound: OutboundSection = Field(default_factory=OutboundSection)
    media: MediaSection = Field(default_factory=MediaSection)
    advanced: AdvancedSection = Field(default_factory=AdvancedSection)

    def connection_fingerprint(self) -> tuple[Any, ...]:
        """Fields whose change requires reconnecting the Telegram client."""
        a, c = self.account, self.connection
        return (
            self.plugin.enabled, a.type, a.api_id, a.api_hash, a.bot_token, a.phone, a.password,
            a.session_name, c.proxy, c.flood_sleep_threshold, c.catch_up,
        )
