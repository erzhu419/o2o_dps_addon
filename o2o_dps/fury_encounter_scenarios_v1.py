"""Compile bounded Chronicle wave reconstruction into Fury simulator scenarios.

The compiler is intentionally conservative.  A scenario is one duration-mode
``RaidSimRequest`` for one reconstructed wave/pile.  Chronicle kill-budget
proxies remain sampling metadata, while target level and armor are explicit
caller-supplied sensitivity hypotheses.  No target HP, coordinates, or
separation claim is synthesized.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence


JSONMap = dict[str, Any]
ARMOR_STAT_INDEX = 26
HEALTH_STAT_INDEX = 34
MIN_TARGET_STATS_LENGTH = HEALTH_STAT_INDEX + 1


class ScenarioCompileError(ValueError):
    """The reconstruction or base request cannot satisfy the V1 contract."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenarioCompileError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ScenarioCompileError(f"{label} must be an array")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioCompileError(f"{label} must be an integer")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioCompileError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ScenarioCompileError(f"{label} must be finite")
    return parsed


def _render_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _slug(value: Any) -> str:
    rendered = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value)).strip("-")
    return rendered or "unknown"


def _validate_hypotheses(
    armor_hypotheses: Iterable[int | float],
    level_hypotheses: Iterable[int],
) -> tuple[tuple[int | float, ...], tuple[int, ...]]:
    armors: list[int | float] = []
    for index, raw in enumerate(armor_hypotheses):
        value = _number(raw, f"armor_hypotheses[{index}]")
        if value < 0:
            raise ScenarioCompileError("armor hypotheses must be nonnegative")
        rendered = _render_number(value)
        if rendered not in armors:
            armors.append(rendered)
    levels: list[int] = []
    for index, raw in enumerate(level_hypotheses):
        value = _integer(raw, f"level_hypotheses[{index}]")
        if value <= 0:
            raise ScenarioCompileError("level hypotheses must be positive")
        if value not in levels:
            levels.append(value)
    if not armors:
        raise ScenarioCompileError(
            "at least one explicit armor hypothesis is required"
        )
    if not levels:
        raise ScenarioCompileError(
            "at least one explicit level hypothesis is required"
        )
    return tuple(armors), tuple(levels)


def _base_target_template(base_request: Mapping[str, Any]) -> Mapping[str, Any]:
    encounter = _mapping(base_request.get("encounter"), "base_request.encounter")
    targets = _array(encounter.get("targets"), "base_request.encounter.targets")
    if not targets:
        raise ScenarioCompileError(
            "base_request.encounter.targets must contain a target template"
        )
    return _mapping(targets[0], "base_request.encounter.targets[0]")


