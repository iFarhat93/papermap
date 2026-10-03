from papermap.grounding import find_quote, name_in_text, normalize, number_in_text, lower_text

SOURCE = (
    "We propose a new simple network architecture, the Transformer, based solely on attention mecha-\n"
    "nisms, dispensing with recurrence and convolutions entirely. Our model achieves 28.4 BLEU."
)


def test_normalize_dehyphenates_and_fixes_quotes():
    assert normalize("atten-\ntion “quoted” ﬁne") == 'attention "quoted" fine'


def test_exact_quote_is_found_across_line_breaks():
    q = "based solely on attention mechanisms, dispensing with recurrence and convolutions entirely."
    assert find_quote(q, SOURCE) is not None


def test_near_quote_with_extraction_noise_is_accepted():
    q = "based solely on attention mechanisms, dispensing with recurence and convolutions entirely"  # typo
    assert find_quote(q, SOURCE) is not None


def test_paraphrase_is_rejected():
    q = "The Transformer only uses attention and gets rid of recurrent layers and convolutional layers."
    assert find_quote(q, SOURCE) is None


def test_short_quotes_are_rejected():
    assert find_quote("Transformer", SOURCE) is None


def test_number_forms():
    assert number_in_text(28.4, SOURCE)
    assert number_in_text("28.40", SOURCE)
    assert not number_in_text(27.3, SOURCE)
    assert number_in_text(0.853, "accuracy of 85.3% on the test set")  # fraction <-> percent
    assert not number_in_text(8, "version 28 of the model")  # no partial-number matches
    assert number_in_text(None, "anything")


def test_scientific_notation_survives_pdf_flattening():
    text = "Training cost (FLOPs) 3.3 · 1018 for the base model and 2.3 · 1019 for the big model; epsilon 10−9."
    assert number_in_text(3.3e18, text)
    assert number_in_text("2.3e+19", text)
    assert number_in_text(1e-9, text)
    assert not number_in_text(4.1e18, text)


def test_name_in_text_handles_acronyms():
    low = lower_text("We fine-tune with LoRA and compare against adapters.")
    assert name_in_text("Low-Rank Adaptation (LoRA)", low)
    assert name_in_text("adapters", low)
    assert not name_in_text("prefix tuning", low)
