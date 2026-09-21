# Upper Kara causal program V8 result (2026-09-20)

## Verdict

V8 is the first frozen search result in this project that beats exact Cat on a fresh held-out panel. The result is positive for the model-defined two-wave development environment, not yet a claim about the full Upper Karazhan route or the live game.

The selected program is a one-step Cat-relative residual. Outside its observable guard it executes exact Cat, so Cat was structurally present in the searched family rather than reconstructed approximately.

## Frozen protocol

- Build: `live_bonereaver`.
- Search family: `CatActionPlanResidualSequenceSearchV8`.
- Proposal evidence: 128 predeclared seeds per loadout.
- Selection evidence: a separate 128-seed half.
- Final evaluation: 256 untouched seeds, arrivals fixed to 0/1000/3000/5000/7000/9000 ms.
- Loadouts searched: no potion, Mighty Rage Potion, Rage Potion, and Quickness Potion.
- Native baselines in every held-out case: Cat, deployed Contra, and Contra260817 (`contra_new`).
- Every held-out lane used the same selected loadout, exact build, case, seed, and replay interface.
- Completion: 1024/1024 held-out policy lanes complete; no failed or invalid lane was scored as zero.

## Selected program

The frozen winner is:

`v8::upper-kara-cat-action-plan-residual-v8-256x256::contra_turtle_burst__rage::proposal-005`

It has one wave-local step:

1. Observe the `multi_two` wave with exactly two live targets.
2. Require Death Wish (`spell_id=12328`) to be ready.
3. Wait for Cat's current proposed GCD to be Battle Shout rank 7 (`spell_id=25289`).
4. Replace that GCD with Death Wish and set the next-swing queue to Cleave rank 5 (`spell_id=20569`).
5. Latch the step after execution; at every other decision use exact Cat.

This is a searched action-order change, not a scalar threshold adjustment. It combines cooldown timing, a GCD replacement, and a queued next-swing action under a current-state-only guard.

## Results

Training freeze evidence:

| Quantity | Mean effective damage |
|---|---:|
| Selected residual | 19,561.88 |
| Best imported incumbent (Cat) | 17,292.21 |
| Separate selection-half value for selected residual | 19,764.79 |

Fresh 256-seed held-out panel:

| Policy | Mean effective damage | Mean effective DPS | Candidate delta | Relative delta |
|---|---:|---:|---:|---:|
| Frozen V8 candidate | 19,376.12 | 602.38 | - | - |
| Cat | 17,052.97 | 529.74 | +2,323.15 | +13.62% |
| Contra deployed | 13,268.21 | 411.91 | +6,107.91 | +46.03% |
| Contra260817 / `contra_new` | 13,593.44 | 421.67 | +5,782.68 | +42.54% |

Paired held-out statistics:

| Baseline | 95% normal CI for damage delta | Win / tie / loss |
|---|---:|---:|
| Cat | [1,859.26, 2,787.03] | 163 / 42 / 51 |
| Contra deployed | [5,575.90, 6,639.93] | 237 / 0 / 19 |
| Contra260817 | [5,252.73, 6,312.64] | 236 / 0 / 20 |

Against Cat by first-wave arrival:

| Arrival | Seeds | Mean damage delta | 95% normal CI | Win / tie / loss |
|---:|---:|---:|---:|---:|
| 0 ms | 43 | +2,607.20 | [1,419.94, 3,794.46] | 31 / 0 / 12 |
| 1,000 ms | 43 | +2,698.45 | [1,449.98, 3,946.93] | 34 / 0 / 9 |
| 3,000 ms | 43 | +3,292.78 | [2,151.55, 4,434.02] | 36 / 0 / 7 |
| 5,000 ms | 43 | +3,210.71 | [2,048.93, 4,372.50] | 33 / 0 / 10 |
| 7,000 ms | 42 | +2,069.82 | [819.05, 3,320.58] | 29 / 0 / 13 |
| 9,000 ms | 42 | 0.00 | [0.00, 0.00] | 0 / 42 / 0 |

The exact tie at 9,000 ms is expected: the guarded opportunity is no longer encountered, so the residual falls through to Cat for the whole episode.

## What this establishes

- The previous “search can still lose to Cat because exact Cat is absent” defect is closed for this family.
- A searched action-order intervention can generalize beyond the proposal and selection seeds.
- The improvement is localized and executable: it is one observable, wave-specific rule, not future-informed playback of a fixed sequence.
- Cat, Contra, and Contra260817 were all executed as native reactive programs in the same held-out cases.

## Limitations and next stage

- This remains the `multi_two -> single_long` development model, not every Upper Kara pull or boss.
- The selected residual has one intervention step. It does not yet search a complete per-wave variable-length action sequence.
- The three raw baselines retain their own native cooldown behavior; they were not independently re-optimized with the candidate's planner. This is appropriate for measuring improvement over the deployed baselines, but it does not isolate rotation quality from cooldown planning quality.
- Therefore the held-out result establishes that the complete automated controller beats raw Cat in this development model. It does not establish that the underlying short-cooldown rotation still beats a Cat control given the same Death Wish/Cleave trigger. A fresh-seed Death-Wish-only, Cleave-only, and combined-rule ablation is still required for that attribution.
- The four potion/loadout requests have different request identities and therefore different derived simulator RNG streams. The selected Rage Potion loadout is not evidence that Rage Potion is superior to the other three loadouts; only the within-loadout paired policy comparisons are strict common-case comparisons.
- The model-defined team kill clock and inferred target model still require route-level validation against Chronicle-derived wave episodes and then live-game evaluation.
- Recklessness remains excluded from the candidate teacher until its Turtle mechanics contract is resolved.

The next experiment should preserve this exact-Cat fallback and frozen train/held-out accounting while allowing multiple latched steps per modeled wave, then bind those programs to the Chronicle-derived Upper Kara wave registry. Long-cooldown and potion availability must carry across waves so late engagement can reschedule a saved cooldown rather than consume it on a nearly dead pull.
