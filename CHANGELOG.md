# 更新日志 / Changelog

## 0.1.1 — 2026-10-02

根据插件仓库审核意见修改。Changes from the plugin-repo review.

- 链接预览：不再读取解析到本机、局域网、链路本地（含云服务器元数据 169.254.169.254）等非公网地址的链接，每次重定向都会重新检查；fake-ip 代理的 198.18.0.0/15 地址不受影响。新增 `media.link_allow_private`（默认关闭）可放开限制。
- PyAV（`av`）改为必需依赖并在 manifest 中声明，移除运行时自动安装及 `media.install_pyav`；`media.animation` 默认改为 `gif`。
- 原始 MTProto 调用：允许名单默认只含读取类方法（`*.get*`、`*.search*`、`*.check*`、`contacts.resolve*`），空名单不再等于全部允许；`messages.getBotCallbackAnswer`（会按下按钮）加入默认禁止名单。
- Telegraph：渲染时去掉原始 HTML 中的 iframe / video，链接只保留 http(s) / mailto / tg；只能修改或清空本适配器记录过的页面，不再用当前账号去尝试未记录的页面。
- 配置版本升至 0.1.1：MaiBot 会按新结构重建配置，保留已有的值。

## 0.1.0 — 2026-09-30

首个版本。First release.

### 主要功能

- 基于 Telethon (MTProto) 的双工消息网关，插件类型为 `adapter`；支持 Bot 账号与用户账号（userbot）。
- 登录：Bot Token；用户账号支持手机号验证码（在 WebUI 填写）、两步验证密码、现有 Telethon 会话文件。
- 断线自动重连（Telethon 快速重连 + 适配器指数退避）；网关状态随连接上报。
- 补收离线消息：重连（包括 Telethon 内部的自动重连）或重启后从会话文件中保存的进度继续接收；重复收到的消息会被跳过；发出超过 `catch_up_max_age` 分钟的旧消息不单独触发回复。
- 消息：Telegram 格式与 markdown 双向转换；LaTeX 公式转纯文本；回复、@、转发来源、频道 / 匿名管理员身份、论坛话题。
- 媒体：图片、贴纸（附 emoji 提示）、语音、文件；同一文件复用 MaiBot 已有识别结果、不重复下载；MaiBot 发送的已知贴纸 / 动图按原文件转发；GIF 可转为真正的 GIF（可选 PyAV）；链接预览与网页标题抓取（多 User-Agent 回退）。
- 投递节奏：编辑合并通知、删除通知、等待对方输入完成、静默窗口、攒批（按条数 / 间隔 / 自适应）、Bot 账号历史轮询（仅更新上下文，每条消息的重读间隔逐次翻倍）、命令过滤（丢弃或随下一条消息一起投递）。
- 互动：回复前显示一次“正在输入”；接收与发送表情回应（含长按大表情）；内联按钮与回调登记。
- 工具与 API：贴纸查找与发送、读取消息、聊天信息、Telegraph 长文发布 / 修改 / 清空（用户账号可用 Telegram AI 摘要）、原始 MTProto 调用（默认关闭，有允许 / 禁止名单）。

### 细节

- 消息 ID 为 `chat_id:msg_id`，群号与话题格式兼容 exynos967/MaiBot-Telegram-Adapter。
- 依赖由 MaiBot 自动安装：`telethon>=1.45,<2`（Telegram API layer 229 的内联按钮结构）、`cryptg`、`markdown-it-py`、`python-socks`、`Pillow`、`aiohttp`。
- 插件图标：`assets/icon.svg`（改自 m8rge 的 Telegram 图标，MIT）。
- 能力声明：`database.get`、`chat.get_all_streams`、`maisaka.context.append`、`component.enable`、`component.disable`。
