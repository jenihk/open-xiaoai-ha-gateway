"""Main application controller.

This module manages the main application flow, coordinating between:
- XiaoAI (Xiaomi speaker device bridge)
- Home Assistant Assist (conversation/process)
- Audio system (VAD, KWS, ASR)
- HTTP API Server (optional)
"""

import asyncio
import os
import threading
import time

from core.xiaoai import XiaoAI
from core.ha import HAManager
from core.ref import set_app
from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.services.protocols.typing import EventType
from core.services.api_server import APIServer


class MainApp:
    """Main application controller."""

    _instance = None

    @classmethod
    def instance(cls, enable_ha: bool = True):
        """Get singleton instance.

        Args:
            enable_ha: Whether to enable the Home Assistant integration.
        """
        if cls._instance is None:
            cls._instance = MainApp(enable_ha=enable_ha)
        return cls._instance

    def __init__(self, enable_ha: bool = True):
        """Initialize the main application."""
        if MainApp._instance is not None:
            raise Exception("MainApp is singleton, use instance() to get instance")
        MainApp._instance = self

        # Config
        self.config = ConfigManager.instance()

        # Feature flags
        self._enable_ha = enable_ha

        # Chat state
        self.current_text = ""
        self.current_emotion = "neutral"

        # Event loop and threads
        self.loop = asyncio.new_event_loop()
        self.loop_thread = None
        self.config_watch_thread = None
        self.shutdown_requested = False
        self.running = False

        # Task queue
        self.main_tasks = []
        self.mutex = threading.Lock()

        # Events
        self.events = {
            EventType.SCHEDULE_EVENT: threading.Event(),
        }

        # API Server
        self.api_server = None
        self._enable_api_server = False

        set_app(self)

    def run(self, enable_api_server: bool = False):
        """Start the main application.

        Args:
            enable_api_server: Whether to start the HTTP API Server
        """
        self._enable_api_server = enable_api_server

        # KWS keyword detection needs the microphone; without audio input the
        # whole wake-word -> HA flow cannot work.
        audio_input_enabled = os.environ.get(
            "AUDIO_INPUT_ENABLE", "true"
        ).strip().lower() in ("true", "1", "yes", "on")
        if not audio_input_enabled and self._enable_ha:
            raise RuntimeError(
                "Audio input is disabled (AUDIO_INPUT_ENABLE=false) but the "
                "Home Assistant integration is enabled. Either enable audio "
                "input or disable HA."
            )

        # Create event loop thread
        self.loop_thread = threading.Thread(target=self._run_event_loop)
        self.loop_thread.daemon = True
        self.loop_thread.start()

        self._start_config_watcher()

        time.sleep(0.1)

        # Initialize XiaoAI service (registers Rust callbacks + WebSocket server)
        asyncio.run_coroutine_threadsafe(XiaoAI.init_xiaoai(), self.loop)

        # Initialize Home Assistant integration
        if self._enable_ha:
            HAManager.initialize_from_config()

        # Start API Server if enabled
        if self._enable_api_server:
            host = os.environ.get("API_SERVER_HOST", "127.0.0.1")
            port = int(os.environ.get("API_SERVER_PORT", 9092))
            self.api_server = APIServer(host=host, port=port)
            asyncio.run_coroutine_threadsafe(self.api_server.start(), self.loop)

        # Start main loop thread
        main_loop_thread = threading.Thread(target=self._main_loop)
        main_loop_thread.daemon = True
        main_loop_thread.start()

        # Start audio services (VAD/KWS) when audio input is enabled
        if self._enable_ha and audio_input_enabled:
            from core.services.audio.vad import VAD
            from core.services.audio.kws import KWS

            VAD.start()
            KWS.start()
            logger.info("[MainApp] Audio input enabled (VAD/KWS started)")
        else:
            logger.info("[MainApp] Audio input disabled (VAD/KWS not started)")

        # Pre-warm the local ASR model only when HA is configured with
        # input_mode="local_asr".
        if (
            self._enable_ha
            and audio_input_enabled
            and self.config.get_app_config("ha.input_mode", "local_asr")
            == "local_asr"
        ):
            from core.services.audio.asr import ASRService

            threading.Thread(
                target=ASRService.ensure_loaded,
                daemon=True,
                name="asr-warmup",
            ).start()

    def _run_event_loop(self):
        """Run asyncio event loop in separate thread."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _start_config_watcher(self):
        """Start config file watcher thread."""
        if self.config_watch_thread and self.config_watch_thread.is_alive():
            return

        self.config_watch_thread = threading.Thread(
            target=self._watch_config_file,
            daemon=True,
        )
        self.config_watch_thread.start()

    def _watch_config_file(self):
        """Poll config file for changes and hot-reload."""
        config_path = self.config.get_config_path()
        last_mtime = None

        while True:
            if self.shutdown_requested:
                break

            try:
                current_mtime = os.path.getmtime(config_path)
                if last_mtime is None:
                    last_mtime = current_mtime
                elif current_mtime != last_mtime:
                    last_mtime = current_mtime
                    self.config.reload_app_config()
                    logger.info(f"[Config] Reloaded runtime config from {config_path}")
            except Exception as exc:
                logger.warning(f"[Config] Failed to reload config: {exc}")

            time.sleep(1)

    def _main_loop(self):
        """Main application loop."""
        self.running = True

        while self.running:
            if self.events[EventType.SCHEDULE_EVENT].is_set():
                self.events[EventType.SCHEDULE_EVENT].clear()
                self._process_scheduled_tasks()
            time.sleep(0.01)

    def _process_scheduled_tasks(self):
        """Process scheduled tasks."""
        with self.mutex:
            tasks = self.main_tasks.copy()
            self.main_tasks.clear()

        for task in tasks:
            try:
                task()
            except Exception as exc:
                logger.error(
                    f"[MainApp] Scheduled task failed: {type(exc).__name__}: {exc}"
                )

    def schedule(self, callback):
        """Schedule task to main loop."""
        with self.mutex:
            self.main_tasks.append(callback)
        self.events[EventType.SCHEDULE_EVENT].set()

    # State management

    def set_chat_message(self, role, message):
        """Set chat message."""
        self.current_text = message

    def set_emotion(self, emotion):
        """Set emotion."""
        self.current_emotion = emotion

    def alert(self, title, message):
        """Show alert."""
        logger.warning(f"[Alert] {title}: {message}")

    # Shutdown

    def shutdown(self):
        """Shutdown the application."""
        self.shutdown_requested = True
        self.running = False

        if self.api_server:
            asyncio.run_coroutine_threadsafe(
                self.api_server.stop(), self.loop
            )

        # Close the HA aiohttp session
        if HAManager.is_enabled():
            asyncio.run_coroutine_threadsafe(
                HAManager.close(), self.loop
            )

        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=1.0)

        if self.config_watch_thread and self.config_watch_thread.is_alive():
            self.config_watch_thread.join(timeout=1.0)

    # Public API

    async def send_to_ha(
        self, text: str, wait_response: bool = True
    ) -> str | None:
        """Send text to the Home Assistant conversation agent.

        Returns the spoken reply on success, None on failure.
        """
        try:
            return await HAManager.send(text, wait_response=wait_response)
        except Exception as e:
            logger.error(
                f"[MainApp] 发送消息到 Home Assistant 失败: {type(e).__name__}: {e}"
            )
            return None

    async def send_to_ha_and_play_reply(
        self, text: str, wait_response: bool = True
    ) -> str | None:
        """Send text to HA and play the spoken reply through TTS."""
        try:
            return await HAManager.send_and_play_reply(
                text, wait_response=wait_response
            )
        except Exception as e:
            logger.error(
                f"[MainApp] 发送消息到 Home Assistant 失败: {type(e).__name__}: {e}"
            )
            return None

    def set_ha_agent_id(self, agent_id: str):
        """Route the next conversation to a different HA agent at runtime."""
        HAManager.set_agent_id(agent_id)
