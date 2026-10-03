"""Stage 7 - narrate: synthesize one audio clip per beat (or defer to the
browser's speech engine). Clips are cached by text+voice, so editing one beat
only re-synthesizes that beat."""

from __future__ import annotations

from ..config import stable_hash
from ..models import AudienceProfile, Explanations, Narration, NarrationInfo
from ..tts import DEFAULT_VOICES, create_tts, speakable
from .base import RunContext, Stage


def _run(ctx: RunContext, deps: dict) -> Narration:
    log = STAGE.log()
    exp: Explanations = deps["review"]
    profile: AudienceProfile = deps["profile"]
    settings = ctx.config.tts
    provider_name = settings.provider.lower()
    tts = create_tts(settings)
    voices = {**DEFAULT_VOICES.get(provider_name, {}), **settings.voices}
    info = NarrationInfo(provider=provider_name, language_code=profile.language_code, voices=voices, rate=settings.rate)
    if tts is None:
        log.info("%s narration: audio is produced in the browser at playback time", provider_name)
        return Narration(info=info)

    beats = [b for v in exp.views.values() for s in v.sections for b in s.beats]

    def synth(beat):
        voice = tts.voice_for(beat.speaker)
        text = speakable(beat.narration)
        key = stable_hash(tts.name, settings.model, settings.base_url, voice, settings.rate, text)[:24]
        path = ctx.cache.blob_path("audio", key, tts.ext)
        if not path.is_file():
            ctx.cache.put_blob("audio", key, tts.ext, tts.synthesize(text, voice))
        return beat.id, f"audio/{key}.{tts.ext}"

    clips = dict(ctx.parallel(synth, beats))
    log.info("%d clips synthesized with %s", len(clips), tts.name)
    return Narration(info=info, clips=clips)


STAGE = Stage(
    name="narrate",
    version="1",
    deps=("review", "profile"),
    output=Narration,
    run=_run,
    uses_llm=False,
    key_extra=lambda ctx: ctx.config.tts.model_dump(mode="json"),
    description="per-beat audio clips (or browser speech)",
)
