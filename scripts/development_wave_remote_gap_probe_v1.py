"""Read only compact diagnostics from the staged node001 development panels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys


PROJECT = (
    "/home/zhengliang01/scheduleurm_work/o2o-dps-hpc/runs/"
    "development-wave-61944-v1/attempt-20260913-003/AddOns/BrainOfCat/o2o-dps"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("schema", "aggregate"), default="schema")
    args = parser.parse_args()
    sys.path.insert(0, str(Path.home() / "mine_code/scheduleurm/skill"))
    import scheduler  # type: ignore[import-not-found]

    if args.mode == "schema":
        code = '''
import json, pathlib
p = pathlib.Path("results")
for name in ("anchor_13d-full32.json", "cat_residual-full32.json", "stratified-20260913-32.json"):
    x = json.loads((p/name).read_text())
    print(name, json.dumps({"keys":list(x), "sample":str(x)[:1200]}, ensure_ascii=False))
    if name.startswith("stratified"):
        first = x["panels"][0] if x["panels"] else {}
        print("stratified_shape", json.dumps({"reduction_keys":list(x["reduction"]), "panel_count":len(x["panels"]), "first_panel_keys":list(first), "first_row_keys":list(first["rows"][0]) if first.get("rows") else [], "first_row_sample":str(first["rows"][0])[:1000] if first.get("rows") else ""}, ensure_ascii=False))
f = next((p/"panels").glob("seed-*/*.json"))
x = json.loads(f.read_text())
print("panel", str(f), json.dumps({"keys":list(x), "rows":x.get("rows")}, ensure_ascii=False)[:1500])
'''
    else:
        code = '''
import collections, json, math, pathlib, statistics
p = pathlib.Path("results")
cat = "cat.fury.profile1"
contra = "contra.deployed.fury.raid_a"
new = "contra260817.fury.source_candidate"
anchor = json.loads((p/"anchor_13d-full32.json").read_text())
resid = json.loads((p/"cat_residual-full32.json").read_text())
strata = json.loads((p/"stratified-20260913-32.json").read_text())
def summarize(values):
    return {"n":len(values), "positive":sum(v>1e-8 for v in values), "negative":sum(v<-1e-8 for v in values), "ties":sum(abs(v)<=1e-8 for v in values), "mean":statistics.mean(values), "se":statistics.stdev(values)/math.sqrt(len(values)) if len(values)>1 else 0, "min":min(values), "max":max(values)}
def rows(panel):
    return {r["policy_id"]:r for r in panel["rows"]}
profiles = []
for result in anchor["candidate_results"]:
    ident = result["candidate_id"]
    differences = {other:[] for other in (cat,contra,new,anchor["incumbent_candidate_id"])}
    ttk = []
    for seed in anchor["master_seeds"]:
        panel = json.loads((p/"panels"/f"seed-{seed}"/f"{ident}.json").read_text())
        r = rows(panel)
        for other in differences:
            if other == anchor["incumbent_candidate_id"]:
                incumbent = json.loads((p/"panels"/f"seed-{seed}"/f"{other}.json").read_text())
                score = rows(incumbent)[other]["own_effective_damage"]
            else:
                score = r[other]["own_effective_damage"]
            differences[other].append(r[ident]["own_effective_damage"] - score)
        ttk.append(r[ident]["ttk_ms"] - r[cat]["ttk_ms"])
    profiles.append({"id":ident,"parameters":result.get("parameters"),"vs":{k:summarize(v) for k,v in differences.items()},"ttk_minus_cat":summarize(ttk),"cat_extremes":sorted(zip(anchor["master_seeds"],differences[cat]),key=lambda x:x[1])[:2]+sorted(zip(anchor["master_seeds"],differences[cat]),key=lambda x:x[1])[-2:] if ident==anchor["next_candidate_id"] else None})
residual_profiles = []
for result in resid["candidate_results"]:
    d = result["reserve_discount_rage"]
    diffs = {other:[] for other in (cat,contra,new)}
    ttk = []
    for seed in resid["seeds"]:
        panel = json.loads((p/"panels-residual"/f"seed-{seed}"/f"discount-{d:g}.json").read_text())
        r = rows(panel)
        cand = r["cat_residual_candidate/v1"]
        for other in diffs:
            diffs[other].append(cand["own_effective_damage"] - r[other]["own_effective_damage"])
        ttk.append(cand["ttk_ms"] - r[cat]["ttk_ms"])
    residual_profiles.append({"discount":d,"vs":{k:summarize(v) for k,v in diffs.items()},"ttk_minus_cat":summarize(ttk),"changed_seeds":[{"seed":seed,"delta":round(delta,3)} for seed,delta in zip(resid["seeds"],diffs[cat]) if abs(delta)>1e-8] if d in (5.0,15.0) else None})
stratified_profiles = []
for reduction in strata["reduction"]["rows"]:
    stratified_profiles.append({"stratum":reduction["stratum"], "comparison_scope":reduction["comparison_scope"], "matched":reduction["matched_complete_seed_count"], "candidate_minus_baselines":reduction["candidate_minus_baselines"]})
print(json.dumps({"anchor":profiles,"residual":residual_profiles,"stratified":stratified_profiles}, ensure_ascii=False))
'''
    remote_python = "/home/zhengliang01/scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
    command = "cd " + shlex.quote(PROJECT) + " && " + remote_python + " -c " + shlex.quote(code)
    rc, stdout, stderr = scheduler.run_on("node001", command, timeout=60, check=False)
    if rc:
        raise RuntimeError(stderr)
    print(stdout)


if __name__ == "__main__":
    main()
