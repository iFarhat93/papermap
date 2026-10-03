"""Text-to-speech providers.

  browser  no files; the page speaks with the Web Speech API (zero setup)
  none     no audio; captions auto-advance at reading speed
  openai   any OpenAI-compatible ``/audio/speech`` endpoint (OpenAI, Kokoro-FastAPI, ...)
  edge     Microsoft Edge neural voices (``pip install "papermap[edge-tts]"``)
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod

import httpx

from ..config import TTSSettings

FILE_PROVIDERS = {"openai", "edge"}

DEFAULT_VOICES: dict[str, dict[str, str]] = {
    "openai": {"narrator": "alloy", "host": "nova", "expert": "onyx"},
    "edge": {
        "narrator": "en-US-AndrewMultilingualNeural",
        "host": "en-US-AvaMultilingualNeural",
        "expert": "en-US-AndrewMultilingualNeural",
    },
    "browser": {},
    "none": {},
}


class TTSError(RuntimeError):
    pass


_GREEK = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau "
          "upsilon phi chi psi omega").split()
_GREEK_CHARS = dict(zip("αβγδεζηθικλμνξοπρστυφχψω", _GREEK))


def speakable(text: str) -> str:
    """Make narration safe for a speech engine: LaTeX and symbols become words
    ("$\\pi_0$" -> "pi 0", "x^2" -> "x squared")."""
    import re

    def math(m: re.Match) -> str:
        t = m.group(1)
        t = re.sub(r"\\(mathrm|text|mathbf|mathcal|operatorname|bm|boldsymbol)\{([^}]*)\}", r"\2", t)
        t = re.sub(r"\^\{?2\}?", " squared", t)
        t = re.sub(r"\^\{?T\}?", " transpose", t)
        t = re.sub(r"\^\{?-1\}?", " inverse", t)
        t = re.sub(r"\\(" + "|".join(_GREEK) + r")(?![A-Za-z])", r" \1 ", t, flags=re.I)
        t = re.sub(r"[_^]\{([^}]*)\}", r" \1", t)
        t = re.sub(r"[_^]", " ", t)
        t = re.sub(r"\\[A-Za-z]+", " ", t)
        t = re.sub(r"[{}\\]", " ", t)
        return " " + t + " "

    text = re.sub(r"\$\$?(.+?)\$\$?", math, text)
    text = "".join(f" {_GREEK_CHARS[c]} " if c in _GREEK_CHARS else c for c in text)
    return re.sub(r"\s+", " ", text).strip()


class TTSProvider(ABC):
    name = "base"

    def __init__(self, settings: TTSSettings):
        self.settings = settings

    @property
    def ext(self) -> str:
        return self.settings.format

    def voice_for(self, speaker: str) -> str:
        voices = {**DEFAULT_VOICES.get(self.name, {}), **self.settings.voices}
        return voices.get(speaker) or voices.get("narrator") or next(iter(voices.values()), "")

    @abstractmethod
    def synthesize(self, text: str, voice: str) -> bytes:  # pragma: no cover - interface
        ...


class OpenAITTS(TTSProvider):
    name = "openai"

    def __init__(self, settings: TTSSettings):
        super().__init__(settings)
        self.base_url = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
        env = settings.api_key_env or ("OPENAI_API_KEY" if "api.openai.com" in self.base_url else None)
        self.api_key = os.environ.get(env) if env else None
        if "api.openai.com" in self.base_url and not self.api_key:
            raise TTSError(f"environment variable {env} is not set (needed for OpenAI TTS)")

    def synthesize(self, text: str, voice: str) -> bytes:
        body = {"model": self.settings.model, "input": text, "voice": voice, "response_format": self.settings.format}
        if self.settings.rate != 1.0:
            body["speed"] = self.settings.rate
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = httpx.post(f"{self.base_url}/audio/speech", json=body, headers=headers, timeout=self.settings.timeout)
        except httpx.HTTPError as e:
            raise TTSError(f"TTS request to {self.base_url} failed: {e}") from e
        if r.status_code >= 400:
            raise TTSError(f"TTS HTTP {r.status_code}: {r.text[:300]}")
        return r.content


class EdgeTTS(TTSProvider):
    name = "edge"

    @property
    def ext(self) -> str:
        return "mp3"

    def __init__(self, settings: TTSSettings):
        super().__init__(settings)
        try:
            import edge_tts  # noqa: F401
        except ImportError as e:
            raise TTSError('the edge provider needs: pip install "papermap[edge-tts]"') from e

    def synthesize(self, text: str, voice: str) -> bytes:
        import edge_tts

        rate = f"{round((self.settings.rate - 1) * 100):+d}%"

        async def go() -> bytes:
            out = bytearray()
            async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
                if chunk.get("type") == "audio":
                    out.extend(chunk["data"])
            return bytes(out)

        try:
            data = asyncio.run(go())
        except Exception as e:  # noqa: BLE001 - network/service errors surface as TTSError
            raise TTSError(f"edge-tts failed: {e}") from e
        if not data:
            raise TTSError("edge-tts returned no audio")
        return data


def create_tts(settings: TTSSettings) -> TTSProvider | None:
    name = settings.provider.lower()
    if name in ("browser", "none"):
        return None
    if name == "openai":
        return OpenAITTS(settings)
    if name == "edge":
        return EdgeTTS(settings)
    raise TTSError(f"unknown TTS provider {settings.provider!r}; choose browser, none, openai or edge")
