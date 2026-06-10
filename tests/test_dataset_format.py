"""L-dataset-format: dataset normalization to a single chat structure.

Feeds messages / {text} / {prompt,completion} / alpaca {instruction,input,output}
shapes to the normalizer and asserts each produces the expected messages list and
rendered training ``text``. Pure-offline (no torch / no GPU): a tiny fake
tokenizer stands in for ``apply_chat_template``.
"""

import pytest

import dataset_format as df


class FakeTokenizer:
    """Minimal stand-in: renders messages as ``role: content`` lines."""

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
        return "\n".join(f"{m['role']}: {m['content']}" for m in messages)


# ── detect_format ─────────────────────────────────────────────────────────────

def test_detect_messages():
    assert df.detect_format(["messages"]) == "messages"


def test_detect_alpaca():
    assert df.detect_format(["instruction", "input", "output"]) == "alpaca"


def test_detect_prompt_completion():
    assert df.detect_format(["prompt", "completion"]) == "prompt_completion"


def test_detect_text():
    assert df.detect_format(["text"]) == "text"


def test_detect_priority_messages_over_text():
    # A dataset with both messages and a stray text column trains on messages.
    assert df.detect_format(["messages", "text"]) == "messages"


def test_detect_priority_alpaca_over_text():
    assert df.detect_format(["instruction", "output", "text"]) == "alpaca"


def test_detect_unsupported_raises():
    with pytest.raises(ValueError):
        df.detect_format(["foo", "bar"])


# ── normalize_to_messages ─────────────────────────────────────────────────────

def test_messages_passthrough_strips_extra_keys():
    ex = {"messages": [{"role": "user", "content": "hi", "weight": 1.0}]}
    out = df.normalize_to_messages(ex)
    assert out == [{"role": "user", "content": "hi"}]


def test_messages_json_string_elements():
    ex = {"messages": ['{"role": "user", "content": "hi"}']}
    out = df.normalize_to_messages(ex)
    assert out == [{"role": "user", "content": "hi"}]


def test_alpaca_with_input():
    ex = {"instruction": "Summarize", "input": "The cat sat.", "output": "Cat sat."}
    out = df.normalize_to_messages(ex)
    assert out == [
        {"role": "user", "content": "Summarize\n\nThe cat sat."},
        {"role": "assistant", "content": "Cat sat."},
    ]


def test_alpaca_empty_input():
    ex = {"instruction": "Say hi", "input": "", "output": "hi"}
    out = df.normalize_to_messages(ex)
    assert out == [
        {"role": "user", "content": "Say hi"},
        {"role": "assistant", "content": "hi"},
    ]


def test_alpaca_missing_output_raises():
    with pytest.raises(ValueError):
        df.normalize_to_messages({"instruction": "x", "input": "", "output": None}, fmt="alpaca")


def test_prompt_completion():
    ex = {"prompt": "Q?", "completion": "A."}
    out = df.normalize_to_messages(ex)
    assert out == [
        {"role": "user", "content": "Q?"},
        {"role": "assistant", "content": "A."},
    ]


def test_text_uses_raw_role():
    ex = {"text": "raw pretraining text"}
    out = df.normalize_to_messages(ex)
    assert out == [{"role": df.RAW_TEXT_ROLE, "content": "raw pretraining text"}]


# ── messages_to_text (rendering) ──────────────────────────────────────────────

def test_messages_to_text_uses_template():
    tok = FakeTokenizer()
    msgs = [{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"}]
    assert df.messages_to_text(msgs, tok) == "user: Q\nassistant: A"


def test_messages_to_text_raw_is_verbatim():
    tok = FakeTokenizer()
    msgs = [{"role": df.RAW_TEXT_ROLE, "content": "verbatim text"}]
    # No template applied — returned exactly as supplied.
    assert df.messages_to_text(msgs, tok) == "verbatim text"


# ── end-to-end: every shape produces the same downstream schema ───────────────

@pytest.mark.parametrize(
    "row",
    [
        {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]},
        {"prompt": "hi", "completion": "yo"},
        {"instruction": "hi", "input": "", "output": "yo"},
        {"text": "hi yo"},
    ],
)
def test_normalize_dataset_uniform_schema(row):
    out = df.normalize_dataset([row])
    assert len(out) == 1
    assert set(out[0].keys()) == {"messages"}
    for m in out[0]["messages"]:
        assert set(m.keys()) == {"role", "content"}


def test_normalize_dataset_renders_each_shape():
    tok = FakeTokenizer()
    pc = df.normalize_dataset([{"prompt": "Q", "completion": "A"}])
    assert df.messages_to_text(pc[0]["messages"], tok) == "user: Q\nassistant: A"
    alpaca = df.normalize_dataset([{"instruction": "Do", "input": "X", "output": "Y"}])
    assert df.messages_to_text(alpaca[0]["messages"], tok) == "user: Do\n\nX\nassistant: Y"


def test_normalize_dataset_heterogeneous_source_fails_loud():
    """A source whose rows disagree on shape must fail rather than train on a mix.
    The format is locked to the first row, so a later mismatching row raises."""
    rows = [
        {"prompt": "Q", "completion": "A"},
        {"instruction": "Do", "input": "X", "output": "Y"},
    ]
    with pytest.raises(ValueError):
        df.normalize_dataset(rows)
