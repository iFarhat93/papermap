# PaperMap

Turn a scientific paper into an interactive, narrated walkthrough written for one specific reader.

![PaperMap demo: "Knowledge Insulating Vision-Language-Action Models" explained for an AI and robotics researcher](docs/images/demo.gif)

You give it a paper (a PDF or an arXiv link) and a short `profile.md` describing the reader. It builds one self-contained web page with:

- **High-level and deep-dive views.** Section-by-section narration with diagrams that highlight what is being explained.
- **A knowledge graph.** How the paper connects to prior work, models, datasets and concepts.
- **Ask any time.** Questions while you read, answered from the paper with links back to the sections they come from.
- **A quiz at the end.** It unlocks once you have read every section. It has 10 or more multiple-choice questions, each with an answer key checked against the paper. You get a score, and each question you missed points to the part of the narration and the passage in the paper to revisit.
- **Stays close to the paper.** Uses the arXiv LaTeX source when available (exact equations, tables, figures), shows the paper's own figures, and opens the source page with the quoted passage highlighted. Numbers are checked against the paper, and a fact-checking pass reviews the narration.

It runs with local models (Ollama, vLLM, llama.cpp, LM Studio) or with cloud APIs.

## Install

Python 3.11 or newer.

```bash
git clone https://github.com/iFarhat93/papermap.git
cd papermap
pip install -e ".[all]"
```

The command-line tool is called `papermap`.

## Run

```bash
# 1. write a profile.md describing the reader (see examples/)
papermap init

# 2. generate (here with a local model through Ollama; any PDF path works too)
papermap https://arxiv.org/abs/1706.03762 --profile profile.md --provider ollama --model qwen2.5:7b

# 3. open the result (the local server answers your questions)
papermap serve papermap-out/1706.03762-profile
```

Optional flags: `--tts edge` adds real voice audio, `--style duo` switches to a host-and-expert conversation, and `--no-quiz` skips the quiz. Re-running is instant because every step is cached.

Other providers (vLLM, llama.cpp, LM Studio, OpenAI-compatible and Anthropic APIs), voices, configuration and debugging are covered in [docs/GUIDE.md](docs/GUIDE.md). How it works is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

MIT