def _target_index(wave: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(_array(wave.get("targets"), "wave.targets")):
        target = _mapping(raw, f"wave.targets[{index}]")
        guid = target.get("target_guid")
        if not isinstance(guid, str) or not guid:
            raise ScenarioCompileError(f"wave.targets[{index}] has no target_guid")
        if guid in result:
            raise ScenarioCompileError(f"wave contains duplicate target_guid {guid}")
        result[guid] = target
    if not result:
        raise ScenarioCompileError("wave must contain at least one target")
    return result


def _group_components(
    wave: Mapping[str, Any], target_guids: Sequence[str]
) -> list[tuple[tuple[str, ...], bool, Mapping[str, Any]]]:
    groups = _array(wave.get("target_groups"), "wave.target_groups")
    components: list[tuple[tuple[str, ...], bool, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for index, raw in enumerate(groups):
        group = _mapping(raw, f"wave.target_groups[{index}]")
        guids_raw = _array(
            group.get("target_guids"), f"wave.target_groups[{index}].target_guids"
        )
        guids: list[str] = []
        for raw_guid in guids_raw:
            if not isinstance(raw_guid, str) or raw_guid not in target_guids:
                raise ScenarioCompileError(
                    f"wave.target_groups[{index}] references an unknown target"
                )
            if raw_guid in seen:
                raise ScenarioCompileError(
                    f"target {raw_guid} appears in multiple target groups"
                )
            seen.add(raw_guid)
            guids.append(raw_guid)
        if not guids:
            raise ScenarioCompileError(f"wave.target_groups[{index}] is empty")
        position = _mapping(
            group.get("position_assumption"),
            f"wave.target_groups[{index}].position_assumption",
        )
        stacked = (
            len(guids) > 1
            and position.get("value") == "stacked"
            and position.get("status") == "INFERRED"
        )
        components.append((tuple(guids), stacked, position))
    for guid in target_guids:
        if guid not in seen:
            components.append(
                (
                    (guid,),
                    False,
                    {
                        "value": "unknown",
                        "status": "MISSING",
                        "basis": "target absent from reconstruction target_groups",
                    },
                )
            )
    return components


def _spatial_layouts(wave: Mapping[str, Any]) -> list[JSONMap]:
    targets = _target_index(wave)
    guids = tuple(targets)
    components = _group_components(wave, guids)
    wave_position = _mapping(
        wave.get("position_assumption"), "wave.position_assumption"
    )

    if (
        len(guids) > 1
        and wave_position.get("value") == "stacked"
        and wave_position.get("status") == "INFERRED"
    ):
        return [
            {
                "variant": "cohit_stacked",
                "pile_ordinal": 1,
                "target_guids": list(guids),
                "spatial_assumption": {
                    "value": "stacked",
                    "status": "INFERRED",
                    "role": "evidence_bounded_inference",
                    "basis": wave_position.get("basis"),
                    "coordinates": None,
                    "not_observed": True,
                },
            }
        ]

    if len(guids) == 1:
        return [
            {
                "variant": "single_target",
                "pile_ordinal": 1,
                "target_guids": list(guids),
                "spatial_assumption": {
                    "value": "single_target",
                    "status": "RECONSTRUCTED",
                    "role": "no_relative_position_needed",
                    "coordinates": None,
                    "not_observed": True,
                },
            }
        ]

    # With no positive relation across every component, evaluate both extremes.
    # The upper variant assumes every target is mutually reachable.  The lower
    # family preserves positive co-hit components but otherwise separates them.
    layouts: list[JSONMap] = [
        {
            "variant": "stacked_upper",
            "pile_ordinal": 1,
            "target_guids": list(guids),
            "spatial_assumption": {
                "value": "stacked",
                "status": "INFERRED",
                "role": "sensitivity_upper",
                "basis": "space is missing; assume all wave targets are mutually reachable",
                "coordinates": None,
                "not_observed": True,
            },
        }
    ]
    for pile_ordinal, (component, stacked, position) in enumerate(
        components, start=1
    ):
        variant = "separated_component" if stacked else "separated_singleton"
        layouts.append(
            {
                "variant": variant,
                "pile_ordinal": pile_ordinal,
                "target_guids": list(component),
                "spatial_assumption": {
                    "value": "stacked" if stacked else "separated_singleton",
                    "status": "INFERRED",
                    "role": (
                        "evidence_bounded_component_in_sensitivity_lower"
                        if stacked
                        else "sensitivity_lower"
                    ),
                    "basis": (
                        position.get("basis")
                        if stacked
                        else "space is missing; assume this target is outside every other pile"
                    ),
                    "coordinates": None,
                    "not_observed": True,
                },
            }
        )
    return layouts


def _sim_target(
    template: Mapping[str, Any],
    source_target: Mapping[str, Any],
    *,
    armor: int | float,
    level: int,
) -> JSONMap:
    target = deepcopy(dict(template))
    stats_source = target.get("stats")
    if stats_source is not None and not isinstance(stats_source, list):
        raise ScenarioCompileError("base target stats must be an array")
    stats_length = max(
        MIN_TARGET_STATS_LENGTH,
        len(stats_source) if isinstance(stats_source, list) else 0,
    )
    stats: list[int | float] = [0] * stats_length
    stats[ARMOR_STAT_INDEX] = armor
    # Individual enemy health is deliberately disabled: current wowsims health
    # fights aggregate damage and do not retire individual target units.
    stats[HEALTH_STAT_INDEX] = 0

    target.pop("id", None)
    target["name"] = str(
        source_target.get("target_name")
        or source_target.get("target_guid")
        or "Chronicle target"
    )
    target["level"] = level
    target["mobType"] = "MobTypeUnknown"
    target["stats"] = stats
    # Target offense/threat ownership is not identified by the reconstruction.
    # Remove template boss behavior rather than silently inheriting it.
    for field in (
        "minBaseDamage",
        "damageSpread",
        "swingSpeed",
        "dualWield",
        "dualWieldPenalty",
        "parryHaste",
        "spellSchool",
        "targetInputs",
    ):
        target.pop(field, None)
    target["tankIndex"] = -1
    return target


def _kill_budget_metadata(
    source_targets: Sequence[Mapping[str, Any]],
) -> tuple[list[JSONMap], JSONMap]:
    proxies: list[JSONMap] = []
    total = 0.0
    available = 0
    for target in source_targets:
        proxy_raw = target.get("kill_budget_proxy")
        proxy = proxy_raw if isinstance(proxy_raw, Mapping) else {}
        value_raw = proxy.get("value")
        value: int | float | None = None
        if isinstance(value_raw, (int, float)) and not isinstance(value_raw, bool):
            parsed = float(value_raw)
            if math.isfinite(parsed) and parsed >= 0:
                value = _render_number(parsed)
                total += parsed
                available += 1
        proxies.append(
            {
                "target_guid": target.get("target_guid"),
                "target_name": target.get("target_name"),
                "value": value,
                "status": proxy.get("status", "MISSING"),
                "death_anchor": proxy.get("death_anchor"),
                "role": "sampling_weight_metadata_only",
                "not_exact_health": True,
            }
        )
    weight: JSONMap = {
        "value": _render_number(total) if available else None,
        "status": "RECONSTRUCTED" if available else "MISSING",
        "available_target_count": available,
        "target_count": len(source_targets),
        "completeness": "complete" if available == len(source_targets) else "partial",
        "basis": "sum of available Chronicle kill-budget proxies",
        "role": "sampling_or_aggregation_weight_only",
        "not_exact_health": True,
    }
    return proxies, weight


def _target_source_anchors(
    source_targets: Sequence[Mapping[str, Any]],
) -> list[JSONMap]:
    anchors: list[JSONMap] = []
    for target in source_targets:
        interval_raw = target.get("activity_interval")
        interval = interval_raw if isinstance(interval_raw, Mapping) else {}
        anchors.append(
            {
                "target_guid": target.get("target_guid"),
                "first_anchor": interval.get("first_anchor"),
                "last_anchor": interval.get("last_anchor"),
            }
        )
    return anchors


def _request_for_scenario(
    base_request: Mapping[str, Any],
    target_template: Mapping[str, Any],
    source_targets: Sequence[Mapping[str, Any]],
    *,
    duration_ms: int,
    armor: int | float,
    level: int,
) -> JSONMap:
    request = deepcopy(dict(base_request))
    encounter = _mapping(request.get("encounter"), "base_request.encounter")
    if not isinstance(encounter, dict):
        # deepcopy(dict(...)) retains ordinary dicts in supported JSON input;
        # this branch gives a useful error for custom Mapping implementations.
        encounter = dict(encounter)
        request["encounter"] = encounter
    encounter["duration"] = _render_number(duration_ms / 1000.0)
    encounter["durationVariation"] = 0
    encounter["useHealth"] = False
    encounter["targets"] = [
        _sim_target(target_template, target, armor=armor, level=level)
        for target in source_targets
    ]
    return request


def compile_fury_encounter_scenarios(
    reconstruction_report: Mapping[str, Any],
    base_request: Mapping[str, Any],
    *,
    armor_hypotheses: Iterable[int | float],
    level_hypotheses: Iterable[int],
    base_request_label: str | None = None,
) -> JSONMap:
    """Build a compact catalog of independent duration-mode requests."""

    report = _mapping(reconstruction_report, "reconstruction_report")
    if report.get("kind") != "chronicle_encounter_reconstruction_v1":
        raise ScenarioCompileError(
            "reconstruction_report.kind must be chronicle_encounter_reconstruction_v1"
        )
    base = _mapping(base_request, "base_request")
    target_template = _base_target_template(base)
    armors, levels = _validate_hypotheses(
        armor_hypotheses, level_hypotheses
    )

    scenarios: list[JSONMap] = []
    variant_counts: Counter[str] = Counter()
    encounters = _array(report.get("encounters"), "reconstruction_report.encounters")
    for encounter_index, raw_encounter in enumerate(encounters, start=1):
        encounter = _mapping(raw_encounter, f"encounters[{encounter_index - 1}]")
        encounter_id = encounter.get("encounter")
        instance_id = encounter.get("instance")
        waves = _array(
            encounter.get("waves"), f"encounters[{encounter_index - 1}].waves"
        )
        for wave_index, raw_wave in enumerate(waves, start=1):
            wave = _mapping(raw_wave, f"encounter[{encounter_index}].waves[{wave_index - 1}]")
            wave_id = wave.get("wave_id") or f"{encounter_id}:wave:{wave_index}"
            duration_ms = _integer(wave.get("duration_ms"), f"{wave_id}.duration_ms")
            if duration_ms < 0:
                raise ScenarioCompileError(f"{wave_id}.duration_ms must be nonnegative")
            targets_by_guid = _target_index(wave)
            layouts = _spatial_layouts(wave)
            for layout in layouts:
                guids = layout["target_guids"]
                source_targets = [targets_by_guid[guid] for guid in guids]
                proxies, weight = _kill_budget_metadata(source_targets)
                for armor in armors:
                    for level in levels:
                        variant = str(layout["variant"])
                        variant_counts[variant] += 1
                        scenario_id = (
                            f"{_slug(instance_id)}__{_slug(encounter_id)}__"
                            f"wave-{wave_index}__{variant}-pile-{layout['pile_ordinal']}__"
                            f"armor-{_slug(armor)}__level-{level}"
                        )
                        request = _request_for_scenario(
                            base,
                            target_template,
                            source_targets,
                            duration_ms=duration_ms,
                            armor=armor,
                            level=level,
                        )
                        scenarios.append(
                            {
                                "scenario_id": scenario_id,
                                "source": {
                                    "instance": instance_id,
                                    "encounter": encounter_id,
                                    "wave_id": wave_id,
                                    "wave_ordinal": wave.get("ordinal", wave_index),
                                    "wave_first_anchor": wave.get("first_anchor"),
                                    "wave_last_anchor": wave.get("last_anchor"),
                                    "target_anchors": _target_source_anchors(source_targets),
                                },
                                "pile": {
                                    "layout_variant": variant,
                                    "sensitivity_family": str(wave_id),
                                    "layout_side": (
                                        "upper"
                                        if variant == "stacked_upper"
                                        else (
                                            "lower"
                                            if variant
                                            in {
                                                "separated_component",
                                                "separated_singleton",
                                            }
                                            else "evidence_bounded"
                                        )
                                    ),
                                    "pile_ordinal": layout["pile_ordinal"],
                                    "target_guids": list(guids),
                                    "target_count": len(guids),
                                    "spatial_assumption": layout["spatial_assumption"],
                                },
                                "duration": {
                                    "seconds": _render_number(duration_ms / 1000.0),
                                    "observed_span_ms": duration_ms,
                                    "status": "RECONSTRUCTED",
                                    "source": "wave.end_offset_ms - wave.start_offset_ms",
                                    "not_exact_pull_duration": True,
                                },
                                "target_hypotheses": {
                                    "armor": {
                                        "value": armor,
                                        "status": "INFERRED",
                                        "role": "explicit_sensitivity_parameter",
                                        "source": "compiler_argument",
                                        "not_identified_by_chronicle": True,
                                    },
                                    "level": {
                                        "value": level,
                                        "status": "INFERRED",
                                        "role": "explicit_sensitivity_parameter",
                                        "source": "compiler_argument",
                                        "not_identified_by_chronicle": True,
                                    },
                                },
                                "kill_budget_proxies": proxies,
                                "weight": weight,
                                "request": request,
                            }
                        )

    return {
        "schema_version": 1,
        "kind": "fury_encounter_scenario_catalog_v1",
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "source": {
            "reconstruction_kind": report.get("kind"),
            "reconstruction_generated_at": report.get("generated_at"),
            "normalized_file": (
                report.get("source", {}).get("normalized_file")
                if isinstance(report.get("source"), Mapping)
                else None
            ),
            "base_request": base_request_label,
            "raw_rows_copied": False,
        },
        "hypothesis_grid": {
            "armor": [
                {
                    "value": value,
                    "status": "INFERRED",
                    "role": "sensitivity",
                    "not_identified_by_chronicle": True,
                }
                for value in armors
            ],
            "level": [
                {
                    "value": value,
                    "status": "INFERRED",
                    "role": "sensitivity",
                    "not_identified_by_chronicle": True,
                }
                for value in levels
            ],
        },
        "simulator_contract": {
            "mode": "duration",
            "request_grain": "one independent request per wave/pile/layout/hypothesis",
            "use_health": False,
            "individual_target_death_modeled": False,
            "kill_budget_role": "metadata weighting only; never request target health",
            "position_role": "co-hit stacking or explicit upper/lower sensitivity; never observed coordinates",
            "layout_weighting": (
                "upper and lower requests sharing a sensitivity_family are alternatives; "
                "do not add them together as independent observed waves"
            ),
            "target_template_normalization": (
                "target stats and NPC combat identity are cleared; only explicit armor/level "
                "hypotheses and observed display names are applied"
            ),
        },
        "summary": {
            "encounter_count": len(encounters),
            "wave_count": sum(
                len(_array(_mapping(value, "encounter").get("waves"), "encounter.waves"))
                for value in encounters
            ),
            "scenario_count": len(scenarios),
            "variant_counts": dict(sorted(variant_counts.items())),
            "request_payloads_are_independent": True,
        },
        "scenarios": scenarios,
    }


def _load_json(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioCompileError(f"could not read {label} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioCompileError(f"invalid JSON in {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScenarioCompileError(f"{label} must contain one JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconstruction", type=Path, required=True)
    parser.add_argument("--base-request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--armor-hypothesis", type=float, action="append", required=True
    )
    parser.add_argument(
        "--level-hypothesis", type=int, action="append", required=True
    )
    args = parser.parse_args(argv)
    report = _load_json(args.reconstruction, "reconstruction report")
    base = _load_json(args.base_request, "base request")
    catalog = compile_fury_encounter_scenarios(
        report,
        base,
        armor_hypotheses=args.armor_hypothesis,
        level_hypotheses=args.level_hypothesis,
        base_request_label=str(args.base_request.resolve()),
    )
    _write_json(args.output.resolve(), catalog)
    print(json.dumps(catalog["summary"], ensure_ascii=False, sort_keys=True))
    return 0


__all__ = (
    "ARMOR_STAT_INDEX",
    "HEALTH_STAT_INDEX",
    "ScenarioCompileError",
    "compile_fury_encounter_scenarios",
)


if __name__ == "__main__":
    raise SystemExit(main())
