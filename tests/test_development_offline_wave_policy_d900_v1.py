from types import SimpleNamespace

import pytest

from scripts.development_offline_wave_policy_d900_remote_v1 import (
    RUNTIME,
    _offline_argv,
)
from scripts.development_offline_wave_policy_d900_v1 import (
    ENCOUNTER_ID,
    FOCAL_GUID,
    INSTANCE_ID,
    _cat_resolver_bomb_factory,
    _exact_build_segment_ref,
    _runtime_binding,
)


def _exact_build():
    return {
        "historical_runtime_executable": True,
        "source_identity": {
            "instance_id": INSTANCE_ID,
            "player_guid": FOCAL_GUID,
            "build_segment_id": "segment-0042",
        },
        "source_valid_from": {"encounter_id": ENCOUNTER_ID},
    }


def test_exact_build_segment_ref_is_source_bound():
    assert _exact_build_segment_ref(_exact_build()).endswith(":segment-0042")
    wrong = _exact_build()
    wrong["source_identity"]["player_guid"] = "another-player"
    with pytest.raises(ValueError, match="does not bind"):
        _exact_build_segment_ref(wrong)


def test_runtime_binding_uses_explicit_guid_map_not_position_or_modulo():
    policy = SimpleNamespace(source_target_guids=("GUID-B", "guid-a"))
    fixed = SimpleNamespace(
        occurrence_index_registry=(
            SimpleNamespace(target_guid="GUID-A", target_index=7),
            SimpleNamespace(target_guid="guid-b", target_index=3),
        )
    )
    binding = _runtime_binding(policy, fixed)
    assert binding.source_target_guids == ("GUID-B", "guid-a")
    assert binding.target_indexes == (3, 7)


def test_missing_source_target_fails_closed():
    policy = SimpleNamespace(source_target_guids=("GUID-MISSING",))
    fixed = SimpleNamespace(occurrence_index_registry=())
    with pytest.raises(ValueError, match="absent from the native registry"):
        _runtime_binding(policy, fixed)


def test_cat_resolver_bomb_and_remote_command_contract():
    with pytest.raises(AssertionError, match="CAT_RESOLVER_BOMB_CALLED"):
        _cat_resolver_bomb_factory()(None, ())
    argv = _offline_argv()
    assert argv[1] == (
        f"{RUNTIME}/scripts/development_offline_wave_policy_d900_v1.py"
    )
    assert "--source" not in argv
    assert "--deployed-runtime-binding" not in argv
    assert "--route-focus" not in argv
    assert argv[argv.index("--seed-count") + 1] == "3"
