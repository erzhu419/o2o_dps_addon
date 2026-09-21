# O2O-DPS current status

Updated: 2026-09-21.

This is the single current-result entry point. Historical reports remain as
evidence for their own frozen stages, but they do not override later results.

## Result lineage

| Stage | Frozen result | Interpretation |
|---|---|---|
| V6 | Best searched candidate improved on its proposal data, but its independent selection interval crossed zero; exact Cat was retained. | Search signal, no accepted improvement. |
| V7 | Every nonzero fixed-sequence candidate was rejected; the frozen winner was exact Cat. | Valid fallback, no policy gain. |
| V8 | One nonzero Cat-relative residual beat exact Cat and both raw Contra baselines on a fresh 256-seed panel. | Positive only in the frozen two-wave development model. |
| V9 | Parent-relative append contract, teacher, distiller, and selector exist; no completed V9 campaign result is published. | Implementation in progress, no result claim. |

The authoritative V8 report is
[`UPPER_KARA_CAUSAL_V8_2026-09-20.md`](UPPER_KARA_CAUSAL_V8_2026-09-20.md).
For `live_bonereaver`, the selected Rage Potion loadout, and the composed
`multi_two -> single_long` model, its fresh panel reports mean effective damage
19,376.12 for V8 versus 17,052.97 for Cat (+13.62%), 13,268.21 for deployed
Contra, and 13,593.44 for Contra260817. All 1,024 policy lanes completed.

That result establishes a model-local controller improvement. It does not yet
establish a better general short-cooldown rotation, complete Upper Karazhan
route superiority, cross-build generalization, Lua-client parity, or live-game
superiority. V8 changes one opportunity: when two first-wave targets are alive,
Death Wish is ready, and Cat proposes Battle Shout, it substitutes Death Wish
and queues Cleave; otherwise it remains Cat.

## Implemented correction

- Residual steps now enter the executed latch only after the ordered action
  plan is accepted by the native executor. Rejected/unconfirmed plans remain
  retryable.
- E0 retains the event-driven replay. E1 now atomically loads the same
  precombat timeline with a shared external press clock; WAIT abstains from a
  press and internal simulator wakes do not create extra policy decisions.
- The frozen A0--A3 attribution arms are implemented as runtime transforms of
  Cat's current ActionPlan, so A1 preserves Cat's queue lane and A2 preserves
  Cat's GCD lane.
- Versioned compact telemetry now records the causal execution evidence needed
  for attribution. Formal seed artifacts without that telemetry are rejected
  by the reducer rather than silently summarized.
- A published-seed E0 compatibility gate and its six-node shard orchestration
  are implemented for reproducing the old V8 aggregate before fresh panels.

These are implementation results, not a completed attribution result. Focused
tests and native single-seed smoke tests have passed; the frozen 256-seed
panels have not run.

## Active work

The next frozen work is:

1. Reproduce the published V8 E0 aggregate after the execution-contract
   changes. This is a compatibility gate over reused held-out examples, not
   fresh evidence.
2. If that gate passes, attribute the V8 gain on fresh paired E0 and E1 panels
   with A0 Cat, A1 Death-Wish-only, A2 Cleave-only, and A3 exact V8. A1 must
   preserve Cat's current queue and A2 must preserve Cat's current GCD at
   runtime.
3. Then run the existing V9 parent-relative append search end to end and retain
   V8 unless a child beats that parent on independent data.

No searched policy is currently authorized to control Cat2 in the live client.
Shadow/client tests are mechanism and execution validation, not substitutes for
the frozen simulator comparisons above.
