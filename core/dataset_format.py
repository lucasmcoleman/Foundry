"""Dataset format normalization for Foundry training (audit L-dataset-format).

The training stage trains on a single chat ``messages`` structure. Datasets in
the wild come in several shapes; this module normalizes each supported shape to
the same ``[{"role", "content"}, ...]`` list so the rest of the pipeline only
ever sees one format.

Supported input shapes (auto-detected, in priority order):

  1. chat ``messages``         -> ``{"messages": [{"role", "content"}, ...]}``
  2. ``{"text"}``              -> single ``user`` turn carrying the raw text
  3. ``{"prompt", "completion"}`` -> ``user`` (prompt) + ``assistant`` (completion)
  4. alpaca ``{"instruction", "input", "output"}``
                                -> ``user`` (instruction[+input]) + ``assistant`` (output)

The functions here are deliberately dependency-free (stdlib + json only) so they
are importable and unit-testable without torch / transformers / a GPU. The
training entry module (``core/_train_entry.py``) imports ``normalize_dataset``
and feeds the result to the tokenizer's chat template.

A row whose ``text`` field was supplied directly (shape 2) is special: there is
no chat structure to apply a template to, so the normalizer marks it with the
sentinel role ``"_raw_text"``. ``messages_to_text`` returns that raw text
verbatim instead of calling ``apply_chat_template`` on it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

# Sentinel role used to carry a pre-rendered ``text`` field through the same
# ``messages`` pipeline without a chat template.
RAW_TEXT_ROLE = "_raw_text"

# Recognized column-name aliases per logical field.
_PROMPT_KEYS = ("prompt", "input_text", "question")
_COMPLETION_KEYS = ("completion", "response", "answer", "output_text")
_TEXT_KEYS = ("text", "content")
_INSTRUCTION_KEYS = ("instruction",)
_ALPACA_INPUT_KEYS = ("input",)
_ALPACA_OUTPUT_KEYS = ("output",)


def detect_format(columns: list[str]) -> str:
    """Return the detected dataset format for a set of column names.

    One of: ``"messages"``, ``"alpaca"``, ``"prompt_completion"``, ``"text"``.
    Raises ``ValueError`` if no supported format matches.

    Detection priority is messages > alpaca > prompt/completion > text so that a
    dataset carrying both ``messages`` and a stray ``text`` column trains on the
    structured chat data, not the flattened text.
    """
    cols = set(columns)
    if "messages" in cols:
        return "messages"
    if any(k in cols for k in _INSTRUCTION_KEYS) and any(k in cols for k in _ALPACA_OUTPUT_KEYS):
        return "alpaca"
    if any(k in cols for k in _PROMPT_KEYS) and any(k in cols for k in _COMPLETION_KEYS):
        return "prompt_completion"
    if any(k in cols for k in _TEXT_KEYS):
        return "text"
    raise ValueError(
        f"Unsupported dataset format. Columns {sorted(cols)} match none of: "
        "messages / {text} / {prompt,completion} / alpaca {instruction,input,output}"
    )


def _first(example: dict, keys: tuple[str, ...]) -> Optional[Any]:
    for k in keys:
        if k in example and example[k] is not None:
            return example[k]
    return None


def _coerce_messages(raw: Any) -> list[dict[str, str]]:
    """Coerce a raw ``messages`` value into ``[{"role", "content"}, ...]``.

    Handles the HF-on-disk variants where each message may itself be a JSON
    string, and strips any extra per-message keys so the Arrow schema is always
    a flat ``List(struct{role, content})``.
    """
    if raw is None:
        raise ValueError("messages field is null")
    msgs = raw
    if msgs and isinstance(msgs[0], str):
        msgs = [json.loads(m) for m in msgs]
    out = []
    for m in msgs:
        out.append({"role": str(m.get("role", "")), "content": str(m.get("content", ""))})
    return out


def normalize_to_messages(example: dict, fmt: Optional[str] = None) -> list[dict[str, str]]:
    """Normalize a single example (any supported shape) to a messages list.

    ``fmt`` may be passed to force a format; when omitted it is auto-detected
    from the example's own keys. For the ``text`` shape the returned list is a
    single message with role :data:`RAW_TEXT_ROLE` carrying the raw text, which
    :func:`messages_to_text` emits verbatim (no chat template).
    """
    if fmt is None:
        fmt = detect_format(list(example.keys()))

    if fmt == "messages":
        return _coerce_messages(example["messages"])

    if fmt == "alpaca":
        instruction = (_first(example, _INSTRUCTION_KEYS) or "").strip()
        extra = _first(example, _ALPACA_INPUT_KEYS)
        output = _first(example, _ALPACA_OUTPUT_KEYS)
        if output is None:
            raise ValueError("alpaca format requires a non-null 'output' field")
        user = instruction
        if extra is not None and str(extra).strip():
            user = f"{instruction}\n\n{str(extra).strip()}" if instruction else str(extra).strip()
        return [
            {"role": "user", "content": user},
            {"role": "assistant", "content": str(output)},
        ]

    if fmt == "prompt_completion":
        prompt = _first(example, _PROMPT_KEYS)
        completion = _first(example, _COMPLETION_KEYS)
        if prompt is None or completion is None:
            raise ValueError("prompt_completion format requires both prompt and completion")
        return [
            {"role": "user", "content": str(prompt)},
            {"role": "assistant", "content": str(completion)},
        ]

    if fmt == "text":
        text = _first(example, _TEXT_KEYS)
        if text is None:
            raise ValueError("text format requires a non-null 'text' field")
        return [{"role": RAW_TEXT_ROLE, "content": str(text)}]

    raise ValueError(f"Unknown dataset format: {fmt!r}")


def messages_to_text(messages: list[dict[str, str]], tokenizer) -> str:
    """Render a normalized messages list to the training ``text`` field.

    A single :data:`RAW_TEXT_ROLE` message is returned verbatim (the dataset
    already supplied finished text); everything else goes through the
    tokenizer's chat template with ``add_generation_prompt=False``.
    """
    if len(messages) == 1 and messages[0].get("role") == RAW_TEXT_ROLE:
        return messages[0]["content"]
    return tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=False
    )


def normalize_dataset(rows, fmt: Optional[str] = None) -> list[dict]:
    """Normalize an iterable of raw examples to ``[{"messages": [...]}, ...]``.

    ``fmt`` is detected once from the first row's columns when omitted, then
    applied to every row (so a heterogeneous source fails loudly rather than
    silently training on mixed shapes).
    """
    out = []
    resolved = fmt
    for ex in rows:
        if resolved is None:
            resolved = detect_format(list(ex.keys()))
        out.append({"messages": normalize_to_messages(ex, resolved)})
    return out
