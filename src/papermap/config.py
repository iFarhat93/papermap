"""Configuration: TOML file + CLI overrides, validated with pydantic.

Lookup order (first found wins): ``--config PATH``, ``./papermap.toml``,
``~/.config/papermap/config.toml``. Missing file -> built-in defaults.
Secrets never live in the config: providers read API keys from the
environment variable named by ``api_key_env``.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONFIG_FILENAME = "papermap.toml"


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMSettings(Section):
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.2
    seed: int | None = 7
    max_tokens: int = 16000
    timeout: float = 600.0
    max_retries: int = 3
    # How hard to push the server for JSON: "auto" picks the best the provider supports,
    # "schema" = schema-constrained decoding, "json" = JSON mode, "none" = prompt only.
    structured_output: str = "auto"
    # Ollama only: context window (Ollama's own default is small and silently truncates).
    num_ctx: int | None = 32768
    # Ollama only: send "think": <value> (None = don't send).
    think: bool | None = None
    # Anthropic only: output_config.effort and server-side refusal fallbacks.
    effort: str | None = "medium"
    fallbacks: bool = True
    # Extra fields merged verbatim into the request body (provider specific).
    extra: dict[str, Any] = Field(default_factory=dict)


class TTSSettings(Section):
    # browser: Web Speech API at playback time (no files, zero setup)
    # none:    silent, auto-advancing captions
    # openai:  any OpenAI-compatible /audio/speech server (OpenAI, Kokoro-FastAPI, ...)
    # edge:    Microsoft Edge neural voices via the optional `edge-tts` package
    provider: str = "browser"
    model: str = "gpt-4o-mini-tts"
    base_url: str | None = None
    api_key_env: str | None = None
    format: str = "mp3"
    rate: float = 1.0
    # speaker -> voice. Speakers: "narrator" (narrator style) or "host"/"expert" (duo style).
    voices: dict[str, str] = Field(default_factory=dict)
    timeout: float = 120.0


class PipelineSettings(Section):
    concurrency: int = 4
    # "narrator": one voice. "duo": a host and an expert in conversation.
    narration_style: str = "narrator"
    min_sections: int = 4
    max_sections: int = 9
    # Logical sections longer than this are split so no prompt silently overflows.
    max_section_chars: int = 18000
    high_beats: tuple[int, int] = (1, 3)
    deep_beats: tuple[int, int] = (3, 6)
    max_graph_nodes: int = 36
    include_appendix: bool = False
    # Use the arXiv LaTeX source when the paper is on arXiv (exact sections, equations, tables, figures).
    use_latex: bool = True
    # Extract the paper's own figures and show them next to the generated diagrams.
    paper_figures: bool = True
    # Second pass that checks the narration against the paper and fixes errors before diagrams are built.
    review: bool = True
    # End-of-paper comprehension quiz (multiple choice, answer keys verified against the paper).
    quiz: bool = True
    quiz_questions: int = 10  # at least 10 are kept when the paper allows it


class Config(Section):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    # Per-stage LLM overrides, e.g. [stages.diagrams] model = "..."
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)

    def llm_for(self, stage: str) -> LLMSettings:
        override = self.stages.get(stage) or {}
        if not override:
            return self.llm
        return LLMSettings.model_validate({**self.llm.model_dump(), **override})

    def fingerprint(self, *parts: Any) -> str:
        return stable_hash(self.model_dump(mode="json"), *parts)

    def public_dump(self) -> dict[str, Any]:
        """Config as written into the output folder (no secrets are ever stored)."""
        return self.model_dump(mode="json")


def stable_hash(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config(explicit: str | Path | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        return p
    for candidate in (Path.cwd() / CONFIG_FILENAME, Path.home() / ".config" / "papermap" / "config.toml"):
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> tuple[Config, Path | None]:
    found = find_config(path)
    data: dict[str, Any] = {}
    if found:
        with open(found, "rb") as f:
            data = tomllib.load(f)
    if overrides:
        data = _deep_merge(data, overrides)
    return Config.model_validate(data), found


EXAMPLE_CONFIG = """\
# PaperMap configuration. Every key is optional.

[llm]
# Cloud (default): Anthropic. Needs `pip install "papermap[anthropic]"` and ANTHROPIC_API_KEY.
provider = "anthropic"
model = "claude-opus-5-5"
effort = "medium"          # low | medium | high | xhigh | max
fallbacks = true           # server-side refusal fallback (Anthropic API only)

# Local with Ollama (native API, no key):
# provider = "ollama"
# model = "qwen2.5:7b"
# num_ctx = 32768

# Any OpenAI-compatible server (OpenAI, vLLM, llama.cpp, LM Studio, OpenRouter, ...):
# provider = "openai"        # or vllm | llamacpp | lmstudio | openrouter | openai_compatible
# model = "gpt-4.1"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"

temperature = 0.2
seed = 7

# Use a different model for one stage:
# [stages.diagrams]
# model = "claude-opus-5-5"

[tts]
provider = "browser"       # browser | none | openai | edge
# provider = "openai"      # OpenAI or a local OpenAI-compatible TTS server (e.g. Kokoro-FastAPI)
# base_url = "http://localhost:8880/v1"
# model = "kokoro"
# voices = { narrator = "af_heart", host = "af_heart", expert = "am_michael" }

[pipeline]
concurrency = 4
narration_style = "narrator"   # narrator | duo
"""
