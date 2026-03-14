import asyncio
import re
from contextlib import suppress

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


class DiscordChannelResolver:
    """Resolve Discord channel object from AstrBot event payloads."""

    ORIGIN_PATTERNS = (
        r"(?:^|[:|,/_-])channel(?:_id)?[:=](\d{8,22})(?:$|[:|,/_-])",
        r"(?:^|[:|,/_-])chan(?:nel)?[:=](\d{8,22})(?:$|[:|,/_-])",
    )

    def __init__(self, debug_log: bool = False):
        self.debug_log = debug_log

    def is_discord_event(self, event: AstrMessageEvent) -> bool:
        origin = str(getattr(event, "unified_msg_origin", "") or "").lower()
        return origin.startswith("discord:") or ":discord:" in origin

    def extract_channel_id(self, event: AstrMessageEvent) -> int | None:
        direct_candidates = [
            getattr(event, "channel_id", None),
            getattr(event, "channelId", None),
        ]

        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            direct_candidates.extend(
                [
                    getattr(msg_obj, "channel_id", None),
                    getattr(msg_obj, "channelId", None),
                ]
            )

            raw_message = getattr(msg_obj, "raw_message", None)
            if raw_message is not None:
                direct_candidates.extend(
                    [
                        getattr(raw_message, "channel_id", None),
                        getattr(raw_message, "channelId", None),
                        getattr(raw_message, "channel", None),
                    ]
                )

            message = getattr(msg_obj, "message", None)
            if message is not None:
                direct_candidates.extend(
                    [
                        getattr(message, "channel_id", None),
                        getattr(message, "channelId", None),
                        getattr(message, "channel", None),
                    ]
                )

        for value in direct_candidates:
            channel_id = self._parse_discord_id(value)
            if channel_id is not None:
                return channel_id

        origin = str(getattr(event, "unified_msg_origin", "") or "")
        return self._extract_channel_id_from_origin(origin)

    def _extract_channel_id_from_origin(self, origin: str) -> int | None:
        if not origin:
            return None

        for pattern in self.ORIGIN_PATTERNS:
            match = re.search(pattern, origin, re.IGNORECASE)
            if match:
                return int(match.group(1))

        return None

    def _parse_discord_id(self, raw_value) -> int | None:
        if raw_value is None:
            return None

        if hasattr(raw_value, "id"):
            raw_value = getattr(raw_value, "id", None)

        value = str(raw_value).strip()
        if not value:
            return None

        if value.isdigit():
            return int(value)

        matches = re.findall(r"\d+", value)
        if not matches:
            return None

        return int(matches[-1])


class DiscordTypingInternal:
    """Discord API access wrapper, inspired by adapter internal layers."""

    def __init__(
        self, plugin: "StopRushingIamTypingPlugin", resolver: DiscordChannelResolver
    ):
        self.plugin = plugin
        self.resolver = resolver

    async def resolve_channel(self, event: AstrMessageEvent):
        if not self.resolver.is_discord_event(event):
            return None

        client = self._ensure_client()
        if client is None:
            if self.plugin.debug_log:
                logger.debug("[StopRushingTyping] Discord client not found.")
            return None

        channel_id = self.resolver.extract_channel_id(event)
        if channel_id is None:
            if self.plugin.debug_log:
                logger.debug(
                    "[StopRushingTyping] Cannot resolve channel id from event."
                )
            return None

        channel = None
        try:
            channel = client.get_channel(channel_id)
        except Exception:
            channel = None

        if channel is None and hasattr(client, "fetch_channel"):
            try:
                channel = await client.fetch_channel(channel_id)
            except Exception:
                channel = None

        return channel

    async def trigger_typing(self, channel) -> bool:
        trigger = getattr(channel, "trigger_typing", None)
        if callable(trigger):
            maybe_coro = trigger()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
            return True

        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return False

        client = self._ensure_client()
        http = getattr(client, "http", None) if client else None
        if http is None:
            return False

        for method_name in (
            "send_typing",
            "trigger_typing_indicator",
            "trigger_typing",
        ):
            method = getattr(http, method_name, None)
            if not callable(method):
                continue

            maybe_coro = method(channel_id)
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
            return True

        return False

    def _ensure_client(self):
        if self.plugin.discord_client is None:
            self.plugin.discord_client = self.plugin._get_astrbot_discord_client()
        return self.plugin.discord_client


