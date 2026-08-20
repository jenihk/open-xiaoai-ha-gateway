"""Home Assistant continuous conversation controller.

After a custom wake word triggers wakeup, this module drives either:
  - local VAD -> ASR -> HA Assist -> TTS
  - XiaoAI native ASR -> HA Assist -> TTS

The selected input path runs independently of the XiaoAI session state.

Key design decisions:
  - Uses per-session asyncio.Future objects so it never conflicts with the
    KWS wakeup flow.
  - TTS playback is blocking (awaited), so the next listening round
    only starts after the response has finished playing.
"""

import asyncio
import os

import open_xiaoai_server

from core.ref import get_speaker, get_vad
from core.utils.config import ConfigManager
from core.ha import HAManager

_NOTIFY_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "sounds", "tts_notify.mp3",
)

_SEND_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "sounds", "send_notify.mp3",
)

def _load_notify_sound() -> bytes | None:
    """Decode tts_notify.mp3 to PCM at startup."""
    if not os.path.isfile(_NOTIFY_SOUND_PATH):
        return None
    try:
        with open(_NOTIFY_SOUND_PATH, "rb") as f:
            mp3_data = f.read()
        return open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
    except Exception:
        return None

def _load_send_sound() -> bytes | None:
    """Decode send_notify.mp3 to PCM at startup."""
    if not os.path.isfile(_SEND_SOUND_PATH):
        return None
    try:
        with open(_SEND_SOUND_PATH, "rb") as f:
            mp3_data = f.read()
        return open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
    except Exception:
        return None

_NOTIFY_PCM = _load_notify_sound()
_SEND_PCM = _load_send_sound()
from core.utils.logger import logger


