"""LaTeX ingestion, arXiv helpers, attribution checks and the review stage."""

from __future__ import annotations

import io
import tarfile

import pytest

from papermap.cache import Cache
from papermap.config import LLMSettings, load_config
from papermap.grounding import number_near_label
from papermap.llm import register_provider
from papermap.llm.base import LLMProvider, LLMResponse
from papermap.models import (
    AudienceProfile, Beat, Explanations, PaperOverview, ParsedPaper, RawSection, SectionUnderstanding,
    SectionView, Table, Understanding, View,
)
from papermap.sources import arxiv
from papermap.sources.latex import parse_latex
from papermap.stages import review
from papermap.stages.base import RunContext

MAIN_TEX = r"""
\documentclass{article}
\newcommand{\method}{SparseRoute}
\title{Sparse Routing for \method{} Experts}
\begin{document}
\maketitle
\begin{abstract}
We present \method, a router. It reaches 41.2\% accuracy. % a comment that must vanish
\end{abstract}
\section{Introduction}\label{sec:intro}
Dense models are slow~\citep{smith2020}. As shown by \citet{lee2021}, routing helps.
See Figure~\ref{fig:arch}, Table~\ref{tab:main} and Eq.~\eqref{eq:gate}.
\begin{figure}
  \centering
  \includegraphics[width=\linewidth]{figs/arch}
  \caption{The \textbf{router} architecture.}
  \label{fig:arch}
\end{figure}
\input{sections/method}
\appendix
\section{Extra details}
More text with $x^2$ inline math.
\end{document}
"""

METHOD_TEX = r"""
\section{Method}\label{sec:method}
The gate is
\begin{equation}
  g(x) = \mathrm{softmax}(W_r x) \label{eq:gate}
\end{equation}
\subsection{Results}
\begin{table}
\caption{Accuracy on GLUE.}\label{tab:main}
\begin{tabular}{lcc}
\toprule
Method & \multicolumn{2}{c}{Accuracy} \\
\midrule
Dense \cite{smith2020} & 38.9 & 39.1 \\
\method & \textbf{41.2} & 41.5 \\
\bottomrule
\end{tabular}
\end{table}
\begin{figure}
\begin{subfigure}{0.5\linewidth}\includegraphics{figs/arch.png}\caption{Left}\end{subfigure}
\begin{subfigure}{0.5\linewidth}\includegraphics{figs/arch.png}\caption{Right}\end{subfigure}
\caption{Two panels.}\label{fig:panels}
\end{figure}
"""

BBL = r"""
\begin{thebibliography}{2}
\bibitem[{Smith et~al.(2020)Smith, Doe}]{smith2020}
Jane Smith and John Doe.
\newblock Dense models.
\newblock In \emph{ICML}, 2020.
\bibitem[{Lee(2021)}]{lee2021}
Ann Lee.
\newblock Routing.
\newblock 2021.
\end{thebibliography}
"""


@pytest.fixture()
def latex_tree(tmp_path):
    import pymupdf

    root = tmp_path / "src"
    (root / "sections").mkdir(parents=True)
    (root / "figs").mkdir()
    (root / "main.tex").write_text(MAIN_TEX, "utf-8")
    (root / "sections" / "method.tex").write_text(METHOD_TEX, "utf-8")
    (root / "main.bbl").write_text(BBL, "utf-8")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), False)
    pix.clear_with(200)
    pix.save(str(root / "figs" / "arch.png"))
    return root


def test_latex_structure(latex_tree):
    lp = parse_latex(latex_tree)
    assert lp.title == "Sparse Routing for SparseRoute Experts"
    assert "41.2% accuracy" in lp.abstract and "comment" not in lp.abstract
    assert [s.heading for s in lp.sections] == ["1 Introduction", "2 Method", "2.1 Results", "A Extra details"]
    intro = lp.sections[0].text
    assert "[Smith et al., 2020]" in intro and "Lee (2021)" in intro
    assert "Figure 1" in intro and "Table 1" in intro and "Eq. (1)" in intro
    assert "[Figure 1: The router architecture.]" in intro
    method = lp.sections[1].text
    assert "$$ g(x) = \\mathrm{softmax}(W_r x) $$" in method
    assert "$x^2$" in lp.sections[3].text
    assert len(lp.references) == 2 and lp.references[0].startswith("Jane Smith")


