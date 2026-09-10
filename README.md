# o2o-dps

`o2o-dps` is the Windows-native data, simulator, search, and policy-compiler
side of BrainOfCat. Python uses only the standard library. The simulator bridge
is a pure-Go Windows executable built from the local `wowsims-turtle` tree.

## Public source tree and local inputs

This repository is source-only. It excludes `offline_data/`, compiled binaries,
and the Cat, Cat2, Contra, Contra_new, wowsims-turtle, and DPSSim project trees.
See [`NOTICE.md`](NOTICE.md) for the provenance boundary. Local character paths
are supplied by CLI options or the `BOC_*` environment variables documented in
[`.env.example`](.env.example); account, realm, and character identifiers are
not stored in the repository.

Run the standalone standard-library test set from a clean checkout with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test_source_windows.ps1
```

The complete local integration suite additionally needs the excluded simulator,
BrainOfCat/Cat2 addon trees, calibration artifacts, and portable Go toolchain:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test_windows.ps1 `
  -SimulatorRoot D:\path\to\wowsims-turtle `
  -AddonRoot D:\path\to\BrainOfCat `
  -DeployedCat2Root D:\path\to\Cat2
```

Implemented today:

- official Chronicle **All Activity** CSV preservation and normalization;
- WoW `BrainOfCat.lua` SavedVariables preservation and normalization;
- a persistent `o2obridge.exe` process with state, exact tagged actions,
  action masks, action application, wait, and event advance;
- fixed-seed prefix reconstruction and bounded queue+GCD beam search;
- JSON policy validation and legacy-Lua generation for the root addon.

None of the simulator policies are marked calibrated or better than an expert.

## Build the first Fury Chronicle partial trajectory

The normalized Chronicle archive can now be converted into a player-scoped,
versioned partial trajectory without modifying the raw or normalized inputs:

```powershell
py -3 -B -m o2o_dps.chronicle_fury_trajectory `
  --instance 043b4d65-9c58-4643-b601-62f1684af04a `
  --player-guid 0x000000000066ADAE `
  --player-name Narcissly
```

The JSONL keeps `START` as a candidate and links `GO/FAIL` only when the
preceding exact candidate is unique. Damage/death rewards, rage gain evidence,
combatant context, consumes, and field-level provenance are retained. See the
bilingual contract and limitations in
[`CHRONICLE_FURY_TRAJECTORY.md`](CHRONICLE_FURY_TRAJECTORY.md).

## Build conservative Fury decision windows

The first bounded behavior slice converts one identity-verified encounter from
the partial trajectory into one row per Chronicle `START` candidate:

```powershell
py -3 -B -m o2o_dps.chronicle_fury_decision_windows `
  --trajectory .\offline_data\derived\chronicle_fury_partial_trajectory\v1\043b4d65-9c58-4643-b601-62f1684af04a__000000000066ADAE.jsonl `
  --trajectory-manifest .\offline_data\derived\chronicle_fury_partial_trajectory\v1\043b4d65-9c58-4643-b601-62f1684af04a__000000000066ADAE.manifest.json `
  --readiness-report .\offline_data\reports\chronicle_warrior_reconstruction_readiness.json `
  --encounter 6dcc1914-1742-4962-8a8c-c89a1b2539b1
```

Only uniquely successful candidates that map to the positive active-action
catalog in the mechanics registry become observable behavior labels. Unmapped
server visuals and known on-swing actions remain records but are excluded from
labels because Chronicle does not expose keypress or queue intent. State fields
not supported by earlier evidence remain `null` with a false mask, and damage
between adjacent decisions is an observation rather than a causal reward.

The current bounded output contains 82 `START` candidates, of which 74 map to
the active-action catalog and 53 are observable labels. Its manifest verifies
zero future-state leakage, zero queue-intent labels and zero causal-reward
claims. It is suitable for partial-observation behavior analysis only, not
full-state behavior cloning or offline RL.

## Build the compact multi-player Fury dataset

After the reconstruction-readiness report has been reviewed, build the batch
artifact directly from normalized JSONL without first materializing one partial
trajectory per player:

```powershell
py -3 -B -m o2o_dps.chronicle_fury_decision_dataset `
  --readiness-report .\offline_data\reports\chronicle_warrior_reconstruction_readiness.json `
  --workers 4
```

Selection is strict: the readiness row must have a verified identity and at
least one leaderboard row whose `board_spec` is exactly `Fury`; Arms and
unverified rows are excluded. Each normalized source is streamed once for all
of its selected players and becomes one compact `jsonl.gz` partition under
`offline_data/derived/chronicle_fury_decision_dataset/v1/`. The dataset manifest
records source/selection counts and every partition. State masks and event/CSV
provenance anchors are retained. GO/FAIL labels remain uniqueness-gated, while
client queue intent and causal reward stay `MISSING`; observed damage between
two START candidates is not promoted to an offline-RL reward.

`o2o_dps.o2o_observation.project_decision_record()` provides the in-memory
`O2OObservationV1` adapter for these rows. It emits one
`{value, status, provenance}` object per fixed field, validates every evidence
anchor against the decision START, and keeps unavailable fields explicitly
`MISSING`. The contract is partial observation only; it does not materialize a
second dataset or use result windows, behavior labels, or leaderboard identity
as policy state. `schema_descriptor()` exposes the machine-readable contract.

`o2o_dps.state_reconstructor_v1.iter_reconstructed_observations()` consumes a
chronological compact-decision iterator once and performs the first strictly
prefix-only upgrade in memory. It carries Warrior stance forward only after a
uniquely associated successful Battle, Defensive, or Berserker Stance `GO`,
and never applies that `GO` to its own or an earlier `START`. Failed or
ambiguous results do not establish stance. GCD, cooldown, rage, swing timers,
queue intent, and the other unavailable fields remain `MISSING`; V1 does not
rewrite partitions or create another derived dataset.

### Audit the observed interval reward contract

Audit the compact rows without creating a reward-row copy:

```powershell
py -3 -B -m o2o_dps.fury_observed_interval_reward_v1
```

The audit streams every compressed partition once and buffers only the
preceding decision for each instance x encounter x player trajectory. For each
non-final row it verifies that the declared `next_start_anchor` exactly matches
the actual following START, that event indices increase, and that offsets do
not move backward. `observed_interval_outgoing_damage` is the raw outgoing
damage in `[current START, next START)`. It is an observed variable-duration
SMDP/POMDP transition outcome, not damage causally attributed to the preceding
action.

Only mapped `gcd` or `off_gcd` actions with a uniquely associated successful
result, verified damage observation, and an actual next observation are marked
`reward_eligible_success`. On-swing STARTs remain unusable as queue-intent
labels; failed, ambiguous, missing, unmapped, final, and structurally invalid
rows receive mutually exclusive exclusion reasons. The compact manifest and
partitions are not modified. The only artifact is the aggregate report
`offline_data/reports/fury_observed_interval_reward_v1.json`; its eligible plus
excluded counts must close exactly to the decision-row count. It reports
count/sum/min/max/mean `delta_t_ms` separately for structurally observed and
reward-eligible intervals, without retaining those intervals as rows. Direct
causal attribution stays `MISSING`, `causal_reward_rows` stays zero, and
offline RL remains blocked pending terminal outcomes, boss/add effective-damage
weights, queue/failure semantics, the missing rage/timer state, and a separate
causal factorized-action contract. The reward audit itself never merges START
rows.

