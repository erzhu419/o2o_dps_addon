# O2O v17 external press clock

Apply `0001-external-press-clock.patch` after the v16 patch. This is an incremental patch for two Go sources plus two press-clock tests; v16's client-observed same-key queue rejection is unchanged.

The opt-in `configure_press_clock`/`finish_press` bridge commands expose fixed external key opportunities. A successful on-swing queue still uses native simulator acceptance and does not create another key at the same timestamp. GCD becoming ready also does not create an extra key. The default interactive mode is unchanged. The period and phase are experimental inputs, not a measured WoW player's cadence. This version does not integrate dynamic-v3 idle or responsive team wake; do not use its output as a four-policy fairness verdict until the same key-opportunity protocol is applied to every lane.

From the `wowsims-turtle` root, verify with `go test -tags with_db ./sim/o2o ./cmd/o2obridge -run PressClock -count=1`.
