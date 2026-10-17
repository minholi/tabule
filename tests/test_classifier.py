import pytest

from tabule.classifier import OptionsMismatchError, build_choices, decide
from tabule.decisions import ChoiceAnswer


def test_build_choices_requires_description_for_every_option():
    with pytest.raises(OptionsMismatchError, match="RECUSA X"):
        build_choices(["RECUSA X", "OUTRA"], {"RECUSA X": "", "OUTRA": "descrição"})


def test_build_choices_keeps_order_from_chatwoot():
    choices = build_choices(["B", "A"], {"A": "da A", "B": "da B"})
    assert choices == [{"value": "B", "description": "da B"}, {"value": "A", "description": "da A"}]


def test_refusal_goes_to_review():
    outcome = decide(ChoiceAnswer(refused=True, choice=None, confidence=None), valid_values=["A", "B"], threshold=0.8)
    assert outcome.action == "review"


def test_value_outside_chatwoot_list_goes_to_review():
    outcome = decide(ChoiceAnswer(False, "Z", 0.99), valid_values=["A", "B"], threshold=0.8)
    assert outcome.action == "review"


def test_low_confidence_goes_to_review():
    outcome = decide(ChoiceAnswer(False, "A", 0.5), valid_values=["A", "B"], threshold=0.8)
    assert outcome.action == "review"
    assert outcome.choice == "A"


def test_confident_valid_choice_is_accepted():
    outcome = decide(ChoiceAnswer(False, "A", 0.9), valid_values=["A", "B"], threshold=0.8)
    assert outcome.action == "accept"
    assert outcome.choice == "A"
