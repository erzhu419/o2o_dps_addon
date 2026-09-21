"""Small fit-only v8 white-head artifact; never includes the v7 joint tables."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any, Mapping

from .development_white6603_joint_training_v8 import SCHEMA as FIT_SCHEMA
from .development_white6603_runtime_head_v8 import DevelopmentWhite6603CountHeadV8
from .development_white6603_selection_eval_v8 import CANDIDATE_ALPHA, WHITE_MINIMUMS


SCHEMA = "development_white6603_fit_only_runtime_head/v8"
SELECTION_SCHEMA = "development_white6603_v8_teacher_forced_selection/v1"
STATUS = "INTRA_COMPONENT_FIT_ONLY_DEVELOPMENT"


def build_selected_white6603_head_artifact_v8(
    fit: Mapping[str, Any], selection: Mapping[str, Any]
) -> dict[str, Any]:
    if fit.get("schema") != FIT_SCHEMA:
        raise ValueError("v8 fit-only merged count schema differs")
    if (
        selection.get("schema") != SELECTION_SCHEMA
        or selection.get("status") != "PASS_INTRA_COMPONENT_DEVELOPMENT_GATE"
    ):
        raise ValueError("v8 selection gate did not pass")
    candidate = selection.get("selected_candidate")
    if candidate not in CANDIDATE_ALPHA:
        raise ValueError("v8 selected alpha candidate is not frozen")
    fit_ids = fit["fit_instance_ids"]
    selection_ids = selection["selection_instance_ids"]
    if (
        len(fit_ids) != selection["fit_task_count"]
        or len(set(fit_ids)) != len(fit_ids)
        or set(fit_ids) & set(selection_ids)
        or fit["row_count"] != selection["fit_count_rows"]
        or fit["white_count"] != selection["fit_count_white"]
    ):
        raise ValueError("v8 selected head fit-only membership or counts differ")
    # The source has a large v7 joint table; keep only the bounded v8 contexts.
    rows = fit["white_opportunity_counts"]
    return {
        "schema": SCHEMA,
        "status": STATUS,
        "selected_candidate": candidate,
        "alpha": CANDIDATE_ALPHA[candidate],
        "minimums": dict(WHITE_MINIMUMS),
        "fit_instance_ids": list(fit_ids),
        "selection_instance_ids": list(selection_ids),
        "fit_row_count": fit["row_count"],
        "fit_white_count": fit["white_count"],
        "white_context_count": len(rows),
        "white_opportunity_counts": [
            {
                "context": list(row["context"]),
                "white": row["white"],
                "nonwhite": row["nonwhite"],
            }
            for row in rows
        ],
    }


def selected_white6603_head_from_artifact_v8(
    artifact: Mapping[str, Any],
) -> DevelopmentWhite6603CountHeadV8:
    if artifact.get("schema") != SCHEMA or artifact.get("status") != STATUS:
        raise ValueError("unsupported selected v8 white-head artifact")
    candidate = artifact.get("selected_candidate")
    if (
        candidate not in CANDIDATE_ALPHA
        or artifact.get("alpha") != CANDIDATE_ALPHA[candidate]
        or artifact.get("minimums") != WHITE_MINIMUMS
    ):
        raise ValueError("selected v8 white-head alpha or backoff differs")
    counts: dict[tuple[Any, ...], Counter[bool]] = {}
    for row in artifact["white_opportunity_counts"]:
        context = tuple(row["context"])
        if context in counts or context[0] not in WHITE_MINIMUMS:
            raise ValueError("selected v8 white-head context differs")
        counts[context] = Counter({True: row["white"], False: row["nonwhite"]})
    if len(counts) != artifact["white_context_count"]:
        raise ValueError("selected v8 white-head context count differs")
    global_rows = sum(
        sum(value.values()) for key, value in counts.items() if key[0] == "GLOBAL"
    )
    global_white = sum(
        value[True] for key, value in counts.items() if key[0] == "GLOBAL"
    )
    if (
        global_rows != artifact["fit_row_count"]
        or global_white != artifact["fit_white_count"]
    ):
        raise ValueError("selected v8 white-head global support differs")
    return DevelopmentWhite6603CountHeadV8(
        counts, minimums=artifact["minimums"], alpha=artifact["alpha"]
    )


def load_selected_white6603_head_v8(path: str | Path) -> DevelopmentWhite6603CountHeadV8:
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    return selected_white6603_head_from_artifact_v8(artifact)
