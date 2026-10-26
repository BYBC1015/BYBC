from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


SPEECH_CONTRACT_VERSION = "voice-v1"
SPEECH_OUTPUT_FORMATS = ("audio/mpeg", "audio/ogg", "audio/wav")


@dataclass(frozen=True, slots=True)
class TextToSpeechRequest:
    text: str
    voice_id: str | None = None
    language: str | None = None
    output_format: str = "audio/mpeg"


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    content: bytes
    content_type: str
    provider_request_id: str | None = None


class TextToSpeechProvider(Protocol):
    """Provider-neutral seam for a later TTS implementation."""

    def synthesize(self, request: TextToSpeechRequest) -> SpeechAudio: ...


def speech_capabilities() -> dict[str, object]:
    """Advertise direct client speech without routing raw audio through Hub."""

    return {
        "contract_version": SPEECH_CONTRACT_VERSION,
        "status": "client_available",
        "transport": "direct_on_device",
        "tts": {
            "enabled": True,
            "streaming": False,
            "server_synthesis": False,
            "client_modes": ["android_native", "web_speech"],
            "output_formats": list(SPEECH_OUTPUT_FORMATS),
        },
        "voice_input": {
            "enabled": True,
            "client_modes": ["android_native", "web_speech"],
            "sends_audio_to_hub": False,
            "send_requires_confirmation": True,
        },
    }
