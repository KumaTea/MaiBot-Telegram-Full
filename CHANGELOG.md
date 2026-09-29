# 更新日志 / Changelog

## 0.1.0 — 2026-09-30

首个版本。First release.

### 主要功能

- 基于 Telethon (MTProto) 的双工消息网关，插件类型为 `adapter`；支持 Bot 账号与用户账号（userbot）。
- 登录：Bot Token；用户账号支持手机号验证码（在 WebUI 填写）、两步验证密码、现有 Telethon 会话文件。
- 断线自动重连（Telethon 快速重连 + 适配器指数退避）；网关状态随连接上报。
- 消息：Telegram 格式与 markdown 双向转换；LaTeX 公式转纯文本；回复、@、转发来源、频道 / 匿名管理员身份、论坛话题。
- 媒体：图片、贴纸（附 emoji 提示）、语音、文件；同一文件复用 MaiBot 已有识别结果、不重复下载；MaiBot 发送的已知贴纸 / 动图按原文件转发；GIF 可转为真正的 GIF（可选 PyAV）；链接预览与网页标题抓取（多 User-Agent 回退）。
- 投递节奏：编辑合并通知、删除通知、等待对方输入完成、静默窗口、攒批（按条数 / 间隔 / 自适应）、Bot 账号历史轮询（仅更新上下文）、命令过滤（丢弃或随下一条消息一起投递）。
- 互动：回复前显示一次“正在输入”；接收与发送表情回应（含长按大表情）；内联按钮与回调登记。
- 工具与 API：贴纸查找与发送、读取消息、聊天信息、Telegraph 长文发布 / 修改 / 清空（用户账号可用 Telegram AI 摘要）、原始 MTProto 调用（默认关闭，有允许 / 禁止名单）。

### 细节

- 消息 ID 为 `chat_id:msg_id`，群号与话题格式兼容 exynos967/MaiBot-Telegram-Adapter。
- 依赖由 MaiBot 自动安装：`telethon>=1.45,<2`（Telegram API layer 229 的内联按钮结构）、`cryptg`、`markdown-it-py`、`python-socks`、`Pillow`、`aiohttp`。
- 能力声明：`database.get`、`chat.get_all_streams`、`maisaka.context.append`、`component.enable`、`component.disable`。
