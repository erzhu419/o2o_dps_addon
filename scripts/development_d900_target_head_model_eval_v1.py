"""Score the new reduced target head on the held-out d900 Stage-5 instance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from o2o_dps.chronicle_external_teammate_response_model_v1 import ABLATION_D
from o2o_dps.responsive_team_hpc_result_loader_v1 import CurrentSourceDeclarationV1
from o2o_dps.responsive_team_runtime_store_v1 import open_responsive_team_runtime_store_v1
from o2o_dps.upper_kara_target_head_heldout_eval_v1 import evaluate_heldout_stage5_gzip_v1


D900_STAGE5_SHA = "75fe0c215d09b6b0ca5e3a2c46b5397f70b4b5de60f8740dceae1b2465576bc0"
D900_COMPONENT = "114175c6c5af0b03978551442d51dcbbf325f8ba2af8b7e3bef468bc7da8fa68"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage5", type=Path, required=True)
    parser.add_argument("--runtime-store", type=Path, required=True)
    parser.add_argument("--result-sha", required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
    loaded = open_responsive_team_runtime_store_v1(
        args.runtime_store,
        expected_result_content_sha256=args.result_sha,
        expected_model_content_sha256=args.model_sha,
        variant_id=ABLATION_D,
        current_source=CurrentSourceDeclarationV1(
            stage5_content_sha256=D900_STAGE5_SHA,
            component_id=D900_COMPONENT,
            declared_held_out=True,
        ),
    )
    try:
        result = evaluate_heldout_stage5_gzip_v1(args.stage5, model=loaded.model)
        result["new_reduced_model_binding"] = {
            "model_content_sha256": args.model_sha,
            "source_result_content_sha256": args.result_sha,
            "variant_id": ABLATION_D,
            "heldout_instance_excluded_by_runtime_store_binding": True,
        }
        print(json.dumps(result, ensure_ascii=False))
    finally:
        loaded.model.close()


if __name__ == "__main__":
    main()
