#!/usr/bin/env python3
"""
Smoke test the local Paraformer ASR pipeline end to end.

Synthesizes a sentence with Doubao TTS and runs the resulting PCM through
the local Sherpa-ONNX recognizer, then prints what it heard.

Usage:
  python3 tests/test_local_asr.py
  python3 tests/test_local_asr.py --text "打开客厅灯"
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def add_local_venv_site_packages() -> None:
    venv_lib = PROJECT_ROOT / ".venv" / "lib"
    if not venv_lib.exists():
        return
    for site_packages in sorted(venv_lib.glob("python*/site-packages"), reverse=True):
        site_path = str(site_packages)
        if site_path not in sys.path:
            sys.path.insert(0, site_path)
        break


add_local_venv_site_packages()

import aiohttp

from core.utils.config_loader import ensure_config_module_loaded

ensure_config_module_loaded()
from config import APP_CONFIG
from core.services.audio.asr import ASRService


DEFAULT_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"


def get_arg(name: str, default=None):
    if name in sys.argv:
        index = sys.argv.index(name)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return default


async def fetch_tts_pcm(
    text: str,
    api_key: str,
    resource_id: str,
    speaker: str,
    sample_rate: int = 16000,
) -> bytes:
    """Fetch raw PCM audio from Doubao (mirrors the native client contract)."""
    payload = {
        "user": {"uid": "open-xiaoai-bridge"},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {
                "format": "pcm",
                "sample_rate": sample_rate,
                "enable_timestamp": False,
                "speed": 1.0,
            },
            "additions": json.dumps(
                {
                    "explicit_language": "zh",
                    "disable_markdown_filter": True,
                }
            ),
        },
    }
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }
    chunks: list[bytes] = []
    async with aiohttp.ClientSession() as session:
        async with session.post(DEFAULT_URL, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Doubao API returned {resp.status}: {body[:300]}")
            async for line in resp.content:
                if not line.strip():
                    continue
                data = json.loads(line)
                code = data.get("code", 0)
                if code == 0:
                    b64 = data.get("data") or ""
                    if b64:
                        chunks.append(base64.b64decode(b64))
                elif code == 20000000:
                    break
                else:
                    raise RuntimeError(
                        f"Doubao API error {code}: {data.get('message')}"
                    )
    if not chunks:
        raise RuntimeError("Doubao returned no audio")
    return b"".join(chunks)


async def main() -> None:
    text = get_arg("--text", "今天天气真不错，帮我打开客厅的灯")
    tts_config = APP_CONFIG.get("tts", {}).get("doubao", {})
    api_key = str(tts_config.get("api_key") or "")
    speaker = tts_config.get("default_speaker", "zh_female_xiaohe_uranus_bigtts")

    if not api_key:
        raise RuntimeError("Please configure tts.doubao.api_key in config.py first")

    print("=" * 60)
    print("Local ASR (Paraformer) smoke test")
    print("=" * 60)
    print(f"TTS text      : {text}")
    print(f"TTS speaker   : {speaker}")

    # 1. Synthesize speech at 16 kHz PCM via Doubao TTS.
    pcm = await fetch_tts_pcm(
        text,
        api_key,
        resource_id="seed-tts-2.0",
        speaker=speaker,
        sample_rate=16000,
    )
    print(f"TTS PCM       : {len(pcm)} bytes ({len(pcm) / (16000 * 2) * 1000:.0f} ms)")

    # 2. Run the local recognizer over the synthesized speech.
    recognized = ASRService.asr(pcm, sample_rate=16000)
    print(f"ASR result    : {recognized!r}")

    if not recognized:
        raise RuntimeError("Local ASR returned empty text")
    print("\n[OK] Local ASR recognized the synthesized speech")


if __name__ == "__main__":
    asyncio.run(main())