### Audit factorized Fury decision epochs

Build the roadmap action tuple without creating another row dataset:

```powershell
py -3 -B -m o2o_dps.fury_factorized_decision_epoch_v1
```

V1 groups START candidates only when `offset_ms` is exactly equal and the
instance, encounter, and player GUID are identical. It deliberately uses a
zero-millisecond tolerance rather than guessing that two merely nearby casts
came from one client decision. The projection reads only compact-row schema,
identity, `source.start_anchor`, and `action`; it never reads later result or
reward windows, eligibility labels, or state fields. Every nested provenance
anchor must be at or before the epoch cutoff and remain inside the same exact
time and trajectory boundary.

Each in-memory epoch has
`(gcd, swing_queue, off_gcd, stance, target)` lanes with explicit
`OBSERVED`, `UNKNOWN`, or `MISSING` status. Known stance STARTs are removed
from the generic off-GCD lane and projected into `stance`. A resolved nonself
spell target may be retained as observed target evidence, but is labelled as
the server-resolved target, not target-switch intent. Heroic Strike/Cleave
STARTs remain execution evidence only: `swing_queue` is always `MISSING`
because Chronicle cannot reveal the earlier client queue intent. Duplicate
same-lane candidates, unmapped actions, and conflicting targets stay
`UNKNOWN` or excluded with their START provenance rather than being guessed.

The production audit conserves all 76,257 compact rows into 74,890 epochs:
1,352 epochs contain multiple exact-time STARTs, accounting for 1,367 grouped
extra rows, while only 9 epochs expose two distinct mapped action lanes in
parallel. The only written artifact is
`offline_data/reports/fury_factorized_decision_epoch_v1.json`; the compact
partitions remain unchanged and no line-level epoch output is materialized.
This closes the structural factorized-epoch contract, not the offline-RL gate:
queue intent, result-confirmed execution, target-switch intent, full timer and
resource state, and causal terminal-weighted reward remain unavailable.

### Add the Chronicle CombatantInfo sidecar

The compact manifest currently references 23 source instances. Fetch only the
small official `combatant_info` stream for those instances with:

```powershell
py -3 -B -m o2o_dps.chronicle_combatant_sidecar --workers 4
```

The command resolves each public slug from `chronicle_raw/export_queue.json`,
requests each unique source once, and reuses an existing raw `.events.gz` by
default. Raw responses are kept under
`offline_data/chronicle_raw/external_api/combatant_info/`; per-instance
`jsonl.gz` sidecars and their manifest go under
`offline_data/derived/chronicle_combatant_info_sidecar/v1/`. The manifest reports
reused, downloaded, 404, decoded, encounter/message, and byte totals. A 404 is
recorded as an unavailable stream; every other HTTP failure stops the build.

Each compressed JSONL row retains one original INFO message, including repeated INFO for a
player, full ordered item/enchant/temporary-enchant/gem data, and the exact
talent summary/rank strings. Use
`select_latest_at_or_before_start()` to choose only the latest matching INFO
whose event index is at or before a decision START. This is a causal static
profile sidecar, not a full-state reconstruction, and it does not rewrite the
76,257 compact decision rows.

Audit the causal join over all compact decisions with:

```powershell
py -3 -B -m o2o_dps.combatant_context_join
```

The join streams each `jsonl.gz` decision partition and uses only INFO from the
same instance, encounter, and player GUID whose event index is at or before the
decision START. In memory, it may promote only `gear_item_ids` and
`exact_talent_ranks` to `OBSERVED`, retaining the INFO anchor as provenance.
Late INFO, GUID mismatches, unavailable streams, and the 404 instance remain
`MISSING`; the current live character profile is never used for historical
players. No enriched row dataset is written. The only output is the small
aggregate report
`offline_data/reports/fury_combatant_context_coverage_v1.json`. Its mutually
exclusive join outcomes distinguish unavailable instances, missing encounters,
GUID mismatches, late-only INFO, unanchored-only INFO, and causal matches; their
sum must equal the streamed decision-row count.

### Audit Fury timer-transition readiness

Before attempting another L2 state reconstruction, inventory the timer contract
without opening any compact decision partition:

```powershell
py -3 -B -m o2o_dps.fury_timer_transition_contract_v1
```

The audit reads the Warrior mechanics registry, the existing Fury action-lane
registry, the Phase12 evidence review/comparison, the strict live
`offline_data/timer_calibration_summaries/fury_timer_source_recovery_summary_v1.json`
contract, and a bounded set of simulator source rules. The live input must have
the exact v1 schema/kind, `status=complete`, an exact source/recovery run link,
14/14 composite checks, all four A-D stage gates, and
`live_contract_complete=true`. It evaluates `gcd_remaining_ms`,
`cooldown_remaining_ms`, `mainhand_swing_remaining_ms`, and
`offhand_swing_remaining_ms` separately. Each field still requires complete
duration, causal timer-start, reset/cancel, rank/talent/build applicability, and
provenance components before `reconstruction_allowed` can become true.

The current report is
`offline_data/reports/fury_timer_transition_contract_v1.json`. Stage A promotes
the current-build GCD/Bloodthirst start and failed-retry rules to
`LIVE_CALIBRATED_PARTIAL`; Stage B does the same for the fixed-loadout independent
MH/OH intervals and stop/restart observations. Stage D closes the same-GUID
Heroic Strike cancel transition, including the next white MH and continuing OH;
the current Warrior, interactive environment, and bridge expose the aligned
explicit `cancel_queue` lane, while the real two-target switch remains
`EXTERNAL_HOLD`. Stage C retains the historical comparison as
`PRE_PATCH_SIMULATOR_CONTRADICTED_BY_LIVE_FLURRY`: the already-scheduled opposite
hand is closer to its unchanged deadline at both add and remove boundaries. The
current Warrior source uses the white-swing boundary rule and live Flurry aura
IDs 12966 through 12970, so that path is
`CURRENT_WHITE_SWING_FLURRY_ALIGNED_WITH_LIVE`; ordinary and non-white-boundary
haste still uses the generic proportional rescale and remains outside this live
calibration. All four
historical remaining-time reconstructions stay blocked because Chronicle lacks
the required client anchor, initial hand deadlines, exact loadout/haste state,
queue intent, and target-selection state. The audit reads zero compact rows,
writes no line-level state, and starts neither behavior cloning nor offline RL.

Source inventory matches inside Go block comments are excluded. In particular,
the Slam cooldown-reset snippet under the commented SoD item-set section is not
treated as active Turtle simulator behavior; cooldown reconstruction remains
blocked by the independent live start/shared-timer/reset coverage gaps.

## Fit the partial-observation Fury behavior prior

After building and reviewing the compact dataset, fit the standard-library
empirical/backoff Markov baseline with:

```powershell
py -3 -B -m o2o_dps.chronicle_fury_behavior_prior `
  --dataset-manifest .\offline_data\derived\chronicle_fury_decision_dataset\v1\manifest.json
