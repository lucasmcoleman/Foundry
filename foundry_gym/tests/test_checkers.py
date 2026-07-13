"""Thorough unit tests for foundry_gym.core.checkers: canonicalization,
equality, JSON extraction, schema validation, answer/number/date/money
parsing."""

from __future__ import annotations

from foundry_gym.core import checkers
from foundry_gym.core.checkers import CanonicalError

import pytest


# ---------------------------------------------------------------------------
# canonical
# ---------------------------------------------------------------------------


class _EvilInt(int):
    def __eq__(self, other):
        return True

    def __hash__(self):
        return int.__hash__(self)


class _EvilStr(str):
    def __eq__(self, other):
        return True

    def __hash__(self):
        return str.__hash__(self)


class _EvilList(list):
    def __eq__(self, other):
        return True


class TestCanonical:
    def test_subclass_of_int_with_overridden_eq_raises(self):
        with pytest.raises(CanonicalError):
            checkers.canonical(_EvilInt(5))

    def test_subclass_of_str_with_overridden_eq_raises(self):
        with pytest.raises(CanonicalError):
            checkers.canonical(_EvilStr("hi"))

    def test_subclass_of_list_with_overridden_eq_raises(self):
        with pytest.raises(CanonicalError):
            checkers.canonical(_EvilList([1, 2, 3]))

    def test_bool_survives(self):
        assert checkers.canonical(True) is True
        assert checkers.canonical(False) is False

    def test_nan_float_raises(self):
        with pytest.raises(CanonicalError):
            checkers.canonical(float("nan"))

    def test_deep_nesting_over_32_raises(self):
        value = 0
        for _ in range(40):
            value = [value]
        with pytest.raises(CanonicalError):
            checkers.canonical(value)

    def test_moderate_nesting_ok(self):
        value = 0
        for _ in range(5):
            value = [value]
        # should not raise
        checkers.canonical(value)

    def test_tuple_becomes_list(self):
        result = checkers.canonical((1, 2, 3))
        assert result == [1, 2, 3]
        assert type(result) is list

    def test_dict_non_str_key_raises(self):
        with pytest.raises(CanonicalError):
            checkers.canonical({1: "a"})

    def test_unsupported_type_raises(self):
        class Foo:
            pass

        with pytest.raises(CanonicalError):
            checkers.canonical(Foo())

    def test_none_survives(self):
        assert checkers.canonical(None) is None


# ---------------------------------------------------------------------------
# canonical_equal
# ---------------------------------------------------------------------------


class _SpoofEq:
    def __eq__(self, other):
        return True


class TestCanonicalEqual:
    def test_bool_vs_int_not_equal(self):
        assert checkers.canonical_equal(True, 1) is False

    def test_int_vs_float_numeric_cross_type_equal(self):
        assert checkers.canonical_equal(1, 1.0) is True

    def test_dict_equal(self):
        assert checkers.canonical_equal({"a": 1}, {"a": 1}) is True

    def test_dict_not_equal(self):
        assert checkers.canonical_equal({"a": 1}, {"a": 2}) is False

    def test_tolerance_path_with_rel_tol(self):
        assert checkers.canonical_equal(1.0, 1.0009, rel_tol=0.01) is True
        assert checkers.canonical_equal(1.0, 2.0, rel_tol=0.01) is False

    def test_object_with_spoofed_eq_never_equal(self):
        assert checkers.canonical_equal(_SpoofEq(), _SpoofEq()) is False
        assert checkers.canonical_equal(_SpoofEq(), 1) is False

    def test_list_equal(self):
        assert checkers.canonical_equal([1, 2, 3], [1, 2, 3]) is True
        assert checkers.canonical_equal([1, 2, 3], [1, 2]) is False

    def test_false_vs_zero_not_equal(self):
        assert checkers.canonical_equal(False, 0) is False


# ---------------------------------------------------------------------------
# extract_json_response
# ---------------------------------------------------------------------------


class TestExtractJsonResponse:
    def test_last_parsing_fenced_block_wins(self):
        text = (
            "Here is my work:\n"
            "```json\n{\"a\": 1}\n```\n"
            "revised:\n"
            "```json\n{\"a\": 2}\n```\n"
            "oops this one is broken:\n"
            "```json\nnot valid json at all\n```\n"
        )
        value, status = checkers.extract_json_response(text)
        assert status == "ok"
        assert value == {"a": 2}

    def test_whole_text_json(self):
        value, status = checkers.extract_json_response('  {"x": 5}  ')
        assert status == "ok"
        assert value == {"x": 5}

    def test_exactly_one_balanced_object_in_prose(self):
        text = 'The answer is {"x": 1} as computed above.'
        value, status = checkers.extract_json_response(text)
        assert status == "ok"
        assert value == {"x": 1}

    def test_two_balanced_objects_in_prose_ambiguous(self):
        text = 'First {"a": 1} then {"b": 2} also valid.'
        value, status = checkers.extract_json_response(text)
        assert value is None
        assert "ambiguous" in status

    def test_no_json_found(self):
        value, status = checkers.extract_json_response("no json here at all")
        assert value is None
        assert status == "no JSON value found"

    def test_over_max_len_returns_none(self):
        text = "x" * 50
        value, status = checkers.extract_json_response(text, max_len=10)
        assert value is None
        assert status == "response too long"

    def test_non_str_input_returns_none(self):
        value, status = checkers.extract_json_response(12345)
        assert value is None
        assert status == "response is not text"


# ---------------------------------------------------------------------------
# schema_check
# ---------------------------------------------------------------------------


