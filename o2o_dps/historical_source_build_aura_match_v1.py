"""Join exact historical Warrior builds to strict-prefix aura evidence.

The source-bound request's empty buff/consume maps mean *unconfigured*, not
historically absent.  This development-only join copies only simulator fields
whose spell IDs and proto representation are unambiguous.  Since Chronicle
does not expose remaining aura duration, applying them to a whole simulated
window requires an explicit persistence assumption.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from . import historical_behavior_clone_full_rollout_v2 as full_rollout_v2
from . import historical_fury_source_bound_dynamic_hypothesis_v2 as hypothesis_v2
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v4 import sha256_json
from .sim_bridge_dynamic_v3 import dynamic_target_semantics_config_from_wire_v3


class HistoricalSourceAuraMatchError(ValueError):
    pass


# Spell IDs observed in the strict-prefix Chronicle aura state; values are
# verified against wowsims-turtle/proto/common.proto.  Tristate/food effects
# are intentionally excluded when strength, rank, or precise food is unknown.
_SUPPORTED: dict[int, tuple[str, str, Any]] = {
    11405: ("consumes", "strengthBuff", "ElixirOfGiants"),
    17538: ("consumes", "agilityElixir", "ElixirOfTheMongoose"),
    17626: ("consumes", "flask", "FlaskOfTheTitans"),
    19506: ("raid_buffs", "trueshotAura", True),
    24932: ("raid_buffs", "leaderOfThePack", True),
    25898: ("individual_buffs", "blessingOfKings", True),
    25899: ("individual_buffs", "blessingOfSanctuary", True),
    57108: ("raid_buffs", "emeraldBlessing", True),
}


def _map(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalSourceAuraMatchError(f"{name} must be an object")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalSourceAuraMatchError(f"{name} must be nonempty text")
    return value


def _identity(request_row: Mapping[str, Any], checkpoint_row: Mapping[str, Any]) -> None:
    if _text(request_row.get("segment_ref"), "request segment_ref") != _text(
        checkpoint_row.get("segment_ref"), "checkpoint segment_ref"
    ):
        raise HistoricalSourceAuraMatchError("source segment mismatch")
    causal = _map(request_row.get("causal_source_identity"), "causal_source_identity")
    source = _map(causal.get("identity"), "source identity")
    valid_from = _map(causal.get("valid_from"), "source valid_from")
    checkpoint = _map(checkpoint_row.get("source_identity"), "checkpoint identity")
    for name in ("instance_id", "player_guid"):
        if _text(source.get(name), f"source {name}").lower() != _text(
            checkpoint.get(name), f"checkpoint {name}"
        ).lower():
            raise HistoricalSourceAuraMatchError(f"{name} mismatch")
    _text(valid_from.get("encounter_id"), "source encounter_id")
    _text(checkpoint.get("encounter_id"), "checkpoint encounter_id")
    window_start = _map(checkpoint_row.get("window_start"), "window_start")
    cutoff = window_start.get("cutoff_exclusive_order_key")
    if not isinstance(cutoff, list) or not cutoff or not isinstance(cutoff[0], int):
        raise HistoricalSourceAuraMatchError("window cutoff timestamp unavailable")
    valid_timestamp = valid_from.get("timestamp_ms")
    valid_event_index = valid_from.get("event_index")
    if (
        not isinstance(valid_timestamp, int)
        or not isinstance(valid_event_index, int)
        or (valid_timestamp, valid_event_index) >= (cutoff[0], cutoff[1])
    ):
        raise HistoricalSourceAuraMatchError("source build starts after window cutoff")
    prefix = _map(checkpoint_row.get("strict_prefix_input"), "strict_prefix_input")
    if prefix.get("future_suffix_used") is not False:
        raise HistoricalSourceAuraMatchError("checkpoint is not strict-prefix only")


def match_source_build_auras(
    request_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    *,
    assume_persists_through_window: bool = False,
) -> dict[str, Any]:
    """Return a development request plus field-level observed/assumed provenance.

    Without the explicit assumption the executable request is unchanged and
    the observed fields remain diagnostics.  This never promotes an absent
    aura to a negative observation and never authorizes comparison or release.
    """
    _identity(request_row, checkpoint_row)
    if not isinstance(assume_persists_through_window, bool):
        raise HistoricalSourceAuraMatchError("persistence assumption must be bool")
    composition = _map(request_row.get("composition"), "composition")
    request = deepcopy(dict(_map(composition.get("request"), "source request")))
    raid = _map(request.get("raid"), "raid")
    parties = raid.get("parties")
    if not isinstance(parties, list) or not parties:
        raise HistoricalSourceAuraMatchError("request must have a first party")
    players = _map(parties[0], "first party").get("players")
    if not isinstance(players, list) or not players:
        raise HistoricalSourceAuraMatchError("request must have a first player")
    player = _map(players[0], "first player")
    if player.get("class") != "ClassWarrior":
        raise HistoricalSourceAuraMatchError("source player is not a Warrior")
    if player.get("consumes") != {} or player.get("buffs") != {} or raid.get("buffs") != {}:
        raise HistoricalSourceAuraMatchError("source request already configures effects")

    fields = _map(checkpoint_row.get("fields"), "checkpoint fields")
    aura = _map(fields.get("player.self_auras_and_procs"), "self aura field")
    if aura.get("observation_category") != "PARTIAL":
        raise HistoricalSourceAuraMatchError("strict-prefix aura identities unavailable")
    value = _map(aura.get("value"), "self aura value")
    active = value.get("observed_active")
    if not isinstance(active, list):
        raise HistoricalSourceAuraMatchError("observed_active must be a list")
    cutoff = _map(checkpoint_row["window_start"], "window_start")[
        "cutoff_exclusive_order_key"
    ]
    observed: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    supported: dict[str, dict[str, Any]] = {
        "consumes": {}, "individual_buffs": {}, "raid_buffs": {}
    }
    for raw in active:
        row = _map(raw, "active aura")
        if row.get("is_buff") is not True:
            continue
        spell_id = row.get("spell_id")
        name = _text(row.get("spell"), "aura spell")
        if not isinstance(spell_id, int) or isinstance(spell_id, bool):
            raise HistoricalSourceAuraMatchError("aura spell_id must be an integer")
        anchor = _map(row.get("last_transition"), "aura transition")
        timestamp = anchor.get("timestamp_ms")
        event_index = anchor.get("event_index")
        if (
            not isinstance(timestamp, int)
            or not isinstance(event_index, int)
            or (timestamp, event_index) >= (cutoff[0], cutoff[1])
        ):
            raise HistoricalSourceAuraMatchError("active aura is not strict-prefix evidence")
        evidence = {
            "spell_id": spell_id,
            "spell": name,
            "last_transition": deepcopy(dict(anchor)),
            "remaining_duration_status": row.get("remaining_duration_status"),
        }
        observed.append(evidence)
        mapping = _SUPPORTED.get(spell_id)
        if mapping is None:
            unmapped.append(evidence)
            continue
        location, key, setting = mapping
        if key in supported[location] and supported[location][key] != setting:
            raise HistoricalSourceAuraMatchError(f"conflicting observed aura mapping: {key}")
        supported[location][key] = setting

    if assume_persists_through_window:
        player["consumes"].update(supported["consumes"])
        player["buffs"].update(supported["individual_buffs"])
        raid["buffs"].update(supported["raid_buffs"])
    return {
        "segment_ref": request_row["segment_ref"],
        "source_identity": deepcopy(dict(_map(checkpoint_row["source_identity"], "checkpoint identity"))),
        "request": request,
        "observed_active_self_buffs_at_cutoff": observed,
        "simulator_supported_at_cutoff": supported,
        "unmapped_active_self_buffs_at_cutoff": unmapped,
        "unobserved_effects_status": "UNKNOWN_NOT_PROVEN_ABSENT",
        "remaining_duration_status": "UNKNOWN_NOT_IN_CHRONICLE_AURA_PROTO",
        "applied_to_request": assume_persists_through_window,
        "whole_window_persistence": (
            "EXPLICIT_DEVELOPMENT_ASSUMPTION"
            if assume_persists_through_window else "NOT_ASSUMED"
        ),
        "comparison_eligible": False,
    }


def match_source_build_aura_bundle(
    source_bundle: Mapping[str, Any],
    checkpoint_bundle: Mapping[str, Any],
    *,
    assume_persists_through_window: bool = False,
) -> list[dict[str, Any]]:
    """Match every exact source build once; reject missing/extra checkpoint rows."""
    requests = _map(source_bundle, "source bundle").get("requests")
    checkpoints = _map(checkpoint_bundle, "checkpoint bundle").get("rows")
    if not isinstance(requests, list) or not isinstance(checkpoints, list):
        raise HistoricalSourceAuraMatchError("bundle requests and rows must be lists")
    by_segment: dict[str, Mapping[str, Any]] = {}
    for row in checkpoints:
        item = _map(row, "checkpoint row")
        segment = _text(item.get("segment_ref"), "checkpoint segment_ref")
        if segment in by_segment:
            raise HistoricalSourceAuraMatchError("duplicate checkpoint source segment")
        by_segment[segment] = item
    result = []
    for row in requests:
        item = _map(row, "source request row")
        segment = _text(item.get("segment_ref"), "source segment_ref")
        if segment not in by_segment:
            raise HistoricalSourceAuraMatchError("missing checkpoint source segment")
        result.append(match_source_build_auras(
            item, by_segment.pop(segment),
            assume_persists_through_window=assume_persists_through_window,
        ))
    if by_segment:
        raise HistoricalSourceAuraMatchError("checkpoint has extra source segments")
    return result


def build_ready_source_aura_development_pair(
    source_bundle: Mapping[str, Any],
    checkpoint_bundle: Mapping[str, Any],
    hypothesis_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay the sole v2 wire-smoke pair without changing its frozen manifest.

    The original compiler only changes ``encounter``; this external overlay
    changes ``raid`` effects from the same source player's strict-prefix auras.
    It remains a nonvoting, nontraining, non-HPC sensitivity request.
    """
    try:
        hypothesis = hypothesis_v2.validate_historical_fury_source_bound_dynamic_hypothesis_v2(
            hypothesis_manifest
        )
    except hypothesis_v2.HistoricalFurySourceBoundDynamicHypothesisV2Error as error:
        raise HistoricalSourceAuraMatchError(str(error)) from error
    source = _map(source_bundle, "source bundle")
    checkpoint = _map(checkpoint_bundle, "checkpoint bundle")
    closure = _map(hypothesis.get("input_closure"), "hypothesis input closure")
    source_closure = _map(
        closure.get("source_bound_prototype_bundle_v1"), "source bundle closure"
    )
    source_address = _map(source.get("content_address"), "source bundle address")
    if source_closure.get("content_sha256") != source_address.get("sha256"):
        raise HistoricalSourceAuraMatchError("hypothesis is bound to another source bundle")
    matched = match_source_build_aura_bundle(
        source, checkpoint, assume_persists_through_window=True
    )
    ready = [
        _map(row, "hypothesis row")
        for row in hypothesis["requests"]
        if row.get("status") == hypothesis_v2.READY_STATUS
    ]
    if len(ready) != 1:
        raise HistoricalSourceAuraMatchError("expected exactly one v2 READY pair")
    row = ready[0]
    segment = _text(row.get("segment_ref"), "READY segment_ref")
    source_rows = [
        _map(item, "source request row") for item in source["requests"]
        if item.get("segment_ref") == segment
    ]
    overlay_rows = [item for item in matched if item["segment_ref"] == segment]
    if len(source_rows) != 1 or len(overlay_rows) != 1:
        raise HistoricalSourceAuraMatchError("READY pair has no unique source/aura join")
    source_row = source_rows[0]
    overlay = overlay_rows[0]
    if row.get("base_request_sha256") != source_row.get("request_sha256"):
        raise HistoricalSourceAuraMatchError("READY pair source request differs")
    original_base = _map(
        _map(source_row.get("composition"), "source composition").get("request"),
        "source request",
    )
    original_derived = _map(row.get("derived_request_template"), "v2 derived request")
    if (
        sha256_json(original_base) != row.get("base_request_sha256")
        or sha256_json(original_derived) != row.get("derived_request_template_sha256")
        or original_derived.get("raid") != original_base.get("raid")
        or original_derived.get("simOptions") != original_base.get("simOptions")
        or overlay["request"].get("encounter") != original_base.get("encounter")
        or overlay["request"].get("simOptions") != original_base.get("simOptions")
    ):
        raise HistoricalSourceAuraMatchError("v2 derivation changed more than encounter")
    derived = deepcopy(dict(original_derived))
    derived["raid"] = deepcopy(overlay["request"]["raid"])
    try:
        full_rollout_v2.validate_executable_fury_request_v2(derived)
        config = dynamic_target_semantics_config_from_wire_v3(
            _map(row.get("dynamic_load_config"), "v2 dynamic config")
        )
        seed_text = _map(derived.get("simOptions"), "simOptions").get("randomSeed")
        declared_seeds = _map(source.get("development_seeds"), "development seeds").get(
            "master_seeds"
        )
        if (
            not isinstance(seed_text, str)
            or not seed_text.isdecimal()
            or not isinstance(declared_seeds, list)
            or not declared_seeds
            or int(seed_text) != declared_seeds[0]
        ):
            raise HistoricalSourceAuraMatchError(
                "derived template does not retain the first declared development seed"
            )
        seed = int(seed_text)
        dynamic_load = DynamicRolloutLoadV3.bind(derived, seed, config)
    except (TypeError, ValueError, RuntimeError) as error:
        raise HistoricalSourceAuraMatchError(
            f"aura-overlay request/config cannot bind: {error}"
        ) from error
    old_binding = _map(row.get("pair_binding"), "v2 pair binding")
    if old_binding.get("dynamic_load_config_content_sha256") != config.content_sha256:
        raise HistoricalSourceAuraMatchError("v2 pair binding dynamic config differs")
    pair_core = {
        "schema": "historical_source_aura_development_pair/v1",
        "segment_ref": segment,
        "source_v2_pair_content_sha256": old_binding.get("content_sha256"),
        "source_v2_derived_request_sha256": row["derived_request_template_sha256"],
        "source_prefix_checkpoint_sha256": _map(
            checkpoint.get("content_address"), "checkpoint address"
        ).get("sha256"),
        "aura_overlay_base_request_sha256": sha256_json(overlay["request"]),
        "aura_overlay_derived_request_sha256": sha256_json(derived),
        "dynamic_load_config_content_sha256": config.content_sha256,
        "aura_overlay_dynamic_load_contract_sha256": dynamic_load.contract_sha256,
        "whole_window_persistence": "EXPLICIT_DEVELOPMENT_ASSUMPTION",
        "comparison_eligible": False,
        "policy_value_authorized": False,
        "training_authorized": False,
        "hpc_authorized": False,
    }
    return {
        "request": derived,
        "dynamic_load_config": config.to_wire(),
        "pair_binding": {**pair_core, "content_sha256": sha256_json(pair_core)},
        "aura_match": {
            key: deepcopy(overlay[key]) for key in (
                "simulator_supported_at_cutoff",
                "unmapped_active_self_buffs_at_cutoff",
                "unobserved_effects_status",
                "remaining_duration_status",
                "whole_window_persistence",
            )
        },
        "status": "READY_FOR_LOCAL_NATIVE_WIRE_SMOKE_ONLY_NONVOTING",
        "comparison_eligible": False,
        "training_authorized": False,
        "hpc_authorized": False,
    }
