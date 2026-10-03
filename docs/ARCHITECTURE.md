# Architecture

PaperMap has a backend and a frontend that are kept strictly apart:

- **Backend**: a Python pipeline. It turns a paper and a profile into one JSON artifact, `experience.json`, whose schema is defined in [`models.py`](../src/papermap/models.py).
- **Frontend**: a dependency-free page in [`web/`](../src/papermap/web/). It renders whatever artifact is embedded in it.

The only coupling between them is the artifact schema.

```
                    ┌──────────────────────────── backend (Python) ───────────────────────────┐
paper.pdf ─┐        │ parse → understand → profile → explain → diagrams → graph → narrate      │
profile.md ┼──────► │   │          │           │          │          │        │        │        │──► render ──► index.html (+ audio/)
config ────┘        │   └──── stage cache (content-hashed) + LLM call cache + logs ───────────┘        experience.json, qa_index.json
                    └──────────────────────────────────────────────────────────────────────────┘
                                                                                                       │
                                                       papermap serve ──► /api/ask (grounded Q&A) ◄┘
```

## Repository layout

```
src/papermap/
  cli.py            commands: run (default), serve, init, check, stages
  config.py         TOML config + CLI overrides (pydantic); no secrets stored
  models.py         the data contract (stage outputs and the final Experience)
  pipeline.py       stage registry, cache keys, orchestration
  cache.py          content-addressed cache (stages, LLM calls, downloads, audio)
  grounding.py      quote / number / entity verification against the paper text
  retrieval.py      BM25 for Q&A
  qa.py             grounded question answering over a generated experience
  server.py         static file server + /api/ask + /api/health (stdlib only)
  log.py            console + per-run file logging
  llm/              provider interface, client (cache/retry/JSON), providers
  tts/              text-to-speech providers
  stages/           one module per pipeline stage
  web/              index.html template, app.css, app.js (the generated page)
tests/              pytest suite (mock provider, generated PDF)
```

## Pipeline and stages

A stage is declared in its own module as a `Stage` (see [`stages/base.py`](../src/papermap/stages/base.py)):

```python
Stage(name, version, deps, output, run, uses_llm=True, cacheable=True, key_extra=..., description=...)
```

`run(ctx, deps) -> BaseModel` receives the validated outputs of its dependencies and returns a pydantic model. The orchestrator in [`pipeline.py`](../src/papermap/pipeline.py) computes each stage's **cache key**:

```
hash(stage name, version, schema version, source hash of the package code,
     keys of its dependencies, the LLM settings for this stage, stage-specific inputs)
```

If an entry with that key exists, the stage is loaded instead of run. Because keys chain through dependencies, changing the profile invalidates `profile → explain → diagrams → narrate → render`. `parse`, `understand` and `graph` are reused, since they are audience-independent. Any code change invalidates the stage outputs computed with the old code (parse only tracks its own module), but unchanged LLM calls are still served from the call cache, so recomputing is cheap and results never go silently stale.

| Stage | Output model | What it does |
|---|---|---|
| `parse` | `ParsedPaper` | Fetches the PDF (path, URL or arXiv). Reads PyMuPDF spans and estimates the body font size. Detects headings from style plus numbering or well-known names, with a sanity filter on the numbering sequence (which removes table cells such as "73.7 MultiNLI"). Strips repeated headers and footers, and splits off references and figure captions. Falls back to page chunks when no structure is found. |
| `understand` | `Understanding` | One **plan** call chapters the raw sections into 4–9 logical sections and extracts the title, authors, method name, problem, contribution and key result. Sections are re-ordered, de-duplicated, and split when they exceed `max_section_chars`, so nothing is truncated. One **analyze** call per section extracts the summary, key points, verbatim quotes, equations, results, concepts and prior work. A synthetic overview section `s0` is grounded on the abstract. |
| `profile` | `AudienceProfile` | Turns free-form `profile.md` into a structured audience model. |
| `explain` | `Explanations` | For each section and each view (`high`, `deep`), writes 1–6 **beats**: spoken narration, an on-screen subtitle and an optional verified quote chosen by index. It also decides whether a diagram is needed and of which type. The prompt includes the whole episode outline, which avoids repetition across sections, plus the listener model. A check rejects numbers that are not in the section text and enforces the beat count. It also writes the suggested Q&A questions. |
| `diagrams` | `Diagrams` | For each requested diagram, produces a typed spec: `flow` (nodes, edges, groups), `bar`, `table` or `equation`. It also produces a per-beat **focus** map, listing the diagram elements to highlight while each beat plays. Structural problems and ungrounded numbers are fed back to the model; a diagram that cannot be fixed is skipped. |
| `graph` | `KnowledgeGraph` | Per-section entity and relation extraction, followed by a deterministic merge: citation markers are stripped, aliases and acronyms are resolved with union-find, entities not named in the paper are dropped, relations come from a closed vocabulary, and the graph is pruned to the most connected nodes. |
| `narrate` | `Narration` | Synthesizes one audio clip per beat, cached by text and voice, or defers narration to the browser. |
| `render` | `Experience` | Assembles the artifact and writes `index.html` with the data, CSS and JS inlined, plus `qa_index.json` and the audio files. This stage is never cached. |

