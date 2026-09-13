# O2O v18 dynamic press clock

Apply `0001-dynamic-press-clock.patch` after v17. The increment changes only `sim/o2o/environment.go` and its press-clock tests; v17's default interactive mode and bridge commands remain intact.

Opt-in physical key ticks now continue through dynamic-v3 unattackable windows. Target eligibility still gates `AvailableActions` and `Apply`: a key opportunity is not permission to damage an unattackable target. Responsive team wakes are internal events, not extra policy keys; same-time background effects and wakes resolve before the corresponding key, while off-grid wakes do not shift the configured period/phase. A terminal horizon stops pending keys. The cadence is a shared experimental input, not an observed player cadence or a four-policy fairness result by itself.

From the `wowsims-turtle` root, verify with `go test -tags with_db ./sim/o2o ./cmd/o2obridge -run 'PressClock|PostGCDQueue|CleaveQueuedBeforeWhirlwind' -count=1`.
