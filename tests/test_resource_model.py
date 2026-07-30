from sgrm.resource_model import (
    get_valid_impl_types,
    predict_bram,
    predict_ff,
    predict_lut,
    predict_uram,
)


def test_concrete_search_space_excludes_auto():
    assert get_valid_impl_types(32, 128) == ["srl", "lutram", "bram", "uram"]


def test_depth_two_is_implementation_independent():
    ff_values = {predict_ff(32, 2, impl) for impl in ("srl", "lutram", "bram", "uram")}
    lut_values = {predict_lut(32, 2, impl) for impl in ("srl", "lutram", "bram", "uram")}
    assert ff_values == {65}
    assert lut_values == {36}


def test_storage_resources_follow_explicit_implementation():
    width, depth = 64, 4096
    assert predict_bram(width, depth, "bram") > 0
    assert predict_bram(width, depth, "uram") == 0
    assert predict_uram(width, depth, "uram") > 0
    assert predict_uram(width, depth, "bram") == 0