class TypingSessionController:
    """Track per-session keepalive tasks and close them safely."""

    def __init__(
        self,
        typing_internal: DiscordTypingInternal,
        keepalive_seconds: float,
        max_window_seconds: float,
        debug_log: bool = False,
    ):
        self.typing_internal = typing_internal
        self.keepalive_seconds = keepalive_seconds
        self.max_window_seconds = max_window_seconds
        self.debug_log = debug_log

        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._typing_started_at: dict[str, float] = {}
        self._session_generation: dict[str, int] = {}

    async def start(self, session_key: str, channel):
        if not session_key:
            return

        loop = asyncio.get_running_loop()
        self._typing_started_at[session_key] = loop.time()

        existing = self._typing_tasks.get(session_key)
        if existing is not None and not existing.done():
            return

        generation = self._session_generation.get(session_key, 0) + 1
        self._session_generation[session_key] = generation

        task = asyncio.create_task(
            self._typing_keepalive(session_key, generation, channel)
        )
        self._typing_tasks[session_key] = task

    async def stop(self, session_key: str):
        if not session_key:
            return

        self._session_generation[session_key] = (
            self._session_generation.get(session_key, 0) + 1
        )
        task = self._typing_tasks.pop(session_key, None)
        self._typing_started_at.pop(session_key, None)
        if task is None:
            return

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def stop_all(self):
        for key, task in list(self._typing_tasks.items()):
            self._session_generation[key] = self._session_generation.get(key, 0) + 1
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._typing_tasks.pop(key, None)
            self._typing_started_at.pop(key, None)
            self._session_generation.pop(key, None)

    async def _typing_keepalive(self, session_key: str, generation: int, channel):
        loop = asyncio.get_running_loop()
        try:
            while True:
                if self._session_generation.get(session_key, 0) != generation:
                    return

                started = self._typing_started_at.get(session_key)
                if started is None:
                    return

                if loop.time() - started >= self.max_window_seconds:
                    if self.debug_log:
                        logger.debug(
                            "[StopRushingTyping] typing window timeout for %s",
                            session_key,
                        )
                    return

                ok = await self.typing_internal.trigger_typing(channel)
                if not ok:
                    if self.debug_log:
                        logger.debug(
                            "[StopRushingTyping] no supported trigger_typing method for %s",
                            session_key,
                        )
                    return

                await asyncio.sleep(self.keepalive_seconds)
        except Exception as e:
            if self.debug_log:
                logger.debug("[StopRushingTyping] trigger_typing failed: %s", e)
        finally:
            current = self._typing_tasks.get(session_key)
            this_task = asyncio.current_task()
            if current is this_task:
                self._typing_tasks.pop(session_key, None)
                self._typing_started_at.pop(session_key, None)
                if self._session_generation.get(session_key, 0) == generation:
                    self._session_generation.pop(session_key, None)


@register(
    name="astrbot_plugin_stoprushingiamtyping",
    author="AstrBot Community",
    desc="重建版 Discord typing 指示插件，以分層架構在 LLM 思考中持續顯示輸入中狀態。",
    version="0.2.0",
)
class StopRushingIamTypingPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.enable = bool(self.config.get("enable", True))
        self.typing_keepalive_seconds = max(
            3.0,
            float(self.config.get("typing_keepalive_seconds", 8.0)),
        )
        self.max_typing_window_seconds = max(
            10.0,
            float(self.config.get("max_typing_window_seconds", 120.0)),
        )
        self.debug_log = bool(self.config.get("debug_log", False))

        self.discord_client = None
        self.resolver = DiscordChannelResolver(debug_log=self.debug_log)
        self.internal = DiscordTypingInternal(plugin=self, resolver=self.resolver)
        self.controller = TypingSessionController(
            typing_internal=self.internal,
            keepalive_seconds=self.typing_keepalive_seconds,
            max_window_seconds=self.max_typing_window_seconds,
            debug_log=self.debug_log,
        )

    async def initialize(self):
        self.discord_client = self._get_astrbot_discord_client()
        if self.debug_log:
            logger.info(
                "[StopRushingTyping] initialize done. discord_client=%s",
                "ok" if self.discord_client is not None else "missing",
            )

    async def terminate(self):
        await self.controller.stop_all()

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req=None):
        if not self.enable:
            return

        channel = await self.internal.resolve_channel(event)
        if channel is None:
            return

        session_key = self._get_session_key(event)
        await self.controller.start(session_key, channel)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        session_key = self._get_session_key(event)
        await self.controller.stop(session_key)

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _get_astrbot_discord_client(self):
        try:
            manager_candidates = [
                getattr(self.context, "platform_manager", None),
                getattr(self.context, "_platform_manager", None),
                getattr(self.context, "platform_mgr", None),
                getattr(self.context, "_platform_mgr", None),
            ]

            for pm in manager_candidates:
                if not pm or not hasattr(pm, "platform_insts"):
                    continue

                for inst in pm.platform_insts:
                    client = getattr(inst, "client", None)
                    if client is None and hasattr(inst, "get_client"):
                        try:
                            client = inst.get_client()
                        except Exception:
                            client = None

                    if not hasattr(inst, "meta"):
                        continue

                    try:
                        meta = inst.meta()
                    except Exception:
                        continue

                    if getattr(meta, "name", "") == "discord" and client is not None:
                        return client

            if hasattr(self.context, "get_platform"):
                try:
                    inst = self.context.get_platform("discord")
                    client = getattr(inst, "client", None)
                    if client is None and hasattr(inst, "get_client"):
                        client = inst.get_client()
                    if client is not None:
                        return client
                except Exception:
                    pass

            adapter = getattr(self.context, "platform_adapter", None)
            if adapter is not None and hasattr(adapter, "client"):
                return adapter.client
        except Exception as e:
            logger.warning("[StopRushingTyping] Failed to get Discord client: %s", e)

        return None
