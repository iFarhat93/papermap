"""Reader memory: consolidation over time, use in the profile, privacy, recording and the CLI."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from papermap.cache import Cache
from papermap.cli import main
from papermap.config import load_config
from papermap.memory import MemoryStore, Recorder, apply_memory, consolidate
from papermap.models import AudienceProfile, Experience
from papermap.pipeline import run_pipeline
from papermap.server import make_server
from papermap.stages.base import RunContext


def _quiz(paper, title, results):
    return {"kind": "quiz", "paper": paper, "title": title,
            "data": {"correct": sum(ok for _, ok in results), "total": len(results),
                     "answers": [{"concepts": [c], "correct": ok} for c, ok in results]}}


EVENTS = [  # three papers, oldest first
    _quiz("p1", "Paper One", [("Self-attention", True), ("Positional encoding", False)]),
    _quiz("p2", "Paper Two", [("Positional encoding", True), ("Multi-head attention", True)]),
    _quiz("p3", "Paper Three", [("Positional encoding", True), ("Multi-head attention", True),
                                ("Beam search", False), ("Self-attention", False)]),
    {"kind": "level", "paper": "p3", "data": {"level": "too_basic"}},
]


def test_consolidation_trusts_recent_evidence():
    m = consolidate(EVENTS, "grace")
    assert "Multi-head attention" in m.mastered  # right twice, recently
    assert "Beam search" in m.struggles  # missed in the latest paper
    assert "Self-attention" not in m.mastered  # an old success does not outweigh a recent miss
    assert "Positional encoding" not in m.struggles  # an old miss is forgiven after two recent successes
    assert m.history == ["Paper One (quiz 1/2)", "Paper Two (quiz 2/2)", "Paper Three (quiz 2/4)"]
    assert m.level == 1.0 and m.papers == 3
    assert consolidate(EVENTS + [{"kind": "ask", "paper": "p3", "data": {"question": "?"}}]).digest() == m.digest()


def test_memory_refines_the_profile_and_stays_out_of_pages():
    base = AudienceProfile(name="Grace", known_concepts=["Beam search", "PyTorch"], depth="balanced", expertise_level="advanced")
    p = apply_memory(base, consolidate(EVENTS))
    assert p.known_concepts == ["PyTorch"]  # quiz evidence beats self-report
    assert "Multi-head attention" in p.mastered and p.struggles == ["Beam search"]
    assert (p.depth, p.expertise_level) == ("deep", "expert") and "too basic" in p.level_note
    brief = p.brief()
    for line in ("Has shown they understand", "Has struggled with before", "Papers already read", "Level feedback"):
        assert line in brief
    public = p.public()
    assert not (public.mastered or public.struggles or public.history or public.level_note)
    assert "mastered" not in AudienceProfile.model_json_schema()["properties"]  # the model is never asked for it


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    d = tmp_path / "readers"
    monkeypatch.setenv("PAPERMAP_MEMORY_DIR", str(d))
    return d


def _run(tmp_path, pdf, memory):
    config, _ = load_config(None, {"llm": {"provider": "mock", "model": "mock"}, "pipeline": {"concurrency": 2, "use_latex": False}})
    ctx = RunContext(config=config, cache=Cache(tmp_path / "cache"), out_dir=tmp_path / "out", source=str(pdf),
                     profile_text="Name: Grace", reader="grace", memory=memory)
    return run_pipeline(ctx)


def test_a_new_run_uses_the_memory(tmp_path, sample_pdf, memory_dir):
    store = MemoryStore()
    first = _run(tmp_path, sample_pdf, consolidate(store.events("grace")))
    exp = first.experience
    assert exp.reader == "grace"
    tagged = [q for q in exp.quiz.questions if q.concepts]
    assert tagged, "quiz questions are tagged with the concepts they test"
    rec = Recorder(store, "grace", exp)
    rec.record("quiz", {"answers": {q.id: (q.answer + 1) % 4 for q in tagged}})  # every tagged question missed
    second = _run(tmp_path, sample_pdf, consolidate(store.events("grace")))
    assert "profile" not in second.cached and "explain" not in second.cached  # the explanation is redone
    profile = second.outputs["profile"]
    assert profile.struggles and set(profile.struggles) <= {c for q in tagged for c in q.concepts}
    page = json.loads((tmp_path / "out" / "experience.json").read_text("utf-8"))
    assert page["profile"]["struggles"] == [] and page["profile"]["mastered"] == []  # stays on this computer


def test_server_records_what_the_reader_does(tmp_path, sample_pdf, memory_dir):
    exp = _run(tmp_path, sample_pdf, None).experience
    store = MemoryStore()
    server, url = make_server(tmp_path / "out", None, port=8890, recorder=Recorder(store, "grace", exp))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def post(payload):
        req = urllib.request.Request(url + "api/memory", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        with urllib.request.urlopen(url + "api/health", timeout=5) as r:
            assert json.load(r)["memory"] is True
        answers = {q.id: q.answer for q in exp.quiz.questions}  # all right
        assert post({"kind": "quiz", "data": {"answers": answers}}) == 200
        assert post({"kind": "finished"}) == 200
        assert post({"kind": "level", "data": {"level": "just_right"}}) == 200
        assert post({"kind": "level", "data": {"level": "perfect"}}) == 400
        assert post({"kind": "unknown"}) == 400
    finally:
        server.shutdown()
        server.server_close()
    events = store.events("grace")
    assert [e["kind"] for e in events] == ["quiz", "finished", "level"]
    assert events[0]["data"]["correct"] == len(exp.quiz.questions)  # checked against the answer key
    assert consolidate(events).history[0].endswith(f"(quiz {len(answers)}/{len(answers)})")


def test_memory_command(tmp_path, memory_dir, capsys):
    store = MemoryStore()
    for e in EVENTS:
        store.append("grace", e)
    assert main(["memory"]) == 0
    assert "grace" in capsys.readouterr().out
    assert main(["memory", "grace"]) == 0
    out = capsys.readouterr().out
    assert "Multi-head attention" in out and "Beam search" in out and "too basic" in out
    assert main(["memory", "grace", "--forget"]) == 0
    assert store.events("grace") == [] and "forgot grace" in capsys.readouterr().out
