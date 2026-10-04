# Guide

Reference for the profile, models, narration, configuration, output and debugging. For the internals, see [ARCHITECTURE.md](ARCHITECTURE.md).

You can choose the model per run without a config file:

```bash
papermap paper.pdf -p profile.md --provider ollama --model qwen2.5:7b
papermap paper.pdf -p profile.md --provider anthropic --model claude-opus-5-5
papermap paper.pdf -p profile.md --provider vllm --model Qwen/Qwen2.5-32B-Instruct
papermap paper.pdf -p profile.md --provider mock      # offline and deterministic, for trying the UI
```

`index.html` also works when opened directly from disk or hosted as a static site. Only asking questions needs `papermap serve`, because answering them requires a model.

## The profile

`profile.md` is free-form Markdown. Say who the listener is, what they already know, what is new to them, what they care about, how deep to go, the tone that works for them and the narration language. See the example in [`examples/profile.example.md`](../examples/profile.example.md). The `profile` stage turns it into a structured audience model, and every explanation is written against it. Concepts the listener already knows are not re-explained. Concepts they are missing get a short definition. Analogies come from fields the listener knows.

## Paper sources, figures and the source viewer

- **arXiv LaTeX source.** When the paper is on arXiv (from the link, the "arXiv:" stamp in the PDF, or an exact title match), PaperMap reads its LaTeX source instead of the PDF text. That gives exact section structure, equations, tables and the original figure files. If there is no source, or it cannot be parsed, the PDF is used. Turn this off with `--no-latex` or `use_latex = false`.
- **Tables** are read as rows and columns (from LaTeX, or from ruled tables in the PDF) and given to the model as tables.
- **Paper figures** appear next to the generated diagrams: sections that discuss a figure get a "Figure N" toggle on the stage.
- **Source viewer.** The **Paper** tab shows the PDF page behind each beat, with the quoted passage highlighted. "View in paper" on a quote opens it.

## Reading, asking and the quiz

The page has three modes:

- **Read**: the guided walkthrough, in the high-level or deep-dive view, plus the knowledge graph.
- **Ask**: open at any time. Ask while reading (the Ask tab next to the narration follows what is on screen) or in the full Ask view. Answers are grounded in the paper and link back to sections. This needs `papermap serve`.
- **Quiz**: unlocks once every section has been read through to its last beat. It has at least 10 multiple-choice questions (`quiz_questions`), spread across the sections, written for the profile and chosen so that no two test the same idea. After the last question you get a score and a per-section list of what to review. Each missed question shows your answer, the correct one, an explanation and the supporting sentence from the paper. It also has a button that jumps to the narration beat covering it, and one that opens the PDF page with that sentence highlighted. Results and the best score stay in the browser, and retakes reshuffle the options. Turn the quiz off with `--no-quiz` or `quiz = false`.

## Reader memory

PaperMap remembers what it learns about a reader, on your computer, and uses it to explain the next paper better.

- **What it records.** While a page is open through `papermap serve`, it records your quiz answers (which concepts you got right or wrong), the questions you ask, the papers you finish, and your answer to "How was the level?" at the end.
- **How it refines.** Before each run the record is summarized in code. Concepts you got right in recent quizzes count as known. Concepts you missed or keep asking about count as weak spots. Recent evidence weighs more than old, so an old mistake is forgiven after recent right answers. Recent level ratings move the depth up or down a notch. The summary is added to your profile. The narration skips what you know, spends time on your weak spots and connects to papers you have read, and answers to your questions get the same treatment.
- **Where it lives.** It is one plain JSON-lines file per reader, which you can read or edit: `%LOCALAPPDATA%\papermap\readers\<reader>\events.jsonl` on Windows, `~/.local/share/papermap/readers/<reader>/` elsewhere. Override the location with `PAPERMAP_MEMORY_DIR`. It is never written into a generated page, so sharing a page shares none of it.
- **Which reader.** By default the reader is named after the profile file (`--profile profile.md` gives the reader `profile`). Choose another name with `--reader`.

```bash
papermap memory                                # list readers
papermap memory profile                        # what PaperMap has learned about this reader
papermap memory profile --forget               # delete it
papermap paper.pdf -p profile.md --no-memory   # ignore it for one run
papermap serve <folder> --no-memory            # record nothing this time
```

## Models

All model access goes through one small provider interface (`papermap/llm/base.py`), so you can switch backends without touching the pipeline.

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

Third-party backends can register themselves with `papermap.llm.register_provider(name, factory)` or through the `papermap.providers` entry-point group.

## Narration

| `[tts] provider` | What happens |
|---|---|
| `browser` (default) | The page speaks with the browser's speech engine. No files, no setup. A voice picker is in the page settings. |
| `edge` | Microsoft Edge neural voices, saved as MP3 per beat (needs `pip install "papermap[edge-tts]"`). |
| `openai` | Any OpenAI-compatible `/audio/speech` server: OpenAI, or a local server such as Kokoro-FastAPI (set `base_url`, `model` and `voices`). |
| `none` | Silent. Captions advance at reading speed. |