All LLM stages run their per-section calls in parallel (`pipeline.concurrency`).

## LLM layer

```
LLMProvider.complete(LLMRequest) -> LLMResponse        # the whole provider contract
```

`LLMRequest` carries the messages, the system prompt, an optional large **context** block (the section text), the sampling settings, and the JSON mode or schema. Providers implement only that one call. [`LLMClient`](../src/papermap/llm/client.py) adds the rest:

- **Call cache.** Keyed by provider identity and the full request, which makes runs reproducible and resumable.
- **Retries.** Transient errors (timeouts, 429, 5xx) are retried with exponential backoff.
- **`complete_json(prompt, Schema, check=...)`.** Extracts JSON tolerantly: code fences, `<think>` blocks, trailing commas. It validates against the pydantic schema, runs semantic checks, and sends any problems back to the model for up to two repair attempts.
- **Structured output.** Uses the best mode each server supports: schema-constrained decoding (Ollama, vLLM, llama.cpp), JSON mode, or prompt-only. Schemas are flattened, and every key is required, which makes small models emit complete objects.
- **Usage accounting.** Calls, cache hits and tokens are reported at the end of a run.

**Prompt caching.** Every stage uses the same system prompt and puts the section text first, so the request prefix is shared. The Anthropic provider marks the context block with `cache_control`, and prefix caches in vLLM and llama.cpp benefit in the same way.

## Frontend

`index.html` is a template. `render` inlines `app.css`, `app.js` and the artifact JSON in a single pass, so inserted content is never re-scanned. The page has no runtime dependencies. KaTeX is loaded from a CDN only when equations are present, and plain LaTeX is shown when offline.

The page's main parts are in `app.js`:

- **State and player.** The state holds the view (`high`, `deep`, `graph`), the section index, the beat index, the playing flag and the mode (`read`, `qa`). The player advances beat by beat and section by section. Switching between high and deep keeps the section and maps the beat position proportionally. Progress and position are kept in `localStorage`, and the position is also kept in the URL hash, so links can point at a spot.
- **Narrator.** It has three backends: per-beat audio files, browser speech (split into sentences, with a watchdog for engines that never fire `onend`), or a silent timer based on reading speed.
- **Diagram renderers.** Each returns a `{focus(ids)}` controller:
  - Flow diagrams use a layered layout: cycles are broken by DFS, layers come from the longest path, and barycenter sweeps reduce crossings. The renderer picks left-to-right, top-to-bottom or wrapped bands to fit the stage's aspect ratio.
  - Bar charts use zero baselines, at most three series, selective value labels and hover tooltips.
  - Tables and KaTeX equations with term chips round out the renderers.
- **Knowledge graph.** A deterministic force-directed layout with pan, zoom and drag, plus type filters, search, rings around nodes that appear in the current section, and a details panel that links back to sections.
- **Q&A.** The page calls `api/health` and `api/ask` relative to its own URL. Q&A works in the side panel, scoped to a section, diagram or node, and as a full mode that unlocks after the paper is finished. Answers render with `[sN]` citation chips that jump to the cited section.

## Q&A server

`papermap serve <dir>` serves the folder and answers `POST /api/ask`. To build an answer, the engine retrieves the top BM25 chunks from `qa_index.json`, always including the section the listener is on. It adds every section's notes, the graph facts that touch entities mentioned in the question, and the JSON of the diagram being asked about. The prompt requires citations, and asks the model to say so plainly when the paper does not answer the question. The model configuration comes from `papermap.config.json`, which is the one used for generation. You can override it with `--config` or `--provider`/`--model`, or with a `[stages.qa]` section.

## Extension points

- **New model backend.** Subclass `LLMProvider`, then call `register_provider("name", factory)` or expose it through the `papermap.providers` entry point.
- **New TTS engine.** Subclass `TTSProvider` in `tts/` and add it to `create_tts`.
- **New diagram type.** Extend `Diagram` (fields, `problems()` and `element_ids()`), add a spec to `TYPE_SPECS` in `stages/diagrams.py`, and add a renderer in `app.js` that returns `{focus(ids)}`.
- **New stage.** Add a module with a `STAGE` and register it in `pipeline.STAGES`. Dependencies, caching and debug output then work automatically.