class TestSchemaCheck:
    def test_type_mismatch(self):
        errors = checkers.schema_check(5, {"type": "string"})
        assert errors
        assert "expected string" in errors[0]

    def test_required_missing(self):
        schema = {
            "type": "object",
            "required": ["a", "b"],
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        }
        errors = checkers.schema_check({"a": 1}, schema)
        assert any("required property missing" in e and ".b" in e for e in errors)

    def test_additional_properties_false(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "additionalProperties": False,
        }
        errors = checkers.schema_check({"a": 1, "b": 2}, schema)
        assert any("additional property not allowed" in e for e in errors)

    def test_additional_properties_allowed_by_default(self):
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        errors = checkers.schema_check({"a": 1, "b": 2}, schema)
        assert errors == []

    def test_enum(self):
        errors = checkers.schema_check("z", {"enum": ["x", "y"]})
        assert any("not in enum" in e for e in errors)
        errors_ok = checkers.schema_check("x", {"enum": ["x", "y"]})
        assert errors_ok == []

    def test_items(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        errors = checkers.schema_check([1, "a", 3], schema)
        assert any("[1]" in e for e in errors)

    def test_min_items(self):
        schema = {"type": "array", "minItems": 3}
        errors = checkers.schema_check([1, 2], schema)
        assert any("fewer than 3 items" in e for e in errors)

    def test_max_items(self):
        schema = {"type": "array", "maxItems": 2}
        errors = checkers.schema_check([1, 2, 3], schema)
        assert any("more than 2 items" in e for e in errors)

    def test_integer_vs_boolean_distinction(self):
        errors = checkers.schema_check(True, {"type": "integer"})
        assert errors
        assert "expected integer" in errors[0]
        errors_ok = checkers.schema_check(3, {"type": "integer"})
        assert errors_ok == []

    def test_number_type_rejects_bool(self):
        errors = checkers.schema_check(True, {"type": "number"})
        assert errors

    def test_pattern(self):
        schema = {"type": "string", "pattern": r"^[0-9]+$"}
        assert checkers.schema_check("abc", schema)
        assert checkers.schema_check("123", schema) == []

    def test_nested_object_valid(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        }
        assert checkers.schema_check({"name": "a", "age": 3}, schema) == []


# ---------------------------------------------------------------------------
# extract_final_answer
# ---------------------------------------------------------------------------


class TestExtractFinalAnswer:
    def test_last_answer_line_wins(self):
        text = "reasoning...\nAnswer: 3\nmore reasoning\nAnswer: 5\n"
        token, status = checkers.extract_final_answer(text)
        assert status == "ok"
        assert token == "5"

    def test_boxed_fallback(self):
        text = "The result is \\boxed{42}."
        token, status = checkers.extract_final_answer(text)
        assert status == "ok"
        assert token == "42"

    def test_last_boxed_wins_when_multiple(self):
        text = "First \\boxed{1} then \\boxed{2}."
        token, status = checkers.extract_final_answer(text)
        assert token == "2"

    def test_answer_line_takes_priority_over_boxed(self):
        text = "\\boxed{1}\nAnswer: 9\n"
        token, status = checkers.extract_final_answer(text)
        assert token == "9"

    def test_none_when_no_marker(self):
        token, status = checkers.extract_final_answer("just some prose")
        assert token is None
        assert "no final answer marker" in status


# ---------------------------------------------------------------------------
# parse_number
# ---------------------------------------------------------------------------


class TestParseNumber:
    @pytest.mark.parametrize(
        "token,expected",
        [
            ("42", 42.0),
            ("-7", -7.0),
            ("3.14", 3.14),
            ("1,234,567", 1234567.0),
            ("3/4", 0.75),
            ("1e3", 1000.0),
        ],
    )
    def test_valid_tokens(self, token, expected):
        assert checkers.parse_number(token) == pytest.approx(expected)

    @pytest.mark.parametrize("token", ["garbage", "nan", "inf", "-inf", "", "1/0"])
    def test_invalid_tokens_return_none(self, token):
        assert checkers.parse_number(token) is None

    def test_non_str_input_returns_none(self):
        assert checkers.parse_number(42) is None


# ---------------------------------------------------------------------------
# normalize_date
# ---------------------------------------------------------------------------


class TestNormalizeDate:
    @pytest.mark.parametrize(
        "text",
        [
            "2026-03-07",
            "March 7, 2026",
            "7 March 2026",
            "03/07/2026",
        ],
    )
    def test_variants_normalize_to_iso(self, text):
        assert checkers.normalize_date(text) == "2026-03-07"

    def test_garbage_returns_none(self):
        assert checkers.normalize_date("not a date") is None

    def test_non_str_returns_none(self):
        assert checkers.normalize_date(12345) is None


# ---------------------------------------------------------------------------
# normalize_money
# ---------------------------------------------------------------------------


class TestNormalizeMoney:
    def test_dollar_string(self):
        assert checkers.normalize_money("$1,234.50") == 1234.5

    def test_numeric_input(self):
        assert checkers.normalize_money(1234.5) == 1234.5

    def test_currency_suffix_string(self):
        assert checkers.normalize_money("1234.50 USD") == 1234.5

    def test_garbage_returns_none(self):
        assert checkers.normalize_money("abc") is None

    def test_bool_rejected_as_numeric_but_still_string_path(self):
        # bool is excluded from the numeric fast-path (type(s) is not bool);
        # falls through to the isinstance(str) check and returns None.
        assert checkers.normalize_money(True) is None
