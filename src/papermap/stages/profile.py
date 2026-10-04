"""Stage 3 - profile: profile.md (free-form) + reader memory -> structured AudienceProfile."""

from __future__ import annotations

from ..memory import apply_memory
from ..models import AudienceProfile, Understanding
from .base import SYSTEM_PROMPT, RunContext, Stage
from .common import context_block

PROFILE_PROMPT = """TASK: profile

The text above describes the person who will listen to an explanation of the paper "{title}" ({one_line}).
Build a structured model of this listener. Infer only what the profile supports; keep every list short (max 6 items).

Return JSON:
{{"name": "their first name, or Reader",
 "summary": "one sentence describing who they are",
 "expertise_level": "novice | intermediate | advanced | expert (relative to this paper's field)",
 "background": ["..."],
 "known_concepts": ["concepts they clearly already know and must not be re-explained"],
 "gaps": ["concepts this paper needs that they likely do not know"],
 "interests": ["..."],
 "goals": ["what they want from this paper"],
 "depth": "light | balanced | deep",
 "tone": "a short description of the voice that will work for them",
 "language": "narration language (English unless the profile asks otherwise)",
 "language_code": "BCP-47 code such as en-US or fr-FR",
 "analogy_domains": ["domains they know well that make good analogies"],
 "avoid": ["things to avoid in the explanation"]}}"""

DEFAULT_PROFILE = """A curious, technically literate reader who wants to understand what this paper contributes,
how it works and why it matters, without unnecessary jargon."""


def _run(ctx: RunContext, deps: dict) -> AudienceProfile:
    log = STAGE.log()
    und: Understanding = deps["understand"]
    text = ctx.profile_text.strip() or DEFAULT_PROFILE
    profile = ctx.llm("profile").complete_json(
        PROFILE_PROMPT.format(title=und.overview.title, one_line=und.overview.one_line),
        AudienceProfile,
        system=SYSTEM_PROMPT,
        context=context_block(und.overview.title, "LISTENER PROFILE (profile.md)", text),
        tag="profile",
    )
    log.info("%s - %s, depth %s, %s", profile.name, profile.expertise_level, profile.depth, profile.language)
    if ctx.memory is not None and not ctx.memory.empty:
        profile = apply_memory(profile, ctx.memory)
        log.info("reader memory: %d papers; knows %d concepts, struggled with %d%s", ctx.memory.papers,
                 len(profile.mastered), len(profile.struggles), f"; {profile.level_note}" if profile.level_note else "")
    return profile


STAGE = Stage(
    name="profile",
    version="1",
    deps=("understand",),
    output=AudienceProfile,
    run=_run,
    key_extra=lambda ctx: (ctx.profile_text, ctx.memory.digest() if ctx.memory is not None else ""),
    description="profile.md + reader memory -> structured audience model",
)
