"""MaiBot entry point for the Telethon based Telegram adapter."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, MaiBotPlugin, MessageGateway, PluginConfigBase

from .tg_full.config import TelegramFullConfig
from .tg_full.constants import GATEWAY_NAME, PLATFORM, PROTOCOL
from .tg_full.runtime import AdapterRuntime


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