```

The trainer uses only rows with `eligibility.partial_observation_bc=true`. Its
features are at most the two preceding uniquely linked server actions plus an
explicit present/missing bucket for elapsed time since the preceding observed
Auto Attack. It performs deterministic five-fold cross-validation over
source/player connected components and reports held-out top-1, top-3, log loss,
macro recall, per-class support, confusion and context coverage for global,
last-action and two-action priors.

Outputs are written under
`offline_data/behavior_models/fury_partial_markov_v1/`: `model.json`,
`evaluation.json`, and `MODEL_CARD.md`. The action-key-to-Cat2-card mapping is
explicit and validated against the Warrior card source. These artifacts remain
analysis-only: they do not use queue intent or causal reward, are not full-state
behavior cloning or offline RL, do not establish a DPS improvement, and are not
accepted by the policy compiler for deployment.

## Fit the Chronicle L2 prefix-context behavior prior

The L2 sidecar can be evaluated without reopening normalized JSONL or raw CSV:

```powershell
py -3 -B -m o2o_dps.chronicle_fury_l2_context_prior `
  --l2-manifest .\offline_data\derived\chronicle_fury_l2_temporal_join\v1\manifest.json
```

The reader streams each original decision gzip beside its L2 gzip and accepts a
label only after `source_instance_ref`, `encounter_id`, and `decision_id` match
exactly; START offset/event index, player GUID, and spell ID are checked as
additional alignment witnesses. The candidate prior consumes only the
observed-so-far target-count bucket, strictly-prior positive melee-reach co-hit
evidence, wave-elapsed bucket, and the last two uniquely linked successful
server actions. A missing co-hit field means no positive evidence has appeared;
it is never converted into a claim that targets were separated. Final wave
count/duration, previous duration, kill budget/HP, rage, timers, queue intent,
future outcomes, and causal reward are not consumed or filled.

The committed run exact-joined 76,257/76,257 rows and retained 40,175 eligible
labels. Five-fold evaluation is source-instance-disjoint, with zero instance
overlap in every fold; no thresholds were tuned. The result is a useful negative
control, not an improvement:

| Prior | Top-1 | Top-3 | Log loss | Macro recall |
|---|---:|---:|---:|---:|
| paired legacy Markov2 replay | 0.5363 | 0.8115 | 1.4329 | 0.2524 |
| successful suffix2 only | 0.4921 | 0.8117 | 1.4903 | 0.2366 |
| L2 context + successful suffix2 | 0.4899 | 0.8031 | 1.5195 | 0.2345 |

Therefore the L2 prior does not replace the legacy behavior prior. Its
environment fields remain available for stratified analysis, while deployment
stays disabled. Outputs under
`offline_data/behavior_models/fury_l2_context_prior_v1/` contain only compact
count tables, aggregate/fold metrics, and a model card; there are no per-row
predictions, rollout rows, or checkpoints.

## Build and test on Windows

From this directory:

```powershell
.\scripts\setup_windows.ps1
.\scripts\build_simulator_windows.ps1
.\scripts\test_windows.ps1
```

The build script defaults to `bin\o2obridge.seedfix-v1.exe`, stages the build
under a temporary name, and refuses to replace different existing bytes. It
also refuses to overwrite the frozen legacy `bin\o2obridge.exe`. Fixture
generation is disabled by default; if explicitly requested it requires a new
versioned filename and cannot replace `fury_warrior_phase1.json`. Verify the
pinned legacy bridge, literal-seed bridge, and historical fixture with
`py -B -m o2o_dps.simulator_binary_contract_v1`.

Run the committed simulator example:

```powershell
py -B -m o2o_dps.search_cli `
  .\configs\wowsims\fury_warrior_phase1.json `
  --bridge .\bin\o2obridge.seedfix-v1.exe `
  --seed 123 --depth 3 --beam-width 4 `
  --output .\offline_data\sim_teacher\candidate.json
```

The default Fury allowlist is Heroic Strike, Bloodthirst, Whirlwind, and
Execute. Exact `tag=1` distinguishes the Heroic Strike queue action from the
eventual swing. Other non-GCD actions are not silently treated as queue actions.

Compile a reviewed policy JSON with:

```powershell
py -B -m o2o_dps.compile_policy `
  .\policies\fury_seed.json `
  ..\addon\GeneratedPolicy.lua
```

## Import WoW online data

For the current Fury Phase 4 campaign, enable BrainOfCat, Cat2 and Nampower. The
existing `BoC标定` macro can be reused; if it is missing, run `/boccal macro`
once, place it on an action-bar slot, bind it, target the dummy, and repeatedly
press that same key. Its body is `/boccal press`: every cast remains
hardware-triggered, while campaign start, task/trial transitions, logging and
prompts are automatic. Phase 4 collects four normal Bloodthirst hits without
Battle Shout and four with Battle Shout. The same key removes or casts the buff,
and the addon locks the effective AP observed in each stratum plus one target
GUID and armor value. Crits, misses, non-normal hit encodings, and context drift
are retained but do not advance the eight-sample counter. It survives `/reload`
by restarting only the unfinished trial. Cat and Contra are not needed for this
campaign. Expert collection remains separately opt-in through `/bocexpert start`.

After `CALIBRATION_CAMPAIGN_COMPLETED`, the logger stops and the in-game panel
requests one final `/reload`. The Windows watcher can then snapshot, import and
decode the SavedVariables automatically. Start it once with the exact character
file; `-Status` can be used without disturbing the running watcher:

```powershell
.\scripts\start_calibration_watch_windows.ps1 `
  -SavedVariablesPath 'D:\WOW\WTF\Account\<account>\<realm>\<character>\SavedVariables\BrainOfCat.lua'

.\scripts\start_calibration_watch_windows.ps1 -Status
```

The same stable-file pass also publishes every unseen event-confirmed,
counted schema-v2 Shadow envelope, even when no new calibration campaign is
present. The current strict inner contract is schema-v4 and the smoke target is
12 causally complete actions. Pending macro evaluations, failed actions,
expired observations, and confirmed rows beyond that session target are
ignored. `status.json` retains the
existing campaign/timer fields and adds `shadow_pairs` with the current source
pair total, newly imported count, journal total, and output paths. The compact
journal is
`offline_data/online_decisions/brainofcat_shadow_pairs_v2.jsonl`; its adjacent
manifest records the observed source signature and composite identities.
Deduplication uses normalized character source path plus intrinsic `decisionId`,
so two characters may both contribute `boc-decision-N`; this watcher path never
creates a second raw `BrainOfCat.lua` copy.

An already exported, completely full 15,000-row in-game calibration ring is
released on the next `PLAYER_LOGIN` before Shadow collection. Its campaign and
sequence boundary remain in SavedVariables, while the watcher retains the
latest processed campaign receipt when the compacted ring no longer contains a
terminal marker. This is the observed 74 MB write-blocking case; partial,
unexported, non-full, or structurally inconsistent rings are not compacted.

The independent Fury timer campaign is started in game with `/boccal timer`.
After that, the existing `BoC标定` macro is the only action key: the addon
guides four uninterrupted stages for spell timers, dual-wield swing anchors,
Flurry haste rescaling, and Heroic Strike queue cancellation, then restores the
original weapons. The optional two-target queue test may end as `EXTERNAL_HOLD`
without blocking the dummy campaign. Use the single requested final `/reload`
only after the in-game completion message.

For this campaign the watcher requires the generic
`CALIBRATION_CAMPAIGN_COMPLETED` terminal together with
`marker.campaignId=warrior_fury_timer_campaign_v1` and the exact non-empty
`marker.campaignRunId`. It writes a compact report under
`offline_data/timer_calibration_summaries/` and publishes it as
`timer_summary` in `status.json`, `watch.log`, and the processed-campaign state.
A timer-decoder error is retained as `timer_summary.status=summary_error`; it
does not undo the successful SavedVariables import. Other campaigns receive
`timer_summary.status=not_applicable`.

The same strict decoder can be run manually against an already imported JSONL:

```powershell
py -3 -B -m o2o_dps.fury_timer_calibration_summary `
  .\offline_data\calibration\BrainOfCat__<import-id>.jsonl `
  --campaign-run-id '<timer-campaign-run-id>'
