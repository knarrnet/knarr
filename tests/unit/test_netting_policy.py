import pytest

from knarr.commerce.netting_policy import ManualPolicy, RatioPolicy, get_policy


def test_ratio_policy_triggers_at_threshold():
    policy = RatioPolicy(threshold=0.8)
    assert policy.should_initiate(balance=-8.0, hard_limit=-10.0) is True


def test_ratio_policy_requires_negative_hard_limit():
    policy = RatioPolicy(threshold=0.8)
    assert policy.should_initiate(balance=-8.0, hard_limit=10.0) is False


def test_manual_policy_never_auto_fires():
    assert ManualPolicy().should_initiate(balance=-100.0, hard_limit=-10.0) is False


def test_get_policy_builds_manual_policy():
    assert isinstance(get_policy({"netting": {"policy": "manual"}}), ManualPolicy)


def test_get_policy_defaults_unknown_to_ratio():
    assert isinstance(get_policy({"netting": {"policy": "mystery"}}), RatioPolicy)


def test_ratio_policy_rejects_invalid_threshold():
    with pytest.raises(ValueError):
        RatioPolicy(threshold=1.5)
