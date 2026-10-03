# Guide

Reference for the profile, models, narration, configuration, output and debugging. For the internals, see [ARCHITECTURE.md](ARCHITECTURE.md).

You can choose the model per run without a config file:

```bash
paper2podcast paper.pdf -p profile.md --provider ollama --model qwen2.5:7b
paper2podcast paper.pdf -p profile.md --provider anthropic --model claude-opus-5-5
paper2podcast paper.pdf -p profile.md --provider vllm --model Qwen/Qwen2.5-32B-Instruct
paper2podcast paper.pdf -p profile.md --provider mock      # offline and deterministic, for trying the UI
```

`index.html` also works when opened directly from disk or hosted as a static site. Only Q&A needs `paper2podcast serve`, because answering questions requires a model.

## The profile

`profile.md` is free-form Markdown. Say who the listener is, what they already know, what is new to them, what they care about, how deep to go, the tone that works for them and the narration language. See the two examples in [`examples/`](../examples/): a research-minded ML engineer and a software engineer moving into ML. The `profile` stage turns it into a structured audience model, and every explanation is written against it. Concepts the listener already knows are not re-explained. Concepts they are missing get a short definition. Analogies come from fields the listener knows.

## Models

All model access goes through one small provider interface (`paper2podcast/llm/base.py`), so you can switch backends without touching the pipeline.

| `provider` | Backend | Notes |
|---|---|---|
| `anthropic` | Claude via the official SDK | Default model `claude-opus-5-5`. Prompt caching is used for the section text that several stages share. The server-side refusal fallback (`fallbacks = "default"`) is on by default; turn it off with `fallbacks = false`. |
| `ollama` | Ollama's native API | Uses schema-constrained JSON decoding and sets `num_ctx`, because Ollama's default context is small and truncates silently. |
| `openai`, `openrouter` | Hosted OpenAI-compatible APIs | Set `api_key_env`. |
| `vllm`, `llamacpp`, `lmstudio` | Local OpenAI-compatible servers | Default base URLs are preset; override them with `base_url`. |
| `openai_compatible` | Anything else that speaks `/chat/completions` | Requires `base_url`. |
| `mock` | Deterministic offline provider | Used by the tests and for UI work. Its content is extractive placeholder text. |

You can use a different model for one stage, for example a stronger model for diagrams:

```toml
[stages.diagrams]
model = "claude-opus-5-5"
provider = "anthropic"
```

Third-party backends can register themselves with `paper2podcast.llm.register_provider(name, factory)` or through the `paper2podcast.providers` entry-point group.

## Narration

| `[tts] provider` | What happens |
|---|---|
| `browser` (default) | The page speaks with the browser's speech engine. No files, no setup. A voice picker is in the page settings. |
| `edge` | Microsoft Edge neural voices, saved as MP3 per beat (needs `pip install "paper2podcast[edge-tts]"`). |
| `openai` | Any OpenAI-compatible `/audio/speech` server: OpenAI, or a local server such as Kokoro-FastAPI (set `base_url`, `model` and `voices`). |
| `none` | Silent. Captions advance at reading speed. |

Set `narration_style = "duo"` for a host-and-expert conversation instead of a single narrator.

## Configuration

Every key is optional. `paper2podcast init` writes a commented template. Configuration is resolved from `--config`, then `./paper2podcast.toml`, then `~/.config/paper2podcast/config.toml`, and finally built-in defaults. API keys are never stored; providers read them from the environment variable named by `api_key_env`.