```

The report preserves observed spellbook cooldown snapshots, per-hand swing
intervals, Flurry boundary predictions and next-anchor observations, strong
Heroic Strike cancel evidence, optional target-switch disposition, and loadout
restoration. It authorizes no simulator patch; comparison remains a separate
step.

If a stable write must be reprocessed after repairing the importer, run the
same launcher once with `-Once`; this does not require another in-game reload:

```powershell
.\scripts\start_calibration_watch_windows.ps1 `
  -SavedVariablesPath 'D:\WOW\WTF\Account\<account>\<realm>\<character>\SavedVariables\BrainOfCat.lua' `
  -Once
```

For a manual fallback only, make a snapshot of one exact character directory
from the workspace root:

```powershell
.\o2o-dps\scripts\collect_wow_savedvariables.ps1 `
  -SavedVariablesDirectory 'D:\WOW\WTF\Account\<account>\<realm>\<character>\SavedVariables'
```

Then pass the copied `BrainOfCat.lua` to:

```powershell
py -B -m o2o_dps.import_savedvariables `
  .\offline_data\online_raw\<import-id>\BrainOfCat.lua
```

The importer preserves the raw Lua file and emits ordered decision,
calibration-event, and expert-request JSONL datasets when those sections are
present. Contra Fury records preserve `policyEntered` and `policyFunction`, so
an access/profile gate that returns before the rotation is not treated as an
expert action label.

Decision decoding accepts the outer formats the addon has actually emitted:
legacy schema v1 rows and paired schema v2 envelopes. A v2 envelope preserves
`decisionId`, `activePolicy`, `expertActual`, `candidateShadow`, and
`observedActualOutcome` in `online_decisions` without executing Lua or inferring
a join. New rows also carry `shadowSample`; legacy v2 rows without it remain
readable but are not eligible for the compact Shadow journal. The current
schema-v4 causal contract adds per-sink generation/event tokens and monotonic
causal ordinals. The outcome includes a maximum of 32 compact typed-event evidence rows
and the effective telemetry availability; `serverObservedActions` preserves an
explicit queue/server-GO fallback when an actual sink was not traced. This
fallback is labeled lower-confidence and never supplies a proposal. Optional
`intervalDecisionId`/`actionDecisionId` fields on calibration rows and
`decisionId` fields on expert traces are type-checked but are not required to
point into the current 200-row decision ring, because the logger and decision
stores have different retention sizes.

For the live Shadow smoke workflow, `/boc macro` creates the `BoC Brain` entry
and the in-game top panel displays stage 1/3, `已确认实际动作 X/12`, then stage
3/3. Pressing the macro only arms the newest candidate snapshot; repeated empty
presses coalesce and never increase the count. A pair advances only once, when
typed telemetry attributes an actual skill GO/known result or a real
main/off-hand white swing to that pending decision. Queue acceptance, Cat2
return, sink invocation, and failed casts do not count. The record persists a
`shadowSample` confirmation block, and the watcher publishes it only when both
`materialized=true` and `counted=true`. At 12/12, stop pressing and use the
requested final `/reload` so the watcher can publish the compact pairs above.

For a completed schema-v4 session, the watcher also runs the strict acceptance
audit and materializes only a three-gate PASS into a content-addressed action
fragment dataset:

```powershell
py -3 -B -m o2o_dps.shadow_pair_acceptance_v1 --expected-pairs 12 `
  --export-session-id '<session-id>'
py -3 -B -m o2o_dps.fury_shadow_transition_dataset_v1 `
  --session-id '<session-id>'
```

The resulting `fury_shadow_transitions_v1.jsonl` is suitable for observed
behavior labels and immediate-outcome diagnostics only. It explicitly has no
scalar reward, candidate counterfactual outcome, complete next state, TD
transition, offline-RL episode or deployment permission.

Capture and audit the current installed Cat2 profile without executing Lua:

```powershell
py -3 -B -m o2o_dps.cat2_saved_profile_v1 `
  --savedvariables 'D:\WOW\WTF\Account\<account>\<realm>\<character>\SavedVariables\Cat2.lua' `
  --profile-name 'BrainOfCat Shadow' `
  --installed-root 'D:\WOW\Interface\AddOns\Cat2' `
  --brainofcat-root 'D:\WOW\Interface\AddOns\BrainOfCat' `
  --output .\offline_data\reports\cat2_saved_profile_v1.json

py -3 -B -m o2o_dps.fury_cat2_profile_conformance_v1
```

The profile loader pins the literal SavedVariables shape plus the 16 directly
relevant Cat2/BrainOfCat source files; it does not claim a complete transitive
dependency closure. The conformance report labels current profile identity as
`UNSEALED_ID_NAME_ONLY` because this v4 capture predates its semantic
fingerprint. Required pre-action fields are complete for 0/12 rows, so strict
replay is `NOT_EVALUABLE`. The positive factorized action branch matches 12/12
only under the report's named assumptions; this conditional diagnostic is not
exact Lua runtime or full-policy conformance and never creates an independent
expert vote or deployment authority.

Audit whether the accepted live fragments can seed the current simulator
exactly:

```powershell
py -3 -B -m o2o_dps.fury_shadow_sim_seedability_v1
```

The committed audit classifies 0/12 rows as `EXACT`, 12/12 as
`APPROX_ONLY`, and 0/12 as `REJECT`. The bridge has no canonical
mid-encounter seed/restore for RNG and pending events, the accepted records are
not a continuous command prefix with a common counterfactual horizon, and the
capture lacks exact aura identities/stacks/remains, stance/range/cast and
auto-attack state, dynamic AP/Strength/armor-penetration, effective armor, and
a capture-bound loadout. An approximate microstate is not a restore and cannot
create scalar or counterfactual reward; training, TD, offline-RL, performance,
and deployment gates remain false.

Summarize a completed Bloodthirst transition against the committed mechanics
registry with marker references as the authoritative trial boundaries:

```powershell
py -3 -B -m o2o_dps.calibration_summary `
  .\offline_data\calibration\BrainOfCat__<import-id>.jsonl `
  --registry .\mechanics\registry\turtle_1_18_1\warrior_fury.json
```

The summary reports timing, GCD, cooldown, outcome, attack-power/target context,
and only infers rage cost when a task-bounded `UNIT_RAGE` transition has no
auto-attack, energize, or second-action confounder. It compares observations
with the registry but never edits the registry or emits simulator overrides.
For the Phase 4 task it additionally requires all eight accepted normal hits,
one target GUID and armor value, and two internally constant AP strata. It fits
the declared Rank-4 `200 + 0.35 * AP` and legacy `0.45 * AP` shapes under one
unknown shared damage multiplier. That comparison tests relative shape only;
it does not substitute the template encounter armor or claim an absolute armor
model.

Review the audited Phase-12 campaign separately from generic calibration
promotion:

```powershell
py -3 -B -m o2o_dps.fury_current_build_phase12_review
```