def test_latex_tables_and_figures(latex_tree):
    lp = parse_latex(latex_tree)
    (t,) = lp.tables
    assert t.caption == "Accuracy on GLUE." and t.columns == ["Method", "Accuracy", ""]
    assert t.rows == [["Dense", "38.9", "39.1"], ["SparseRoute", "41.2", "41.5"]]
    assert "| SparseRoute | 41.2 | 41.5 |" in lp.sections[2].text
    f1, f2 = lp.figures
    assert f1.number == 1 and f1.files and f1.files[0].name == "arch.png"
    assert f2.caption == "Two panels." and f2.sub_captions == ["Left", "Right"] and len(f2.files) == 2


def test_arxiv_ids_and_titles():
    assert arxiv.id_from_source("https://arxiv.org/abs/2505.23705v1") == "2505.23705v1"
    assert arxiv.id_from_source("arXiv:2505.23705") == "2505.23705"
    assert arxiv.id_from_source("https://www.pi.website/download/pi05_KI.pdf") is None
    assert arxiv.id_from_text("... arXiv:2106.09685v2 [cs.CL] 16 Oct 2021") == "2106.09685v2"
    assert arxiv.titles_match("Knowledge Insulating VLA Models", "knowledge insulating VLA models.")
    assert not arxiv.titles_match("Knowledge Insulating VLA Models", "Attention Is All You Need")


def test_safe_extract_blocks_path_traversal(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("main.tex", b"\\documentclass{article}"), ("../evil.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    dest = tmp_path / "out"
    assert arxiv._safe_extract(buf.getvalue(), dest)
    assert (dest / "main.tex").is_file() and not (tmp_path / "evil.txt").exists()


def test_attribution():
    t = Table(id="tab1", columns=["", "Spatial", "Long"], rows=[["Baku", "–", "86.0"], ["$\\pi_0$", "96.8", "85.2"]])
    assert number_near_label(96.8, "π0", "", [t])
    assert number_near_label(96.8, "Spatial", "", [t])
    assert not number_near_label(86.0, "π0", "", [t])
    text = "Our big model reaches 41.0 BLEU. Separately, the best ensemble scored 40.4."
    assert number_near_label(41.0, "big model", text)
    assert not number_near_label(41.0, "GNMT + RL Ensemble", text)


class _Fixer(LLMProvider):
    name = "fixer"

    def complete(self, request):
        return LLMResponse(text=(
            '{"issues": [{"beat": "b1", "problem": "wrong number", "fix": "use 41.2"},'
            ' {"beat": "b2", "problem": "x", "fix": "y"}],'
            ' "beats": [{"id": "b1", "narration": "It reaches 41.2 accuracy on GLUE.", "subtitle": "41.2 on GLUE"},'
            ' {"id": "b2", "narration": "It reaches 99.9 accuracy.", "subtitle": "made up"}]}'
        ))


def test_review_applies_grounded_fixes_only(tmp_path):
    register_provider("fixer", lambda s: _Fixer(s))
    config, _ = load_config(None, {"llm": {"provider": "fixer", "model": "f"}, "pipeline": {"concurrency": 1}})
    ctx = RunContext(config=config, cache=Cache(tmp_path), out_dir=tmp_path / "o", source="x", profile_text="")
    paper = ParsedPaper(source="x", sha256="0", title="T", first_page_text="", n_pages=1,
                        sections=[RawSection(id="r1", heading="1 Results", text="Our router reaches 41.2 accuracy on GLUE.", page_start=1, page_end=1)])
    und = Understanding(overview=PaperOverview(title="T", one_line="o", problem="p", contribution="c"),
                        sections=[SectionUnderstanding(id="s1", title="Results", raw_section_ids=["r1"], summary="s")])
    beats = [Beat(id="deep.s1.b1", narration="It reaches 42 accuracy.", subtitle="42"),
             Beat(id="deep.s1.b2", narration="It is fast.", subtitle="fast")]
    exp = Explanations(views={"deep": View(kind="deep", sections=[SectionView(section_id="s1", title="Results", beats=beats)])})
    out = review._run(ctx, {"parse": paper, "understand": und, "profile": AudienceProfile(), "explain": exp})
    b1, b2 = out.views["deep"].sections[0].beats
    assert b1.narration == "It reaches 41.2 accuracy on GLUE."  # grounded fix applied
    assert b2.narration == "It is fast."  # fix with an invented number rejected
    assert [i.applied for i in out.review_issues] == [True, False]
    assert exp.views["deep"].sections[0].beats[0].narration == "It reaches 42 accuracy."  # input untouched
