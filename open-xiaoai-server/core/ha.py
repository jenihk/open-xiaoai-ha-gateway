"""Home Assistant Assist client.

Receives recognized text from the XiaoAI speaker and sends it to a Home
Assistant conversation agent (e.g. the extended OpenAI Conversation
integration) through the ``conversation/process`` REST API.

The spoken reply is extracted from the response and returned for TTS
playback. Multi-turn context is kept per agent through the HA
``conversation_id``.

Configuration (``config.py``):

    "ha": {
        "base_url": "http://homeassistant.local:8123",
        "token": "your long-lived access token",
        "agent_id": "",                 # optional, empty => HA default agent
        "conversation_id": "",          # optional default conversation id
        "input_mode": "local_asr",      # "local_asr" | "xiaoai_asr"
        "exit_keywords": ["退出", "停止", "再见"],
        "response_timeout": 60,         # seconds
        "language": "zh-CN",            # optional
        "tts_speaker": "xiaoai",        # "xiaoai" or a Doubao voice id
        "session_tts_speakers": {},     # agent_id -> voice id
        "rule_prompt": "",              # appended to every message
    }
"""

import asyncio
import json
import os
from typing import Optional

import aiohttp

from core.utils.config import ConfigManager
from core.utils.logger import logger


class HAManager:
    """Manager for Home Assistant conversation/process calls."""

    _instance = None
    _initialized = False
    _reload_listener_registered = False

    # Feature flag
    _enabled = False

    # Connection
    _base_url = "http://homeassistant.local:8123"
    _token = ""
    _agent_id = ""
    _conversation_id = ""
    _language = ""
    _response_timeout = 60  # seconds

    # Routing / conversation state
    _session_key = ""  # current agent id, reset to default on each wakeup
    _conversation_ids: dict[str, str] = {}  # agent_id -> HA conversation_id

    # Input / exit behavior
    _input_mode = "local_asr"  # "local_asr" | "xiaoai_asr"
    _exit_keywords: list[str] = ["退出", "停止", "再见"]

    # Prompt appended to every message sent to HA
    _rule_prompt = ""

    # TTS
    XIAOAI_TTS_SPEAKER = "xiaoai"
    _tts_speaker: Optional[str] = None
    _session_tts_speakers: dict[str, str] = {}

    # aiohttp session (created lazily on the app event loop)
    _session: Optional[aiohttp.ClientSession] = None

    @classmethod
    def initialize_from_config(cls, enabled: bool | None = None):
        """Initialize the manager from config."""
        logger.info("[HA] Initializing from config...")
        cls.reload_from_config(enabled=enabled)
        cls._initialized = True

    @classmethod
    def reload_from_config(cls, enabled: bool | None = None):
        """Refresh HA configuration from config.py."""
        config_manager = ConfigManager.instance()
        if not cls._reload_listener_registered:
            config_manager.add_reload_listener(
                lambda _old, _new: cls.reload_from_config()
            )
            cls._reload_listener_registered = True

        config = config_manager.get_app_config("ha", {})
        cfg_enabled = config.get("enabled")
        if enabled is not None:
            cls._enabled = enabled
        elif cfg_enabled is not None:
            cls._enabled = bool(cfg_enabled)
        else:
            env_enabled = os.environ.get("HA_ENABLE", "1")
            cls._enabled = env_enabled.strip().lower() in ("1", "true", "yes", "on")

        cls._base_url = (config.get("base_url") or cls._base_url).rstrip("/")
        cls._token = config.get("token", "")
        cls._agent_id = config.get("agent_id", "")
        cls._conversation_id = config.get("conversation_id", "")
        cls._language = config.get("language", "")
        cls._response_timeout = int(config.get("response_timeout", 60))
        cls._input_mode = str(config.get("input_mode", "local_asr")).strip().lower()
        cls._exit_keywords = config.get("exit_keywords", ["退出", "停止", "再见"])
        cls._rule_prompt = str(config.get("rule_prompt", "") or "")
        cls._tts_speaker = config.get("tts_speaker")
        cls._session_tts_speakers = {
            str(key): str(value)
            for key, value in (config.get("session_tts_speakers") or {}).items()
            if key and value
        }

        # Default session key: configured agent id (or empty for HA default agent)
        cls._session_key = cls._agent_id

        if cls._enabled:
            logger.info(
                f"[HA] Enabled, base_url={cls._base_url}, "
                f"agent_id={cls._agent_id or '(default)'}, "
                f"input_mode={cls._input_mode}"
            )

    @classmethod
    def set_agent_id(cls, agent_id: str):
        """Route the next conversation to a different HA agent at runtime."""
        agent_id = agent_id or ""
        if agent_id != cls._agent_id:
            logger.info(f"[HA] Agent id updated: {cls._agent_id!r} -> {agent_id!r}")
        cls._agent_id = agent_id
        cls._session_key = agent_id

    @classmethod
    def reset_session(cls):
        """Reset the routing key and start a fresh conversation on next wakeup."""
        cls._session_key = cls._agent_id
        cls._conversation_ids.clear()
        cls._conversation_id = ConfigManager.instance().get_app_config(
            "ha.conversation_id", ""
        )

    @classmethod
    def get_session_key(cls) -> str:
        return cls._session_key

    @classmethod
    def uses_xiaoai_asr(cls) -> bool:
        return cls._input_mode == "xiaoai_asr"

    @classmethod
    def is_enabled(cls) -> bool:
        if not cls._initialized:
            cls.initialize_from_config()
        return cls._enabled

    @classmethod
    def is_connected(cls) -> bool:
        # HA is reached over HTTP; connectivity is checked per request.
        return cls._enabled

    @classmethod
    async def _get_session(cls) -> aiohttp.ClientSession:
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession()
        return cls._session

    @classmethod
    async def close(cls):
        if cls._session and not cls._session.closed:
            await cls._session.close()
        cls._session = None

    @classmethod
    async def process(cls, text: str) -> tuple[Optional[str], bool]:
        """Send text to HA and return ``(reply_text, continue_conversation)``.

        Returns ``(None, False)`` on any failure so callers can fall back to
        a local error prompt.
        """
        if not cls.is_enabled():
            logger.warning("[HA] HA is disabled, cannot process text")
            return None, False

        if not cls._token:
            logger.error("[HA] Long-lived access token is not configured")
            return None, False

        payload: dict = {"text": text}
        if cls._agent_id:
            payload["agent_id"] = cls._agent_id
        conversation_id = cls._conversation_ids.get(
            cls._session_key, cls._conversation_id
        )
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if cls._language:
            payload["language"] = cls._language

        url = f"{cls._base_url}/api/conversation/process"
        headers = {
            "Authorization": f"Bearer {cls._token}",
            "Content-Type": "application/json",
        }

        try:
            session = await cls._get_session()
            timeout = aiohttp.ClientTimeout(total=cls._response_timeout)
            async with session.post(
                url, json=payload, headers=headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        f"[HA] conversation/process failed: HTTP {resp.status}: {body[:200]}"
                    )
                    return None, False
                data = await resp.json()
        except asyncio.TimeoutError:
            logger.error(
                f"[HA] conversation/process timed out after {cls._response_timeout}s"
            )
            return None, False
        except aiohttp.ClientError as exc:
            logger.error(f"[HA] conversation/process request error: {type(exc).__name__}: {exc}")
            return None, False

        response = data.get("response") or {}
        speech = response.get("speech") or {}
        reply = ""
        for kind in ("plain", "ssml"):
            value = (speech.get(kind) or {}).get("speech")
            if value:
                reply = str(value)
                break

        new_conversation_id = data.get("conversation_id")
        if new_conversation_id:
            cls._conversation_ids[cls._session_key] = new_conversation_id

        continue_conversation = bool(data.get("continue_conversation", False))
        if reply:
            logger.info(
                f"[HA] Reply (continue={continue_conversation}): {reply[:100]}"
            )
        else:
            # HA returned 200 but no speech text (e.g. the cloud LLM executed
            # the action but produced an empty final response). Log the full
            # response shape so the next occurrence is diagnosable.
            logger.warning(
                "[HA] conversation/process returned no speech text: "
                f"response_type={response.get('response_type')!r}, "
                f"language={response.get('language')!r}, "
                f"data={json.dumps(response.get('data'), ensure_ascii=False)[:300]}"
            )
        return (reply or None), continue_conversation

    @classmethod
    async def send(cls, text: str, wait_response: bool = True) -> Optional[str]:
        """Send text to HA and return the spoken reply (or None)."""
        full_text = text
        if cls._rule_prompt:
            full_text = text + "\n" + cls._rule_prompt
        reply, _continue = await cls.process(full_text)
        return reply

    @classmethod
    async def send_with_status(cls, text: str) -> tuple[Optional[str], bool]:
        """Send text to HA and return ``(reply_text, continue_conversation)``.

        The continuous conversation loop uses the ``continue_conversation``
        flag returned by HA to decide whether to keep listening for the next
        turn (``True``) or finish the conversation (``False``).
        """
        full_text = text
        if cls._rule_prompt:
            full_text = text + "\n" + cls._rule_prompt
        return await cls.process(full_text)

    @classmethod
    async def send_and_play_reply(
        cls, text: str, wait_response: bool = True
    ) -> Optional[str]:
        """Send text to HA and play the reply through TTS."""
        reply = await cls.send(text, wait_response=wait_response)
        if reply:
            await cls.play_response_with_tts(reply)
        return reply

    @classmethod
    def get_tts_speaker_for_session_key(
        cls, session_key: str | None = None
    ) -> Optional[str]:
        """Resolve the TTS speaker for the current agent."""
        target = session_key or cls._session_key
        if target:
            for agent_id, speaker in cls._session_tts_speakers.items():
                if agent_id in target:
                    return speaker
        return cls._tts_speaker

    @classmethod
    async def play_response_with_tts(
        cls,
        text: str,
        tts_speaker: str | None = None,
        playback_token: int | None = None,
    ) -> bool:
        """Synthesize text and play it through the speaker.

        Returns True if playback was initiated successfully, False otherwise
        so callers can retry.
        """
        from core.ref import get_speaker

        resolved_tts_speaker = tts_speaker or cls.get_tts_speaker_for_session_key()

        # Special value: use XiaoAI native TTS directly
        if resolved_tts_speaker == cls.XIAOAI_TTS_SPEAKER:
            speaker = get_speaker()
            if not speaker:
                return False
            return bool(await speaker.play(text=text, blocking=True))

        from core.services.tts.doubao import DoubaoTTS

        tts_config = ConfigManager.instance().get_app_config("tts.doubao", {})
        api_key = str(tts_config.get("api_key") or "")
        if not api_key:
            logger.warning(
                "[HA] Doubao API Key not configured, "
                "falling back to XiaoAI native TTS"
            )
            speaker = get_speaker()
            if not speaker:
                return False
            return bool(await speaker.play(text=text, blocking=True))

        speaker_id = resolved_tts_speaker or tts_config.get(
            "default_speaker", "zh_female_xiaohe_uranus_bigtts"
        )
        try:
            # Explicit resource_id from config overrides auto-detection.
            tts = DoubaoTTS(
                speaker=speaker_id,
                resource_id=tts_config.get("resource_id"),
            )
            resolved_format = tts.resolve_audio_format(text)
            use_stream = tts_config.get("stream", False)

            import open_xiaoai_server

            if use_stream:
                await open_xiaoai_server.tts_stream_play(
                    text,
                    api_key=api_key,
                    resource_id=tts.resource_id,
                    speaker=speaker_id,
                    format=resolved_format,
                    sample_rate=24000,
                    playback_token=playback_token,
                )
            else:
                await open_xiaoai_server.tts_play(
                    text,
                    api_key=api_key,
                    resource_id=tts.resource_id,
                    speaker=speaker_id,
                    format=resolved_format,
                    sample_rate=24000,
                    playback_token=playback_token,
                )
            return True
        except Exception as exc:
            logger.error(f"[HA] TTS playback error: {type(exc).__name__}: {exc}")
            speaker = get_speaker()
            if not speaker:
                return False
            return bool(await speaker.play(text=text, blocking=True))