The reviewer writes
`offline_data/sim_validation/fury_current_build_phase12_review.json`. Each
question has separate `observed_fact`, `simulator_comparison`, and
`promotion_decision` records. A completed exact stage can be retained as an
observed fact even when its mechanism gate is partial; incomplete sibling
stages and unmeasured values remain unresolved. This review is evidence-only:
it writes no mechanics-registry mutation or simulator override.
For runs collected under the older movement/range expectation, the review also
records `historical_contract_adjudication`: a terminal-incomplete moving Slam
can be retained only when its full corrected action chain and the associated
action row both show `moving=true`; a terminal-incomplete outside-target
Whirlwind can be retained only when cast, server GO, and zero-target evidence
are all present. Source-audit retests remain unchanged and are reported
separately from stages adjudicated as not requiring another collection.

Compare those eight promoted facts with the local Windows simulator in a
separate artifact:

```powershell
py -3 -B -m o2o_dps.fury_current_build_phase12_compare `
  --review .\offline_data\sim_validation\fury_current_build_phase12_review.json `
  --wowsims-root ..\wowsims-turtle `
  --go-executable "$env:LOCALAPPDATA\O2O-DPS\go1.23.4\go\bin\go.exe"
```

The command writes
`offline_data/sim_validation/fury_current_build_phase12_comparison.json`
without changing the review, audit, registry, or simulator. It preserves the
same eight keys and labels each conclusion `MATCH`, `MISMATCH`, or
`NOT_COMPARABLE`, with its deciding evidence channel recorded as
`runtime_test`, `source_model`, or `interface_gap`. Moving Slam is `MATCH` only
when the exact Windows Go test `TestTurtleSlamCanCastWhileMoving` runs and
passes; omitting `--go-executable`, a launch failure, a build failure, or a test
failure leaves that item `NOT_COMPARABLE`. The current Whirlwind source model
is a scoped `MISMATCH` because it processes encounter targets without a
distance/eligibility filter, but this does not identify an exact radius or a
replacement. The artifact also records the Recklessness source conflict: its
comment says 50% critical strike chance, 12 seconds, and a 5-minute cooldown,
while executable fields use 100%, 15 seconds, and 30 minutes. Live Phase-12
evidence did not measure those values, so the duration item remains
`NOT_COMPARABLE`. This first comparison always leaves
`simulator_comparison_complete`, `replacement_formula_identified`, and
`simulator_patch_allowed` false.

Build a live-character wowsims request after importing a logger run that
contains `STATIC_PROFILE_CAPTURED`:

```powershell
.\scripts\build_wowsims_profile_windows.ps1 `
  -CalibrationJsonl .\offline_data\calibration\BrainOfCat__<import-id>.jsonl
```

The command replaces only the template player's observed name, race, class,
equipment and talents. Encounter, buffs, rotation, consumes and existing
Warrior options remain unchanged. It writes a separate
`fury_warrior_live.metadata.json` because
metadata is not part of the RaidSimRequest proto. Turtle's `Ravager` (`碾碎`)
is never conflated with `ImprovedSlam`: its rank is applied separately as
`warrior.options.ravagerRank`, and that mapping is recorded in metadata. With
no full static-profile event, the command stops without writing a request; it
never keeps the template Orc as a substitute for an unknown live character.

## Chronicle acquisition boundary

The export assistant can build its selection from local printed leaderboard
PDFs or enumerate history for known characters through Chronicle's documented
External API. PDF hyperlinks select canonical slugs; the same External API
resolves those slugs to the UUIDs used in official CSV filenames. It does not
call browser-only/internal event routes, set browser-identifying headers, or
crawl the website. See the step-by-step Chinese runbook in
[`CHRONICLE_EXPORT_zh-CN.md`](CHRONICLE_EXPORT_zh-CN.md).

## Queue Chronicle exports on Windows

For the supplied `dps board` PDFs, build the manifest and deduplicated instance
queue without opening a browser:

```powershell
.\scripts\chronicle_export_windows.ps1 `
  -LeaderboardPdfDirectory '..\dps board' `
  -DiscoverOnly
```

The parser uses `pdftotext`, `pdftohtml`, and `pdfinfo` from the current Windows
PATH. It preserves every printed row and pairs names with hyperlinks by page
geometry, so sticky page controls cannot become character names. Start with one
official export:

```powershell
.\scripts\chronicle_export_windows.ps1 -Limit 1 -TimeoutMinutes 60
```

Without PDFs, the non-interactive known-character form remains available:

```powershell
.\scripts\chronicle_export_windows.ps1 `
  -Server 'Capybara' `
  -Realm 'Eversong Wilds' `
  -Character 'YourWarrior'
```

Run the same command again to resume. Use `-Status` to inspect progress or
`-DiscoverOnly` to populate the queue without opening a browser.

In the serial workflow, the user still has to enable the 13 event streams that
are off by default, clear source/target/ability filters, and click **Export
CSV**. The assistant prints the exact 13 stream names and waits for the official
`all-activity-<instance-uuid>.csv` filename.
If the External API does not return a UUID for a public slug, the assistant
accepts only the new official UUID CSV created after that page was opened.
Because a CSV cannot prove which streams were enabled, matching files that
predate the current collection run are ignored by default. `-AcceptExisting`
opts in a previously downloaded file only after the operator has checked it.

The Windows parallel workflow uses independent Chrome profiles and download
directories, atomically claims queue entries, selects all encounters, verifies
all 17 streams and empty filters, clicks Chronicle's official Export button,
and imports only a completed CSV that passes the strict importer:

```powershell
.\scripts\chronicle_parallel_export_windows.ps1 `
  -Concurrency 3 `
  -TimeoutMinutes 360
```

Progress and per-worker details are stored under
`offline_data\chronicle_raw\parallel_export`; see
[`CHRONICLE_PARALLEL_EXPORT_zh-CN.md`](CHRONICLE_PARALLEL_EXPORT_zh-CN.md).

Chronicle's public External API currently has character and character-instance
lookups, but it cannot enumerate the whole site and does not expose All
Activity event streams. The event routes used by the website are browser-only,
and Chronicle's terms prohibit scraping. The parallel worker does not call
those internal routes; it automates the official per-instance export UI with
bounded concurrency. Research use does not itself replace any permission that
Chronicle's terms may require for automated bulk collection.

## Audit the Chronicle offline dataset

After the queue is complete, audit every canonical raw CSV, normalized JSONL,
and provenance record. The JSONL scan is full-file and may use several Windows
worker processes:

```powershell
py -3 -m o2o_dps.chronicle_dataset_audit --workers 8
```

The command writes `offline_data/reports/chronicle_dataset_quality.json` and
`.md`. It reports exact event-type counts, roadmap Grade A/B/C, canonical
download UUIDs and page-slug aliases, plus the fields that must be reconstructed
or supplied by the in-game logger. A zero-row family is labelled
`empty_or_unobserved`; an exported CSV cannot prove which website streams were
selected.

## Audit Warrior reconstruction readiness

Roadmap Grade A records stream-family coverage; it does not prove that a row can
be reconstructed into a full simulator state. Run the stricter field-level
audit separately:

```powershell
py -3 -B -m o2o_dps.chronicle_reconstruction_readiness --workers 8
```

The audit streams normalized JSONL read-only and limits its scope to Warrior
players named by `export_queue.json` leaderboard rows (existing Fury trajectory
manifests add provenance, not raid-wide players). It writes rerunnable JSON and
Markdown reports under `offline_data/reports/`, at normalized instance x player
x encounter grain. The report separates name matches from INFO/GUID/class
identity verification and lists every unmatched leaderboard target. It audits
the roadmap's unified state (health/phase/targets, rage transitions, GCD/cast/CD,
swing/queue, auras, stance/range, recent actions, talents, gear/stats, and client
timing) as `OBSERVED`, `RECONSTRUCTABLE`, `INFERRED`, or `MISSING`.

Skill-parameter coverage uses Chronicle `START` action candidates, not every
source `GO` (which also contains passive procs). Calibrated GCD/cooldown durations
are reported separately from remaining-time state; a duration alone cannot
reconstruct remaining time without an initial anchor and complete transition
replay. It does not modify raw or normalized data and does not start an
experiment.

## Import on Windows

From this directory:

```powershell
py -m o2o_dps.import_chronicle_csv `
  'D:\Downloads\all-activity-example-instance.csv'
```

