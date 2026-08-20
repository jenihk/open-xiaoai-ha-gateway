"""Wakeup session manager.

Dispatches wakeup events (KWS keyword or XiaoAI native ASR) to the Home
Assistant continuous conversation controller.
"""

import asyncio

from core.ref import (
    get_app,
    get_kws,
    get_speaker,
)
from core.ha import HAManager
from core.utils.config import ConfigManager
from core.utils.logger import logger


class WakeupSessionManager:
    """Dispatches wakeup events to the HA conversation controller."""

    def __init__(self):
        self.config = ConfigManager.instance()
        self._ha_controller = None
        self._ha_task: asyncio.Task | None = None

    def _get_loop(self):
        app = get_app()
        if app:
            return app.loop
        from core.xiaoai import XiaoAI
        return XiaoAI.async_loop

    async def _stop_device_playback(self):
        """Stop all audio playback on the device and restart recording."""
        speaker = get_speaker()
        if speaker:
            await speaker.stop_device_audio()
            extra = self.config.get_app_config("wakeup.extra_stop_command", "") or ""
            if extra:
                try:
                    await speaker.run_shell(extra)
                    logger.info(f"[Wakeup] Extra stop command executed: {extra[:80]}")
                except Exception as exc:
                    logger.debug(
                        f"[Wakeup] Extra stop command failed: {type(exc).__name__}: {exc}"
                    )
            import open_xiaoai_server
            await open_xiaoai_server.start_recording()
            return

        import open_xiaoai_server
        await open_xiaoai_server.stop_playing()
        extra = self.config.get_app_config("wakeup.extra_stop_command", "") or ""
        if extra:
            try:
                speaker = get_speaker()
                if speaker:
                    await speaker.run_shell(extra)
                    logger.info(
                        f"[Wakeup] Extra stop command executed: {extra[:80]}"
                    )
            except Exception as exc:
                logger.debug(
                    f"[Wakeup] Extra stop command failed: {type(exc).__name__}: {exc}"
                )
        await open_xiaoai_server.start_recording()

    def on_interrupt(self):
        """User said the XiaoAI wake word: interrupt active sessions."""
        logger.info("[Wakeup] XiaoAI wakeup - interrupting active sessions")

        loop = self._get_loop()

        # Stop the HA continuous conversation (cancels VAD + stops TTS stream)
        if self._ha_controller and self._ha_controller.is_active():
            self._ha_controller.stop()
        if self._ha_task and not self._ha_task.done():
            loop.call_soon_threadsafe(self._ha_task.cancel)

        asyncio.run_coroutine_threadsafe(self._stop_device_playback(), loop)

        from core.xiaoai import XiaoAI
        XiaoAI.stop_conversation()

    def on_speech(self, speech_buffer: bytes):
        """Called by VAD when speech is detected."""
        pass

    def on_silence(self):
        """Called by VAD when silence is detected."""
        pass

    def consume_xiaoai_asr_result(
        self,
        dialog_id: str,
        text: str,
        is_final,
        is_vad_begin,
    ) -> bool:
        """Route XiaoAI native ASR results to the active HA controller."""
        if self._ha_controller and self._ha_controller.is_active():
            return self._ha_controller.consume_xiaoai_recognize_result(
                dialog_id=dialog_id,
                text=text,
                is_final=is_final,
                is_vad_begin=is_vad_begin,
            )
        return False

    async def wakeup(self, text, source):
        before_wakeup = self.config.get_app_config("wakeup.before_wakeup")
        kws = get_kws()
        logger.debug(f"[Wakeup] Received wakeup request from {source}: {text}")

        # Reset the routing key and start a fresh HA conversation on each wakeup,
        # so paths that don't call set_ha_agent_id() always use the default agent.
        HAManager.reset_session()

        if kws:
            kws.pause()
        should_wakeup = await before_wakeup(
            get_speaker(),
            text,
            source,
            get_app(),
        )
        if kws:
            kws.resume()
        logger.info(f"[Wakeup] before_wakeup returned: {should_wakeup}")
        if should_wakeup is not None:
            await self.reset_all_sessions()

        if should_wakeup == "ha":
            await self._start_ha_conversation()

    async def _start_ha_conversation(self):
        """Start an HA continuous conversation session.

        Runs independently: VAD -> ASR -> HA conversation/process -> TTS.
        KWS is paused during the conversation and resumed when done.
        """
        from core.ha_conversation import HAAssistConversationController

        kws = get_kws()
        if kws:
            kws.pause()
        try:
            self._ha_controller = HAAssistConversationController()
            self._ha_task = asyncio.create_task(self._ha_controller.start())
            await self._ha_task
        except asyncio.CancelledError:
            pass  # interrupted cleanly by on_interrupt
        except Exception as exc:
            logger.error(
                f"[Wakeup] HA conversation failed: {type(exc).__name__}: {exc}",
                module="Wakeup",
            )
        finally:
            self._ha_controller = None
            self._ha_task = None
            if kws:
                kws.resume()

    async def reset_all_sessions(self):
        """Reset all active sessions before starting a new one."""
        from core.xiaoai import XiaoAI

        # Stop XiaoAI continuous conversation
        XiaoAI.stop_conversation()

        # Stop the HA continuous conversation (also stops its TTS stream)
        if self._ha_controller and self._ha_controller.is_active():
            self._ha_controller.stop()

        # Stop all audio playback on the device
        await self._stop_device_playback()

        logger.debug("[Wakeup] All sessions reset")


EventManager = WakeupSessionManager()
