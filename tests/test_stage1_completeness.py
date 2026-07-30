from sgrm.interfaces import EvalResult
from sgrm.sgrm import GroupProfile, SGRMOptimizer


def test_stage1_budget_exhaustion_fills_unprofiled_groups():
    optimizer = object.__new__(SGRMOptimizer)
    optimizer.group_names = ["g0", "g1"]
    optimizer.fifo_ids_by_group = {"g0": [0], "g1": [1]}
    optimizer.group_design_space = {"g0": [2, 4], "g1": [2, 4]}
    optimizer._start_from_small = False

    measured = GroupProfile(
        name="g0",
        fifo_ids=[0],
        design_space=[2, 4],
        default_depth_idx=1,
        default_impl_type="srl",
        efficiency=7.0,
    )
    optimizer._stage1_profile = lambda _baseline, budget: [measured]
    optimizer._find_default_depth_idx = lambda _gname: 1
    optimizer._get_baseline_impl_type = lambda _gname, _idx: "srl"

    baseline = EvalResult({}, False, 10, 0)
    profiles, seed_state = optimizer._stage1_profile_and_seed(baseline, budget=1)

    assert seed_state is None
    assert [profile.name for profile in profiles] == optimizer.group_names
    assert profiles[0] is measured
    assert profiles[1].fifo_ids == [1]

    complete_state = {
        profile.name: (profile.default_depth_idx, profile.default_impl_type)
        for profile in profiles
    }
    assert optimizer._state_key(complete_state) == (
        ("g0", 1, "srl"),
        ("g1", 1, "srl"),
    )
