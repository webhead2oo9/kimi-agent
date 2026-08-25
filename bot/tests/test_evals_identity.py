import pytest

from evals.identity import EvalIdentity


def test_eval_identity_is_stable_numeric_and_partitioned():
    base = EvalIdentity("run-1", "candidate:luna", "vision", 0)
    same = EvalIdentity("run-1", "candidate:luna", "vision", 0)
    variants = [
        EvalIdentity("run-2", "candidate:luna", "vision", 0),
        EvalIdentity("run-1", "baseline:luna", "vision", 0),
        EvalIdentity("run-1", "candidate:luna", "workspace", 0),
        EvalIdentity("run-1", "candidate:luna", "vision", 1),
    ]

    assert base.user_id == same.user_id
    assert base.digest == same.digest
    assert base.user_id.isdigit()
    assert len(base.user_id) == 18
    assert len({base.user_id, *(variant.user_id for variant in variants)}) == 5
    assert base.as_dict()["user_id"] == base.user_id


@pytest.mark.parametrize(
    ("field", "value"),
    [("run_nonce", ""), ("arm", ""), ("scenario_id", ""), ("repetition", -1)],
)
def test_eval_identity_rejects_incomplete_components(field, value):
    values = {
        "run_nonce": "run",
        "arm": "candidate",
        "scenario_id": "scenario",
        "repetition": 0,
    }
    values[field] = value
    with pytest.raises(ValueError):
        EvalIdentity(**values)