```toml
[llm]
provider = "ollama"          # see the Models table
model = "qwen2.5:7b"
base_url = ""                # local servers / proxies
api_key_env = ""             # e.g. "OPENAI_API_KEY"
temperature = 0.2
seed = 7
max_tokens = 16000
structured_output = "auto"   # auto | schema | json | none
num_ctx = 32768              # ollama
think = false                # ollama: disable thinking for reasoning models
effort = "medium"            # anthropic: low | medium | high | xhigh | max
fallbacks = true             # anthropic: server-side refusal fallback

[tts]
provider = "browser"         # browser | edge | openai | none
voices = { narrator = "en-US-AndrewMultilingualNeural" }

[pipeline]
concurrency = 4              # parallel LLM calls (use 1 for a single local GPU)
narration_style = "narrator" # narrator | duo
min_sections = 4
max_sections = 9
max_section_chars = 18000    # longer sections are split, never truncated
high_beats = [1, 3]
deep_beats = [3, 6]
max_graph_nodes = 36
include_appendix = false
```

## Output

```
p2p-out/<paper>-<profile>/
  index.html          the experience: data, CSS and JS inlined; opens offline
  audio/              per-beat narration (edge / openai TTS only)
  experience.json     the full artifact the page renders (schema: paper2podcast/models.py)
  qa_index.json       retrieval chunks + section notes for Q&A
  p2p.config.json     the configuration used (no secrets); reused by `serve`
  debug/<stage>.json  every stage's output, for inspection
  logs/run.log        full DEBUG log of the run
```

The page supports deep links (`index.html#view=deep&s=3&b=1`), keyboard shortcuts (press `?`), light and dark themes, and phone-sized screens.

## How it works

```
Paper ─► parse ─► understand ─► profile ─► explain ─► diagrams ─► graph ─► narrate ─► render ─► webpage
          PDF      chapters,     audience   beats per    typed       knowledge  audio       experience.json
          layout   key points,   model      section and  specs +     graph      clips       + index.html
                   verified                 view         per-beat
                   quotes                                focus
```

Each stage is a pure function from its dependencies' outputs to a validated pydantic model. Stage outputs and individual LLM calls are cached by content hash. Re-running the same command is instant. If a run fails partway, re-running resumes from the last completed model call. Changing only the profile reuses parsing, understanding and the graph. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the components, interfaces and extension points.

### Grounding

These checks run in code and do not rely on the model following instructions:

- **Quotes** shown in the page must appear (near-)verbatim in the extracted paper text. Unmatched quotes are dropped.
- **Numbers** in narration, bar charts and tables must appear in the section text. Otherwise the model is asked to fix its output, and an unfixable diagram is skipped.
- **Graph entities** must be named in the paper. Citation markers are stripped and aliases such as acronyms are merged.
- **Q&A** answers are built only from retrieved excerpts, section notes and graph facts, and they must cite sections.

Grounding reduces errors but cannot eliminate them. A model can still misread a correct passage. Equations are reconstructed from PDF text extraction, which can be imperfect.

## Debugging

```bash
paper2podcast paper.pdf -p profile.md -v            # debug logging (also always in logs/run.log)
paper2podcast paper.pdf -p profile.md --until understand
paper2podcast paper.pdf -p profile.md --force diagrams   # recompute one stage (cached LLM calls are reused)
paper2podcast paper.pdf -p profile.md --no-cache         # completely fresh run
paper2podcast stages                                     # list stages and dependencies
```

The cache lives in `%LOCALAPPDATA%\paper2podcast\cache` on Windows and in `~/.cache/paper2podcast` elsewhere. Override it with `P2P_CACHE_DIR` or `--cache-dir`. Every model request and response is stored there as JSON.

## Development

```bash
pip install -e ".[dev,anthropic]"
pytest
```

The tests build a PDF, run the full pipeline with the `mock` provider, and check the artifact, verbatim quotes, caching, reproducibility, invalidation and Q&A.

## Limitations and roadmap

- Input is PDF only, with text-based PDFs (scanned PDFs need OCR first). HTML papers and LaTeX sources are planned.
- Tables are read from extracted text. Structured table extraction would improve result charts.
- Figures from the paper are not shown. Diagrams are generated from the paper's content instead.
- Q&A answers are not streamed yet.
- There are no production deployment assumptions. The server is local and single-user, with no authentication.