If the file was renamed and its instance cannot be inferred from the official
filename, provide it explicitly:

```powershell
py -m o2o_dps.import_chronicle_csv `
  'D:\Downloads\raid.csv' `
  --instance 'example-instance'
```

`--data-root` overrides the default `offline_data` directory, which is useful
for isolated imports and tests:

```powershell
py -m o2o_dps.import_chronicle_csv .\tests\fixtures\all-activity-instance-fixture.csv `
  --data-root "$env:TEMP\o2o-dps-offline-data"
```

On success the CLI prints a JSON receipt and writes:

```text
offline_data/
  chronicle_raw/<UTC-import-id>/
    <original CSV filename>    exact raw copy
    provenance.json            source path, size, timestamps, and schema
  normalized/
    <instance>__<UTC-import-id>.jsonl
```

No hashes, checksums, or fingerprints are generated. `shutil.copy2` preserves
the source file's modification timestamp on the raw copy, while
`provenance.json` records the original filename, absolute source path, byte
size, source modification time, and import time.

## Normalized row schema

Every JSONL row contains:

- `instance`, `encounter`, `event_index`, `offset_ms`, `time`, and `type`;
- `source`, `source_guid`, `target`, and `target_guid`;
- `spell`, `spell_id`, `value`, `outcome`, and `synthetic`;
- `flags` and `activity`, retained from the official export;
- `provenance`, containing the raw-copy path, source filename, CSV line,
  Chronicle export row number, and import timestamp.

`time` is preserved exactly because Chronicle can export absolute clock time or
relative encounter time. `value` becomes JSON `null`, integer, or floating
point when unambiguous; otherwise it remains a string.

The importer requires all 17 confirmed official columns:

```text
#, Type, Time, Source, Source GUID, Action / Ability, Spell ID,
Target, Target GUID, Value, Outcome / Detail, Flags, Activity,
Encounter, Event Index, Offset (ms), Synthetic
```

Missing columns, malformed CSV records, invalid integer fields, empty event
identity fields, or a `Synthetic` value other than `true`/`false` fail the
whole import. Errors identify the CSV line and column; no partial raw or
normalized dataset is published.

## Tests

```powershell
py -m unittest discover -s tests -v
```

The committed fixture is synthetic and exists only to test the importer. It is
not Chronicle data and is not training data.

## Fury expert-guided prefix benchmark

The first unified Fury proposal path combines several deliberately different
sources behind one factorized expert contract. Installed Cat and the deployed
Contra policy are `SOURCE_DERIVED`; they are source interpretations rather than
exact Lua-runtime traces. The current benchmark also consumes the
content-addressed `cat2_saved_profile_v1.json` snapshot of the deployed
`BrainOfCat Shadow` profile. Its Python adapter is likewise source-derived,
marks the mutable capture as `CURRENT_UNSEALED_SOURCE_PROFILE`, and is never an
independent expert vote. When a Warrior initially has no saved Cat2
`configurations` field, `/reload` creates the visible
`BrainOfCat Shadow` profile through Cat2's public APIs. Its first card records
the inactive Brain proposal, then its registered Auto Attack, Berserker Stance,
Bloodrage (below 30 rage), Execute, Bloodthirst, Whirlwind and automatic Heroic
Strike/Cleave (Cleave at two or more nearby enemies) cards remain the actual
executable Cat2 baseline. Existing Cat2 configurations are never changed. Use
`/boc macro` for the Cat2 public-runner entry and `/boc status` to inspect the
guard. The top panel then exposes the strict Shadow gate as stages 1/3,
`X/12`, and 3/3; only server-confirmed actual actions advance it. Macro repeat
without a skill or white-swing event is coalesced into one pending snapshot and
is never published as training data. This does not turn either the deployed
unsealed profile adapter or the curated Cat2 proposal source into an independent
expert vote. `contra_new` and the curated Cat2 source remain candidate-only.
Chronicle's compact Markov model is a partial
behavior prior only: it does not supply legality, queue intent, timers, reward,
or deployment authority. In this clean time-zero state family the query has no
recent-action context, falls back to the global prior, and contributes no unique
final candidate after the exploration union; its effect in this run is recorded
as provenance only, not search guidance.

Run the bounded Windows-native comparison with:

```powershell
py -3 -B -m o2o_dps.fury_expert_guided_search_v1
```

It uses `configs/wowsims/fury_warrior_clean_dual.json`, a proc-free dual-wield
comparison profile, and projects expert/prior proposals through the simulator's
current legal-action mask. The committed run covers 32 fixed seeds, 13
candidates per seed, 416 paired trials, and 32 current-Cat2 proposal rows. The
current Cat2 adapter proposes one distinct candidate already present in the
global union, adds zero exclusive final candidates, applies no ranking weight
or independent vote, and is therefore recorded as
`PROVENANCE_ONLY_NO_UNIQUE_FINAL_CANDIDATE`. It writes the aggregate report to
`offline_data/sim_validation/fury_expert_guided_search_v1.json` and 430 compact
candidate-evaluation rows to
`offline_data/sim_validation/fury_expert_guided_search_v1.trials.jsonl` (one
run-metadata row, 13 candidate-summary rows, and 416 evaluated trial rows).
These rows explicitly set `training_eligible=false`: the global union and trial
scalars are not per-state `(state, action*, next_state)` teacher data. The
current full Windows regression, including the later absolute-seed, catalog,
prefix, and loadout diagnostics, is 798 tests passing.

`DPSSim` is not a second Fury judge in this run. Its current implementation has
no Warrior ability model, and `player.py:216-218` substitutes a generic two-rage
per second rule for Warrior resource dynamics. It remains useful as Balance
Druid and architecture reference material; Fury execution and counterfactual
scoring use only the clean `wowsims-turtle` environment.

This is only a six-second, one-initial-state-family, first-decision prefix
benchmark with a passive continuation. Candidate `proposed_by` means that a
source proposed it for at least one listed seed, not that the source chose it in
every state; the report retains per-source proposal seed IDs and counts. It is
useful for checking adapter composition, legal
projection, queue/cancel/wait semantics, and deterministic simulator scoring;
it is not a full expert-policy replay or a full-encounter policy evaluation.
The artifact therefore keeps `deployment_allowed=false` and cannot support a
claim that BrainOfCat exceeds Cat, Contra, or any other expert.

## Fury held-out duration gate and singleton-health sensitivity

The primary policy comparison remains the instance-disjoint duration-mode
held-out gate in
`offline_data/sim_validation/fury_policy_heldout_corpus_gate_v1.json`.
Singleton-health evaluation is an auxiliary sensitivity check; it never changes
that duration gate and always writes `deployment_allowed=false`, even if its own
diagnostic gate passes.

The current duration artifact trains/selects the candidate from exactly one
Chronicle instance and holds out the other 49 with zero instance overlap. It
contains 392 scenario families, 16 matched seeds and 18,816 streamed rollouts;
all three policy lanes have zero omissions and complete horizons. Weighted DPS
is 610.338909 for the candidate, 586.167895 for the Cat translation and
464.059068 for the deployed-Contra translation. The candidate's margins are
+24.171014 and +146.279841 DPS respectively, and its margin and paired
win/loss gate pass separately in target-count strata 1, 2, 3--4 and 5+. This is
a pass against the two `SOURCE_DERIVED` translations under the explicit
armor=1721 and level=60 simulator hypotheses. It is not exact Cat/Contra Lua
execution or evidence of real-game superiority, so the artifact correctly keeps
`deployment_allowed=false` and names calibrated real-game Shadow evaluation as
the next gate. Repeated simulator seeds are stochastic sensitivity samples, not
additional independent raids.

Before a new health run, build or verify the protected literal-seed Windows
bridge. The builder will not overwrite either frozen simulator generation:

```powershell
.\scripts\build_simulator_windows.ps1
```

Generate the bounded eight-instance representative plan without launching
rollouts:

```powershell
py -3 -B -m o2o_dps.fury_singleton_health_sensitivity_v1 `
  --bridge .\bin\o2obridge.seedfix-v1.exe `
  --require-complete `
  --exclude-instance 043b4d65-9c58-4643-b601-62f1684af04a `
  --split-seed 2026091601 `
  --heldout-count 8 `
  --selection-seed 2026091602 `
  --max-families-per-instance 1 `
  --validation-seed 2026091601 `
  --validation-seed 2026091602 `
  --watchdog-cap-ms 120000 `
  --plan-only `
  --output .\offline_data\sim_validation\fury_policy_singleton_health_sensitivity_representative_plan_v1.json