Set `narration_style = "duo"` for a host-and-expert conversation instead of a single narrator.

## Configuration

Every key is optional. `papermap init` writes a commented template. Configuration is resolved from `--config`, then `./papermap.toml`, then `~/.config/papermap/config.toml`, and finally built-in defaults. API keys are never stored; providers read them from the environment variable named by `api_key_env`.

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
use_latex = true             # read the arXiv LaTeX source when available
paper_figures = true         # show the paper's own figures
review = true                # fact-check the narration before diagrams and audio
quiz = true                  # end-of-paper comprehension quiz
quiz_questions = 10          # how many questions to keep (at least 10)
```

## Output

```
papermap-out/<paper>-<profile>/
  index.html            the experience: data, CSS and JS inlined; opens offline
  audio/                per-beat narration (edge / openai TTS only)
  experience.json       the full artifact the page renders (schema: papermap/models.py)
  qa_index.json         retrieval chunks + section notes for Q&A
  papermap.config.json  the configuration used (no secrets); reused by `serve`
  debug/<stage>.json    every stage's output, for inspection
  logs/run.log          full DEBUG log of the run
```

The page supports deep links (`index.html#view=deep&s=3&b=1`, `#mode=ask`, `#mode=quiz`), keyboard shortcuts (press `?`), light and dark themes, and phone-sized screens.

## How it works

```
Paper ─► parse ─► understand ─► profile ─► explain ─► review ─► diagrams ─► graph ─► quiz ─► narrate ─► render ─► webpage
          PDF or   chapters,     audience   beats per   fact-     typed       knowledge  checked  audio      experience.json
          LaTeX    key points,   model      section     check     specs +     graph      multiple clips      + index.html
                   verified                 and view    and fix   per-beat               choice
                   quotes                                         focus
```

Each stage is a pure function from its dependencies' outputs to a validated pydantic model. Stage outputs and individual LLM calls are cached by content hash. Re-running the same command is instant. If a run fails partway, re-running resumes from the last completed model call. Changing only the profile reuses parsing, understanding and the graph. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the components, interfaces and extension points.

### Grounding

These checks run in code and do not rely on the model following instructions:

- **Quotes** shown in the page must appear (near-)verbatim in the extracted paper text. Unmatched quotes are dropped.
- **Numbers** in narration, bar charts and tables must appear in the section text. Otherwise the model is asked to fix its output, and an unfixable diagram is skipped.
- **Graph entities** must be named in the paper. Citation markers are stripped and aliases such as acronyms are merged.
- **Q&A** answers are built only from retrieved excerpts, section notes and graph facts, and they must cite sections.

- **Attribution:** a number in a chart or table must sit next to its label in the paper (same table row or column, or nearby in the text). A correct number under the wrong method is rejected.
- **Fact-checking pass:** after the narration is written, a second model pass checks each section against the paper for unsupported claims, misattributed numbers and repetition, and applies fixes (a fix that introduces a number not in the paper is ignored). Skip it with `--no-review` or `review = false`.
- **Quiz answer keys:** a separate pass answers every question from the section text alone, with the options shuffled. If its answer differs from the key, the question is dropped. Numbers in correct answers and explanations must appear in the paper, and the supporting quote must be verbatim.

Grounding reduces errors but cannot eliminate them. A model can still misread a correct passage. Equations are reconstructed from PDF text extraction, which can be imperfect.

## Debugging

```bash
papermap paper.pdf -p profile.md -v            # debug logging (also always in logs/run.log)
papermap paper.pdf -p profile.md --until understand
papermap paper.pdf -p profile.md --force diagrams   # recompute one stage (cached LLM calls are reused)
papermap paper.pdf -p profile.md --no-cache         # completely fresh run
papermap paper.pdf -p profile.md --no-latex         # ignore the arXiv LaTeX source, use the PDF text
papermap paper.pdf -p profile.md --no-review        # skip the fact-checking pass
papermap paper.pdf -p profile.md --no-quiz          # skip the comprehension quiz
papermap stages                                     # list stages and dependencies
papermap render papermap-out/<folder>               # rebuild a page with the current design (no model calls)
```

The cache lives in `%LOCALAPPDATA%\papermap\cache` on Windows and in `~/.cache/papermap` elsewhere. Override it with `PAPERMAP_CACHE_DIR` or `--cache-dir`. Every model request and response is stored there as JSON.

## Development

```bash
pip install -e ".[dev,anthropic]"
pytest
```

The tests build a PDF, run the full pipeline with the `mock` provider, and check the artifact, verbatim quotes, caching, reproducibility, invalidation, Q&A and the quiz's answer-key checks.

## Limitations and roadmap

- Input is a PDF or an arXiv link. PDFs must contain text (scanned PDFs need OCR first). HTML papers are planned.
- Q&A answers are not streamed yet.
- There are no production deployment assumptions. The server is local and single-user, with no authentication.
