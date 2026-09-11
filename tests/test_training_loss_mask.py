"""Real TRL label regression using a local tokenizer, with no model download."""

import pytest

pytest.importorskip("torch")
pytest.importorskip("trl")
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import PreTrainedTokenizerFast
from trl.trainer.sft_trainer import DataCollatorForLanguageModeling

from dataset_format import RAW_TEXT_ROLE, tokenize_training_example


@pytest.fixture
def tokenizer():
    vocab = {word: i for i, word in enumerate([
        "[UNK]", "[PAD]", "system", "user", "assistant", "tool", "policy",
        "question", "answer", "followup", "second", "result", "end",
    ])}
    backend = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.Whitespace()
    tok = PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="[UNK]", pad_token="[PAD]")
    tok.chat_template = (
        "{% for m in messages %}{{ m['role'] + ' ' + m['content'] + ' end ' }}{% endfor %}"
        "{% if add_generation_prompt %}{{ 'assistant ' }}{% endif %}"
    )
    return tok


def test_actual_trl_collator_masks_every_nonassistant_turn(tokenizer):
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
        {"role": "tool", "content": "result"},
        {"role": "user", "content": "followup"},
        {"role": "assistant", "content": "second"},
    ]
    example = tokenize_training_example(messages, tokenizer, 128)
    collator = DataCollatorForLanguageModeling(tokenizer.pad_token_id, completion_only_loss=True)
    labels = collator([example])["labels"][0].tolist()
    supervised = tokenizer.convert_ids_to_tokens([label for label in labels if label != -100])
    assert supervised == ["answer", "end", "second", "end"]
    # A config flag without masks was the original bug: prompts were trained.
    unmasked = collator([{"input_ids": example["input_ids"]}])["labels"][0].tolist()
    assert tokenizer.convert_tokens_to_ids("policy") in unmasked
    assert tokenizer.convert_tokens_to_ids("policy") not in labels


def test_sft_trainer_preserves_masks_in_actual_training_batch(tokenizer, tmp_path):
    from datasets import Dataset
    from transformers import GPT2Config, GPT2LMHeadModel
    from trl import SFTConfig, SFTTrainer

    example = tokenize_training_example([
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ], tokenizer, 128)
    model = GPT2LMHeadModel(GPT2Config(
        vocab_size=len(tokenizer), n_layer=1, n_head=1, n_embd=8, n_positions=128,
        bos_token_id=tokenizer.unk_token_id, eos_token_id=tokenizer.unk_token_id,
    ))
    trainer = SFTTrainer(
        model=model, processing_class=tokenizer,
        train_dataset=Dataset.from_list([example]),
        args=SFTConfig(output_dir=str(tmp_path), use_cpu=True, bf16=False,
                       fp16=False, report_to="none", max_length=128,
                       completion_only_loss=True, gradient_checkpointing=False),
    )
    labels = next(iter(trainer.get_train_dataloader()))["labels"][0].tolist()
    assert tokenizer.convert_ids_to_tokens([v for v in labels if v != -100]) == ["answer", "end"]


def test_raw_text_retains_language_model_loss(tokenizer):
    example = tokenize_training_example(
        [{"role": RAW_TEXT_ROLE, "content": "question answer"}], tokenizer, 128
    )
    assert example["completion_mask"] == [1, 1]
    assert example["text"] == "question answer"


@pytest.mark.parametrize("max_length", [1, 2, 3, 4])
def test_truncation_with_no_assistant_target_fails(tokenizer, max_length):
    messages = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}]
    with pytest.raises(ValueError, match="no target tokens"):
        tokenize_training_example(messages, tokenizer, max_length)


def test_chat_without_assistant_fails(tokenizer):
    with pytest.raises(ValueError, match="no assistant response"):
        tokenize_training_example([{"role": "user", "content": "question"}], tokenizer, 128)


def test_prefix_changing_template_is_rejected(tokenizer):
    tokenizer.chat_template = "{{ messages | length }} {% for m in messages %}{{ m['content'] }} {% endfor %}"
    with pytest.raises(ValueError, match="cannot safely derive"):
        tokenize_training_example([
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ], tokenizer, 128)


def test_packing_option_retains_explicit_full_text_loss(tokenizer):
    example = tokenize_training_example([
        {"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"},
    ], tokenizer, 128, completion_only=False)
    assert all(example["completion_mask"])
