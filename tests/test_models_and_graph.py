from paper2podcast.models import Diagram, choose
from paper2podcast.retrieval import BM25
from paper2podcast.stages.graph import EntityOut, GraphOut, RelationOut, clean_label, merge_graph


def test_choose_maps_free_labels_to_vocabulary():
    assert choose("Methods", ("method", "results"), "other") == "method"
    assert choose("related work", ("related_work",), "other") == "related_work"
    assert choose(None, ("a",), "z") == "z"
    assert choose("banana", ("method",), "other") == "other"


def test_flow_diagram_problems():
    d = Diagram(id="d", type="flow", title="t", nodes=[{"id": "a", "label": "A"}, {"id": "b", "label": "B", "group": "null"}],
                edges=[{"source": "a", "target": "c"}])
    probs = d.problems()
    assert any("unknown node id" in p for p in probs)
    assert d.nodes[1].group is None  # "null" strings are normalized away


def test_bar_diagram_problems_and_elements():
    d = Diagram(id="d", type="bar", title="t", categories=["x", "y"], series=[{"name": "acc", "values": [1.0]}])
    assert any("values" in p for p in d.problems())
    ok = Diagram(id="d", type="bar", title="t", categories=["x", "y"], series=[{"name": "acc", "values": [1.0, None]}])
    assert ok.problems() == [] and "x" in ok.element_ids()


def test_clean_label_drops_citations():
    assert clean_label("ConvS2S [9]") == "ConvS2S"
    assert clean_label("Luong et al. (2015)") == "Luong et al."


def test_merge_graph_resolves_aliases_and_grounds():
    text = "We compare LoRA with adapter layers [12] and prefix tuning on GLUE. Low-Rank Adaptation (LoRA) is efficient."
    g1 = GraphOut(
        entities=[
            EntityOut(name="Low-Rank Adaptation (LoRA)", type="method", evidence="Low-Rank Adaptation (LoRA) is efficient"),
            EntityOut(name="adapter layers [12]", type="prior_work"),
            EntityOut(name="GLUE", type="dataset"),
            EntityOut(name="Quantum Teleportation", type="concept"),  # not in the text -> dropped
        ],
        relations=[
            RelationOut(source="LoRA", target="adapter layers", relation="improves on"),
            RelationOut(source="LoRA", target="GLUE", relation="evaluated_on"),
            RelationOut(source="LoRA", target="Quantum Teleportation", relation="uses"),
        ],
    )
    kg = merge_graph([("s1", g1, text)], "LoRA", text, [("s1", "prefix tuning", "compared against")], max_nodes=10)
    labels = {n.label for n in kg.nodes}
    assert kg.center == "this-work"
    assert "Quantum Teleportation" not in labels
    assert "adapter layers" in labels
    rels = {(e.source, e.target, e.relation) for e in kg.edges}
    assert ("this-work", "adapter-layers", "improves_on") in rels
    assert any(e.relation == "compares_to" for e in kg.edges)  # from the understand-stage prior work
    center = next(n for n in kg.nodes if n.id == "this-work")
    assert center.type == "this_work" and center.evidence


def test_bm25_ranks_relevant_chunk_first():
    idx = BM25(["the router selects experts", "results on glue benchmark accuracy", "related work on attention"])
    assert idx.top("glue accuracy", k=1)[0][0] == 1
