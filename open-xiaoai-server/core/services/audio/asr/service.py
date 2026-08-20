"""Local ASR service (Sherpa-ONNX offline recognition)."""

from core.services.audio.asr.sherpa import SherpaASR


class _ASRService:
    """Dispatch ASR requests to the local Sherpa model."""

    def ensure_loaded(self) -> None:
        SherpaASR._ensure_loaded()

    def asr(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        return SherpaASR.asr(pcm_bytes, sample_rate=sample_rate)


ASRService = _ASRService()