class HAAssistConversationController:
    """Manages multi-turn conversation with a Home Assistant agent."""

    LOCAL_ASR_INPUT = "local_asr"
    XIAOAI_ASR_INPUT = "xiaoai_asr"
    XIAOAI_ASR_TIMEOUT = "__timeout__"
    CONFIG_PREFIX = "ha"
    BACKEND_NAME = "Home Assistant"
    LOG_MODULE = "HA Conv"
    WAKEUP_SOURCE = "ha"
    MANAGER = HAManager

    def __init__(self):
        self.config = ConfigManager.instance()
        if self.MANAGER is None:
            raise RuntimeError("MANAGER must be set by subclass")
        self.backend = self.MANAGER
        self.active = False

        # Per-session asyncio.Future used to receive VAD events
        self._vad_future: asyncio.Future | None = None
        # Per-session asyncio.Future used to receive XiaoAI native ASR results
        self._xiaoai_asr_future: asyncio.Future | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Playback token for the current TTS session
        self._playback_token: int | None = None

    # ---- config helpers ----

    def _cfg(self, key: str, default=None):
        return self.config.get_app_config(f"{self.CONFIG_PREFIX}.{key}", default)

    @property
    def exit_keywords(self) -> list[str]:
        return self._cfg("exit_keywords", ["退出", "停止", "再见"])

    @property
    def timeout(self) -> int:
        return int(self.config.get_app_config("wakeup.timeout", 20))

    @property
    def input_mode(self) -> str:
        mode = self._cfg("input_mode", self.LOCAL_ASR_INPUT)
        if not isinstance(mode, str):
            return self.LOCAL_ASR_INPUT
        normalized = mode.strip().lower()
        if normalized in {self.LOCAL_ASR_INPUT, self.XIAOAI_ASR_INPUT}:
            return normalized
        logger.warning(
            f"Unknown {self.CONFIG_PREFIX}.input_mode={mode!r}, fallback to {self.LOCAL_ASR_INPUT}",
            module=self.LOG_MODULE,
        )
        return self.LOCAL_ASR_INPUT

    def uses_xiaoai_asr(self) -> bool:
        return self.input_mode == self.XIAOAI_ASR_INPUT

    # ---- public API ----

    def is_active(self) -> bool:
        return self.active

    async def start(self):
        """Enter Home Assistant conversation mode."""
        if self.active:
            logger.warning(f"[{self.LOG_MODULE}] Already active, ignoring start()")
            return
        self.active = True
        self._loop = asyncio.get_running_loop()

        logger.info(f"🎙️ 进入 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)

        try:
            await self._conversation_loop()
        except Exception as exc:
            import traceback
            logger.error(
                f"Conversation loop error: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}",
                module=self.LOG_MODULE,
            )
        finally:
            self.stop()

    def stop(self):
        """Exit conversation mode and clean up."""
        if not self.active:
            return
        self.active = False
        self._cancel_vad_future()
        self._cancel_xiaoai_asr_future()
        if self._playback_token is not None:
            open_xiaoai_server.stop_tts_playback(self._playback_token)
            self._playback_token = None
        if self.uses_xiaoai_asr():
            speaker = get_speaker()
            if speaker and self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(
                        speaker.wake_up(awake=False),
                        self._loop,
                    )
                except Exception as exc:
                    logger.debug(
                        f"Failed to stop XiaoAI native listening: {exc}",
                        module=self.LOG_MODULE,
                    )
        logger.info(f"👋 退出 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)

    # ---- conversation loop ----

    async def _conversation_loop(self):
        """Run VAD -> ASR -> backend -> TTS turns until exit."""

        # Mute mic → play notify → unmute.
        # _play_notify() blocks for ~740ms (the beep duration), during which
        # the mic is off and before_wakeup TTS echo naturally fades.
        # VAD.resume() resets all state (speech_frames, input_bytes),
        # so speech detection starts clean when listening begins.
        await self._stop_recording()
        logger.debug("Recording stopped", module=self.LOG_MODULE)
        await self._play_notify()
        await self._start_recording()
        logger.debug("Ready to listen", module=self.LOG_MODULE)

        while self.active:
            if self.uses_xiaoai_asr():
                result = await self._run_one_turn_with_xiaoai_asr()
            else:
                result = await self._run_one_turn_with_local_asr()
            if result in ("exit", "timeout"):
                if self.uses_xiaoai_asr():
                    await self._stop_xiaoai_native_listening()
                await self._call_after_wakeup()
                break
            elif result == "error":
                break

    async def _run_one_turn_with_local_asr(self) -> str:
        """Execute a single conversation turn.

        Returns:
            "continue" - turn completed, loop to next
            "exit"     - user said an exit keyword
            "timeout"  - no speech detected within timeout
            "error"    - unrecoverable error
        """
        vad = get_vad()
        if not vad:
            logger.error("VAD not available", module=self.LOG_MODULE)
            return "error"

        # 1. Start listening for speech (recording is already active)
        speech_bytes = await self._wait_for_speech(vad)
        if speech_bytes is None:
            return "timeout"

        logger.debug(
            f"Got speech buffer: {len(speech_bytes)} bytes",
            module=self.LOG_MODULE,
        )

        # 2. ASR: convert speech to text
        from core.services.audio.asr import ASRService

        text = ASRService.asr(speech_bytes, sample_rate=16000)
        if not text:
            logger.debug("ASR empty, retrying", module=self.LOG_MODULE)
            return "continue"
        logger.user_speech(text, module=self.LOG_MODULE)

        # 3. Check exit keywords
        for kw in self.exit_keywords:
            if kw in text:
                logger.info(f"Exit keyword: {kw}", module=self.LOG_MODULE)
                return "exit"

        # 4. Send to Home Assistant and wait for the spoken reply.
        #    Play the "send" sound first so the user gets immediate feedback
        #    while HA (possibly via a cloud LLM) is still working.
        await self._play_send_sound()
        # rule_prompt is appended inside send_with_status().
        response, continue_conv = await self.backend.send_with_status(text)
        if response is None:
            # 注意：HA 可能已经执行了这条指令（如开灯成功但云端模型没
            # 返回文字），所以这里不自动重发，避免"开关类"指令被执行两次。
            logger.warning(
                f"No response from {self.BACKEND_NAME} "
                "(指令可能已执行，请确认设备状态后再让用户重说)",
                module=self.LOG_MODULE,
            )
            speaker = get_speaker()
            if speaker:
                await speaker.play(text="抱歉，我没收到回复，请再说一遍")
            return "continue"

        # 5. Stop recording → TTS → Notify → Start recording → Ready to listen.
        #    Mic is off during TTS and notify, so no echo is captured by the
        #    mic. The next listening round (_wait_for_speech) starts with a
        #    short settle window that absorbs residual echo while still
        #    capturing the first words of a quick follow-up question.
        await self._stop_recording()
        # Stop XiaoAI native listening so the on-device TTS channel is free.
        # The next round restores listening via _wait_for_xiaoai_asr_text().
        await self._stop_xiaoai_native_listening()
        await self._play_tts(str(response))
        await self._play_notify()
        await self._start_recording()
        logger.debug("Ready to listen", module=self.LOG_MODULE)

        # Keep listening after every reply so the user can ask the next
        # question without re-waking. The conversation ends on an exit
        # keyword, a silence timeout, or a "小爱同学" interrupt.
        logger.debug(
            f"HA continue_conversation={continue_conv}",
            module=self.LOG_MODULE,
        )
        return "continue"

    async def _run_one_turn_with_xiaoai_asr(self) -> str:
        """Execute a single conversation turn using XiaoAI native ASR."""
        text = await self._wait_for_xiaoai_asr_text()
        if text is None:
            logger.debug("XiaoAI native ASR turn timed out", module=self.LOG_MODULE)
            return "timeout"

        for kw in self.exit_keywords:
            if kw in text:
                logger.info(f"Exit keyword: {kw}", module=self.LOG_MODULE)
                return "exit"

        # rule_prompt is appended inside send_with_status().
        response, continue_conv = await self.backend.send_with_status(text)
        if response is None:
            # 同上：指令可能已执行，不自动重发。
            logger.warning(
                f"No response from {self.BACKEND_NAME} "
                "(指令可能已执行，请确认设备状态后再让用户重说)",
                module=self.LOG_MODULE,
            )
            speaker = get_speaker()
            if speaker:
                await speaker.play(text="抱歉，我没收到回复，请再说一遍")
            return "continue"

        await self._stop_recording()
        # Stop XiaoAI native listening so the on-device TTS channel is free.
        # The next round restores listening via _wait_for_xiaoai_asr_text().
        await self._stop_xiaoai_native_listening()
        await self._play_tts(str(response))
        await self._play_notify()
        await self._start_recording()
        logger.debug("Ready for next XiaoAI native ASR round", module=self.LOG_MODULE)

        # Keep listening after every reply so the user can ask the next
        # question without re-waking. The conversation ends on an exit
        # keyword, a silence timeout, or a "小爱同学" interrupt.
        logger.debug(
            f"HA continue_conversation={continue_conv}",
            module=self.LOG_MODULE,
        )
        return "continue"

    # ---- VAD integration ----

    async def _wait_for_speech(self, vad) -> bytes | None:
        """Use VAD to detect speech and collect the complete utterance.

        The mic is re-opened right after the reply/notify sound, so a short
        settle window absorbs any residual speaker echo before real speech
        capture starts. Speech that begins during the settle window is NOT
        dropped: it is buffered and handed over to capture, so a quick
        follow-up question is heard from its first word.

        Follows the same two-step pattern as the KWS wakeup session:
          1. settle ("silence" target) → absorb echo, keep early speech
          2. resume("speech") → wait for on_speech (voice detected)
          3. resume("silence") → keep recording → wait for on_silence

        The audio stream is tapped between step 2 and 3 to capture the
        full utterance that the VAD does not provide by itself.

        Returns:
            PCM bytes of captured speech, or None on timeout.
        """
        from core.wakeup_session import EventManager

        settle = max(0.0, float(self._cfg("listen_settle_seconds", 0.3) or 0))

        self._vad_future = self._loop.create_future()
        recording_frames: list[bytes] = []
        is_recording = False
        # Silence events during the settle window are expected (room echo
        # fading) and must not be treated as the end of an utterance.
        armed = settle <= 0

        original_on_speech = EventManager.on_speech
        original_on_silence = EventManager.on_silence

        def _on_speech_hook(speech_buffer: bytes):
            """Voice detected — save initial buffer, start recording, wait for silence."""
            nonlocal is_recording, armed
            armed = True
            recording_frames.append(speech_buffer)
            is_recording = True
            logger.debug(
                f"VAD speech detected, buffer size: {len(speech_buffer)}",
                module=self.LOG_MODULE,
            )
            # Now wait for silence to know user stopped speaking
            vad.resume("silence")

        def _on_silence_hook():
            """Silence detected — stop recording and resolve (only when armed)."""
            nonlocal is_recording
            if not armed:
                return
            is_recording = False
            logger.debug("VAD detected silence, stop recording", module=self.LOG_MODULE)
            if self._vad_future and not self._vad_future.done():
                self._loop.call_soon_threadsafe(
                    self._vad_future.set_result, b"".join(recording_frames)
                )

        # Tap into VAD's audio stream to record frames while waiting for silence
        _orig_handle_speech = vad._handle_speech_frame
        _orig_handle_silence = vad._handle_silence_frame

        def _recording_speech_frame(frames):
            if is_recording:
                recording_frames.append(bytes(frames))
            _orig_handle_speech(frames)

        def _recording_silence_frame(frames):
            if is_recording:
                recording_frames.append(bytes(frames))
            _orig_handle_silence(frames)

        EventManager.on_speech = _on_speech_hook
        EventManager.on_silence = _on_silence_hook
        vad._handle_speech_frame = _recording_speech_frame
        vad._handle_silence_frame = _recording_silence_frame

        try:
            if settle > 0:
                # Phase 1: settle. Run the VAD in "silence" target so any
                # residual echo is absorbed without firing on_speech.
                vad.resume("silence")
                await asyncio.sleep(settle)
                # VAD buffers speech frames as a flat list of byte values
                # (speech_frames.extend(frames) with frames being bytes).
                settle_speech = bytes(vad.speech_frames)
                min_speech_bytes = (
                    2 * vad.sample_rate * vad.min_speech_duration // 1000
                )
                if len(settle_speech) >= min_speech_bytes:
                    # The user already started talking during the settle
                    # window — hand the buffered speech straight to capture
                    # instead of starting a fresh listening phase, so the
                    # first words are preserved.
                    armed = True
                    is_recording = True
                    recording_frames.append(settle_speech)
                    vad.resume("silence")
                    result = await asyncio.wait_for(
                        self._vad_future, timeout=self.timeout
                    )
                    return result
                # Phase 2: normal speech capture (fresh start, stale audio
                # cleared by resume()).
                vad.resume("speech")
            else:
                vad.resume("speech")

            result = await asyncio.wait_for(self._vad_future, timeout=self.timeout)
            return result

        except asyncio.TimeoutError:
            logger.debug("VAD timeout, no speech detected", module=self.LOG_MODULE)
            vad.pause()
            return None

        finally:
            EventManager.on_speech = original_on_speech
            EventManager.on_silence = original_on_silence
            vad._handle_speech_frame = _orig_handle_speech
            vad._handle_silence_frame = _orig_handle_silence
            self._vad_future = None

    def _cancel_vad_future(self):
        """Cancel any pending VAD future."""
        if self._vad_future and not self._vad_future.done():
            self._loop.call_soon_threadsafe(self._vad_future.cancel)
        self._vad_future = None

    async def _wait_for_xiaoai_asr_text(self) -> str | None:
        """Wake XiaoAI and wait for a final native ASR result."""
        speaker = get_speaker()
        if not speaker:
            logger.error("Speaker not available", module=self.LOG_MODULE)
            return None

        deadline = self._loop.time() + self.timeout
        while self.active:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                logger.info("XiaoAI native ASR hit outer wait timeout", module=self.LOG_MODULE)
                return None

            self._xiaoai_asr_future = self._loop.create_future()
            try:
                wait_seconds = max(0.1, remaining)
                logger.debug(
                    f"Triggering XiaoAI native ASR, waiting up to {wait_seconds:.1f}s",
                    module=self.LOG_MODULE,
                )
                await speaker.wake_up(awake=True, silent=True)
                result = await asyncio.wait_for(
                    self._xiaoai_asr_future,
                    timeout=wait_seconds,
                )
                if result == self.XIAOAI_ASR_TIMEOUT:
                    logger.debug(
                        "XiaoAI native ASR ended without speech (native timeout), retrying",
                        module=self.LOG_MODULE,
                    )
                    continue
                return result
            except asyncio.TimeoutError:
                logger.info("XiaoAI native ASR hit outer wait timeout", module=self.LOG_MODULE)
                return None
            finally:
                self._xiaoai_asr_future = None

        return None

    def consume_xiaoai_recognize_result(
        self,
        dialog_id: str,
        text: str,
        is_final,
        is_vad_begin,
    ) -> bool:
        """Consume XiaoAI native ASR events while the backend is waiting."""
        if not (
            self.active
            and self.uses_xiaoai_asr()
            and self._xiaoai_asr_future
        ):
            return False

        normalized_text = text.strip() if isinstance(text, str) else ""
        if not is_final:
            logger.debug(
                f"Ignoring partial XiaoAI ASR result: {normalized_text}",
                module=self.LOG_MODULE,
            )
            return True

        # Silent wakeup may emit an empty final marker before real speech starts.
        if not normalized_text and is_vad_begin is False:
            logger.debug("Ignoring XiaoAI wake marker for native ASR", module=self.LOG_MODULE)
            return True

        if normalized_text:
            logger.debug(f"XiaoAI native ASR recognized: {normalized_text}", module=self.LOG_MODULE)
            self._resolve_xiaoai_asr_future(normalized_text)
            return True

        logger.debug(
            "XiaoAI native ASR received empty final result",
            module=self.LOG_MODULE,
        )
        self._resolve_xiaoai_asr_future(self.XIAOAI_ASR_TIMEOUT)
        return True

    def _resolve_xiaoai_asr_future(self, text: str):
        """Resolve the pending XiaoAI ASR future on the owner loop."""
        if self._xiaoai_asr_future and not self._xiaoai_asr_future.done():
            self._loop.call_soon_threadsafe(self._xiaoai_asr_future.set_result, text)

    def _cancel_xiaoai_asr_future(self):
        """Cancel any pending XiaoAI ASR future."""
        if self._xiaoai_asr_future and not self._xiaoai_asr_future.done():
            self._loop.call_soon_threadsafe(self._xiaoai_asr_future.cancel)
        self._xiaoai_asr_future = None

    async def _stop_xiaoai_native_listening(self):
        """Exit XiaoAI native listening before playing the goodbye prompt."""
        speaker = get_speaker()
        if not speaker:
            return
        try:
            await speaker.stop_device_audio()
            await speaker.wake_up(awake=False)
            # Give the on-device TTS service a moment to become ready.
            await asyncio.sleep(0.5)
        except Exception as exc:
            logger.debug(
                f"Failed to stop XiaoAI native listening: {exc}",
                module=self.LOG_MODULE,
            )

    # ---- Recording control (physical mute/unmute via remote arecord) ----

    async def _stop_recording(self):
        """Kill the remote arecord process so the mic doesn't pick up TTS."""
        try:
            await open_xiaoai_server.stop_recording()
            logger.debug("Recording stopped", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"stop_recording error: {exc}", module=self.LOG_MODULE)

    async def _start_recording(self):
        """Restart the remote arecord process to resume mic input."""
        try:
            await open_xiaoai_server.start_recording()
            logger.debug("Recording started", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"start_recording error: {exc}", module=self.LOG_MODULE)

    # ---- TTS ----

    async def _play_tts(self, text: str):
        """Play text via TTS (blocks until playback finishes).

        Retries once if the first attempt reports failure. The on-device
        XiaoAI TTS channel is sometimes not ready right after the native
        listening session is stopped, so a single retry makes playback
        much more reliable.
        """
        self._playback_token = open_xiaoai_server.begin_playback_session()
        try:
            ok = await self.backend.play_response_with_tts(
                text,
                tts_speaker=self.backend.get_tts_speaker_for_session_key(),
                playback_token=self._playback_token,
            )
            if not ok:
                logger.warning(
                    "TTS playback reported failure, retrying once",
                    module=self.LOG_MODULE,
                )
                await asyncio.sleep(0.3)
                ok = await self.backend.play_response_with_tts(
                    text,
                    tts_speaker=self.backend.get_tts_speaker_for_session_key(),
                    playback_token=self._playback_token,
                )
            if not ok:
                logger.error(
                    "TTS playback failed after retry",
                    module=self.LOG_MODULE,
                )
        except Exception as exc:
            logger.error(
                f"TTS playback error: {exc}",
                module=self.LOG_MODULE,
            )
            speaker = get_speaker()
            if speaker:
                await speaker.play(text=text)
        finally:
            self._playback_token = None

    async def _play_notify(self):
        """Play the listening-ready notification sound via PCM buffer."""
        if not _NOTIFY_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_NOTIFY_PCM)
                # Wait for playback to finish: PCM is int16 at 24000Hz
                duration = len(_NOTIFY_PCM) / (24000 * 2)
                await asyncio.sleep(duration)
            except Exception as exc:
                logger.debug(f"Notify sound error: {exc}", module=self.LOG_MODULE)

    async def _play_send_sound(self):
        """Play the send notification before waiting for backend response."""
        if not _SEND_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_SEND_PCM)
                # 等待播放完成：PCM 为 int16，24000Hz
                duration = len(_SEND_PCM) / (24000 * 2)
                await asyncio.sleep(duration)
            except Exception as exc:
                logger.debug(f"Send sound error: {exc}", module=self.LOG_MODULE)

    async def _call_after_wakeup(self):
        """Call the user-defined after_wakeup hook.

        The hook is user-supplied config; guard it so a bug in the callback
        (e.g. an IndexError while parsing session_key) never tears down the
        conversation exit flow.
        """
        after_wakeup = self.config.get_app_config("wakeup.after_wakeup")
        if after_wakeup:
            speaker = get_speaker()
            if speaker:
                try:
                    await after_wakeup(
                        speaker,
                        source=self.WAKEUP_SOURCE,
                        session_key=self.backend._session_key,
                    )
                except Exception as exc:
                    import traceback
                    logger.error(
                        f"after_wakeup hook error: {type(exc).__name__}: {exc}\n"
                        f"{traceback.format_exc()}",
                        module=self.LOG_MODULE,
                    )
