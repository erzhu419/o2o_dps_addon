# O2O v27 atomic precombat external press clock

Apply `0001-precombat-external-press-clock.patch` after the v22 capsule (or
the later local bridge lineage containing the same v22 precombat command).

The new `load_dynamic_v3_precombat_press_clock` command configures dynamic-v3,
the explicit precombat action allowlist, and the external physical-key grid
before the simulator advances. Its first returned live state is therefore the
first scheduled physical press; no policy decision can occur between separate
precombat and press-clock configuration calls.

Focused verification:

```text
go test -p=1 -tags with_db ./sim/o2o ./cmd/o2obridge -run "PressClock|Precombat" -count=1
```

The corresponding Python client is
`SimulatorBridgePrecombatV1.load_dynamic_v3_precombat_press_clock`. The opt-in
`NativeDynamicV3ActionProgramReplayV1` external press mode treats WAIT as
abstention, closes every nonterminal key with `finish_press`, and only invokes
the resolver when the simulator reports a ready physical press.