```

The evaluator reads only the manifest aggregate and `COMPLETED` compact
`feature.gz` reports. It does not open scenario catalogs or raw JSONL and does
not retain request payloads or rollout rows. The held-out split uses whole
instances and an explicit `random.Random` seed. Each selected family must be an
actual `target_count=1` wave whose creature entry joins one of the 86 same-entry
health profiles. Multi-target waves are not split into artificial singletons.

For each family, `q1`, median, and `q3` are repeated complete Chronicle
kill-budget proxies with `INFERRED` status. They are placed on one isolated
simulator target for sensitivity analysis; they are not exact NPC health, real
raid TTK, or a model of per-target deaths in a multi-target wave. A rollout is
complete only when simulator damage reaches the configured health budget before
the watchdog. Cat, Contra, and the candidate must all have zero omissions,
faithful projections, and complete health horizons. Paired TTK wins/losses are
the primary comparison; DPS remains an overkill-sensitive secondary diagnostic.

The one-family smoke artifact
`fury_policy_singleton_health_sensitivity_one_family_smoke_v1.json` completed all
9 matched rollouts with zero omissions and zero incomplete horizons, but the
candidate lost the auxiliary gate against Cat. The eight-instance plan covers
4 short, 3 medium, and 1 long duration strata across 6 creature entries. Its
maximum `q3` budget is 493642.5, so the planned 144-rollout evaluation was not
started under a 120-second watchdog. No candidate parameters were changed.

## Current Cat2 transition and supplemental held-out diagnostics

The accepted schema-v4 Shadow session has been materialized as 12 ordered
transition fragments in
`offline_data/online_training/fury_shadow_transitions_v1.jsonl`. They contain
seven Whirlwind, three Heroic Strike, and two Bloodrage actions. Each row may be
used as a behavior label with its causally linked, typed immediate observed
outcome. It does **not** contain a preregistered scalar reward, either policy's
counterfactual reward, a complete next state, or an episode; consequently the
dataset is not TD-transition or offline-RL training data and grants no
deployment authority.

The current deployed `BrainOfCat Shadow` Cat2 profile is recorded in a
content-addressed snapshot and checked in two separate diagnostics. Strict
runtime conformance is `NOT_EVALUABLE` because all 12 captures omit decision
inputs required by Cat2; an explicitly assumption-conditioned positive-branch
replay matches 12/12, but is not full-policy conformance. Simulator seedability
classifies 0 rows as `EXACT`, 12 as `APPROX_ONLY`, and 0 as `REJECT`. The bridge
still lacks canonical mid-encounter seed/restore, continuous command prefixes,
RNG and pending-event state, a common counterfactual horizon, a preregistered
scalar reward and a complete post-action state; the mutable Cat2 capture is
also unsealed. These reports therefore remain diagnostic and non-voting.

The earlier reported-seed supplemental held-out replay in
`offline_data/sim_validation/fury_current_cat2_supplemental_heldout_replay_v1.full.json`
completed 392 families × 16 matched seeds × four policies = 25,088 rollouts.
Its receipt records 25,088/25,088, zero omissions and zero incomplete horizons.
Under the reconstructed request corpus and the sampling-plus-duration pooled
estimator, DPS is 610.338909 for the candidate, 586.167895 for Cat, 523.703164
for the current source-derived Cat2 profile, and 464.059068 for Contra. Cat2 is
below candidate and Cat in every target-count stratum. Its pooled margin over
Contra is +59.644096 DPS, but the single-target margin is -27.249363 and only
the 2+ target strata are positive, so this is not a general Cat2-over-Contra
claim.

That earlier full replay deliberately does not modify or reinterpret the historical
three-policy held-out gate. Its quality gate is false: Cat2 required 19,429
bounded `source_api_noop_unknown_blocker_proxy` decisions, and the corpus
contains 11 singleton families no longer than 100 ms, including two at 1 ms.
Those durations make unweighted DPS and paired means/extrema pathological;
they must not be quoted as performance conclusions. The pooled values above
are descriptive simulator diagnostics pending a duration-floor sensitivity
check. Exact Lua execution, an independent expert vote, real-game superiority,
training eligibility and deployment all remain false.

The non-voting duration-floor report
`offline_data/sim_validation/fury_current_cat2_short_horizon_sensitivity_v1.json`
replays the 11 `<100 ms` families with all four policies and the same 16 seeds
(704 rollouts). After excluding those rows, algebraically reconstructed pooled
DPS is candidate 610.133782, Cat 586.054828, Cat2 522.355382, and Contra
463.534629, so the pooled ordering does not change. Cat2's remaining margins
are -87.778400 versus candidate, -63.699446 versus Cat, and +58.820752 versus
Contra. The reconstruction subtracts directly replayed short sufficient
statistics from additive totals recovered from the full report's binary64 mean
and deterministic exposure denominator; it is explicitly not a bit-for-bit
recovery of the unretained numerator. Complement paired minima/maxima are
`NOT_DERIVABLE_WITHOUT_RETAINED_FULL_ROWS`. This post-hoc result resolves that
earlier replay's duration-floor sensitivity question for descriptive pooled
ordering only. The later sidecars below resolve the blocker classification,
but not its fixed retry-timing quality or any non-deployment boundary.

## Literal-seed supplemental replay and sidecars

The simulator bridge previously initialized `NewSim` with the requested seed
and then passed that same value to `Reseed`, whose argument is an iteration
offset. A nonzero reported seed therefore selected effective RNG seed
`2 * reported_seed`. This did not break the old within-seed paired comparison,
because every policy used the same effective stream, but it did make the old
seed label nonliteral. The legacy binary is preserved unchanged at SHA-256
`7055b9a44e8296a5888b1122a4193130e6b5c99e49f99101f3982d5d7ba26065`.
The corrected `bin/o2obridge.seedfix-v1.exe` has SHA-256
`3f455eada0cf962f10294cc0a0db1f5e698d9211cffe5a6b715028be6ed53a9d`.
The diagnosis report and receipt are
`fb4a0d0432073152bbc3bf430109473c63e6b9bda3f0216fa3befe0d12554450`
and
`6aeee2607b7f54f8fa0feeb448c3d4e9696286588a9d4ebf574891f7ea7a6a1a`;
their pre-replay next-step fields describe their generation time and are
superseded by the completed replay below.

The literal-seed input lock is
`fury_current_cat2_supplemental_absolute_seed_v1.input-lock.json`, SHA-256
`c740facfd5c276227c07f308473ed7207edf622a586779a1fdc21096dd017442`.
It pins 66 files, file bundle
`7694612f19461035918965512267e066d6ef9a107c98c26eb13b6d3b6d9aebb6`,
and request bundle
`6ec213d5e700eda39fc8b279a83750270005ecbc0506a65072b8d033ab502c1a`.
The completed full artifact/receipt hashes are
`3d9696ebea2c6f4d8a17c5f241ae5edd5b87395b54f32fb91e60fb78baacc8e6`
and
`1cb52466855b53cdca2fe4c7f4184564dbf0b73863f02f615f66b9fcc9c5aa3d`.
All 25,088 rollouts completed with zero omissions and incomplete horizons.
Descriptive weighted DPS is candidate 597.735135, Cat 593.061237, Cat2
538.181211, and Contra 458.230568. The candidate-minus-Cat pooled difference
is only +4.673897, while the single-target stratum reverses to Cat 571.343624
versus candidate 556.436114. This four-policy artifact stores paired tests with
Cat2 as the focal policy; it is not a new candidate-versus-Cat paired gate.

The literal-seed `<100 ms` sensitivity artifact/receipt hashes are now
`81037e5b7556cbcd0de205ea2206fbaa8ba1116edcb0e9762d525c2c5e18ad0d`
and
`a43684fed915cac0ff785024747856faf240cc4f4dbfbc2a880e85cfc3ca31d6`.
Its 704/704 short rollouts reproduce the 11 short families. Excluding them
gives candidate 597.600213, Cat 592.925328, Cat2 536.253110, and Contra
457.066475. The complement remains an algebraic reconstruction from retained
binary64 aggregates, not exact numerator subtraction, and paired extrema are
not recoverable. The analysis module and test are now pre/post-run locked under
source bundle
`441343c61cb4fbae23df431485388f6baf1a0de415d4d16f5db2c1ca5c319514`;
the prior unsealed sidecar is retained with a superseded filename.

The literal-seed no-op adjudication artifact/receipt hashes are
`424e12c7da29060e406e958455e40b3c51b12a8686f55e99206bf2b01798d6e8`
and
`65dc4a5db64b0e1f23d687fff9c52a77ab4b04a5ab81e2017175456edd43b343`.
All 20,348 Cat2 proxy events reproduce with zero unresolved cases and zero
violations as spell-cooldown-sufficient Whirlwind retries. Every retry still
uses a fixed 100 ms timing proxy rather than an exact Lua/client rejection
code, so diagnostic quality remains false. None of these supplemental or
sidecar results acquires training, voting, deployment, or real-game superiority
authority. The old three-policy gate remains a separate, unchanged simulator
gate with SHA-256
`d70b58829eabe2846af70a3b1cf219f610ec871dfdeb050d62b2fd1f6d08b0ba`.

## Capture-bound historical state catalog

`o2o_dps.fury_historical_state_catalog_v1` materializes one independent row
per calibration capture and never carries a field forward, backward, from a
nearest row, across tasks, or from Shadow. The current catalog contains 65,979
unique rows from 15 JSONL journals and 14 summaries. Dataset SHA-256 is
`54d3ac7d10ad354230e4b7c27a34889d56e344b52092d80b09579581ae63caf0`;
manifest/report hashes are
`d24ccbe19d13b7edb3e61adc722e145222472c632ad19b3144d732d0d1699061`
and
`913033890562fe817533a850ca438d6806e00b86e72e9905d4c8b4b17a62ac53`.
The manifest records resolvable bases for both inputs and outputs, and all
29 referenced source/output identities verify.

Same-capture coverage includes 63,877 attack-power rows, 64,891 target GUIDs,
4,761 equipment/talent/aura rows, 4,705 observed target-armor rows, and 28
skill-line/spellbook rows. The requested core intersection is 4,699 rows and
core-plus-skills is 11. Another 56 rows declare an armor source while lacking
a target and are explicitly unavailable, not observed armor. The catalog is
`HISTORICAL_PRIOR_ONLY`; it performs zero Shadow joins/backfills and supplies
no exact simulator seed, TD transition, expert vote, deployment decision, or
real-game claim.

## Time-zero prefix gate and loadout/armor canary

The prefix artifact/receipt hashes are
`71dbbf56791dbbd9ff3d33f4f2acfd13ac964b2a04baa66e8a919660efd12d28`
and
`de13ee7f47cb98a57ba346f045d2362e21facde0d2ad6a7a5f38bd05a4e50caa`.
Across queue/cancel, stance/off-GCD/GCD/swing, and three-target
queue-plus-Whirlwind lanes, 18/18 fresh-process episodes complete and 12/12
replays match with zero invariant failures. `PASS` means only
`REPLAY_FROM_TIME_ZERO / SIMULATED_PREFIX_ONLY`. The bridge still cannot export
or restore RNG state, pending-event state, or an accepted-but-not-yet-active
queue, and this gate does not seed a historical mid-encounter or backfill a
Shadow row.

The first nuisance canary is pinned by plan/contract hashes
`e56cf279a6dfddfdae763673c2af7411104b18925a4d83575e47bac37abff48b`
and
`e2336c0e85ef0496e7e246d198b9c172ec41f1818172fee2c77a9aa1f17fa423`.
It crosses 12 target-count × duration cells, two same-capture two-hand loadout
bundles, four starting armors, and four source-derived policies. The 384
reference episodes and 384 fresh-process repeats have 384/384 identical result
hashes and zero incomplete horizons. The artifact/receipt hashes are
`c0768bc61c2ed3512e5b4a57c58180b12fffb7263f68af75ca4baea670801fa8`
and
`b33af65501dec5598741366303c6428c4bdeae3b498be60676fe053981b92903`.

This canary intentionally reports `FAIL_CLOSED`: its reference pass has 1,049
Cat omissions in 47 episodes and 22,648 Contra omissions in all 96 Contra
episodes. The latter is the declared boundary of the bounded two-hand Contra
adapter, which covers boss/training-dummy states while these selected Chronicle
cells lack that classification. Candidate and Cat2 have zero omissions; Cat2
also records 344 classified cooldown proxies. Bonereaver and Crusader vary as
one historical bundle and cannot be separated causally here. The observed DPS
cells are therefore nuisance/debugging signals only and cannot rank the four
policies. Candidate remains inactive Shadow.
