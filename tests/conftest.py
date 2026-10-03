from __future__ import annotations

from pathlib import Path

import pytest

BODY = {
    "Abstract": (
        "We present Sparse Mixture Routing, a method for routing tokens to experts. "
        "Our model achieves 41.2 accuracy on the GLUE benchmark, improving over the dense baseline by 2.3 points. "
        "Training is 3.5 times faster than standard Transformers."
    ),
    "1 Introduction": (
        "Large language models are expensive to train. Dense Transformers activate every parameter for every token. "
        "Mixture of Experts models route each token to a small subset of experts. "
        "We build on the Switch Transformer and compare against GShard. "
        "Our contribution is a routing function that balances load without auxiliary losses."
    ),
    "2 Method": (
        "Each token representation x is scored by a router W_r. The router selects the top expert. "
        "The output is y = E(x) weighted by the gate value g. "
        "We use a capacity factor of 1.25 and drop overflowing tokens. "
        "Load balancing emerges from a temperature schedule on the router logits."
    ),
    "3 Experiments": (
        "We evaluate on GLUE and SuperGLUE. Sparse Mixture Routing reaches 41.2 accuracy on GLUE. "
        "The dense baseline reaches 38.9 accuracy. The Switch Transformer reaches 40.1 accuracy. "
        "GShard reaches 39.7 accuracy. Training throughput improves 3.5 times on 64 accelerators."
    ),
    "4 Conclusion": (
        "Sparse Mixture Routing makes expert models simpler to train. "
        "Limitations include sensitivity to the temperature schedule. Future work will study larger expert counts."
    ),
}

REFERENCES = [
    "[1] W. Fedus, B. Zoph, and N. Shazeer. Switch Transformers: scaling to trillion parameter models. JMLR, 2022.",
    "[2] D. Lepikhin et al. GShard: scaling giant models with conditional computation. ICLR, 2021.",
    "[3] A. Wang et al. GLUE: a multi-task benchmark for natural language understanding. ICLR, 2019.",
    "[4] A. Vaswani et al. Attention is all you need. NeurIPS, 2017.",
    "[5] N. Shazeer et al. Outrageously large neural networks. ICLR, 2017.",
]


def make_pdf(path: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    y = 72

    def need(height: float) -> None:
        nonlocal page, y
        if y + height > 760:
            page = doc.new_page()
            y = 72

    page.insert_text((72, y), "Sparse Mixture Routing for Efficient Experts", fontsize=18, fontname="hebo")
    y += 28
    page.insert_text((72, y), "Ada Lovelace, Alan Turing", fontsize=11, fontname="helv")
    y += 36
    for heading, text in BODY.items():
        need(120)
        page.insert_text((72, y), heading, fontsize=12.5, fontname="hebo")
        y += 20
        rect = pymupdf.Rect(72, y, 520, y + 110)
        page.insert_textbox(rect, text, fontsize=10, fontname="helv")
        y += 120
    need(140)
    page.insert_text((72, y), "References", fontsize=12.5, fontname="hebo")
    y += 20
    for ref in REFERENCES:
        need(44)
        rect = pymupdf.Rect(72, y, 520, y + 40)  # insert_textbox silently skips text that does not fit
        assert page.insert_textbox(rect, ref, fontsize=10, fontname="helv") >= 0
        y += 42
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory) -> Path:
    return make_pdf(tmp_path_factory.mktemp("pdf") / "sample.pdf")


@pytest.fixture()
def profile_md(tmp_path) -> Path:
    p = tmp_path / "profile.md"
    p.write_text("- **Name:** Grace\n- Background: backend engineer, knows Python and basic ML.\n- Depth: balanced\n", "utf-8")
    return p
