# O2O v19 atomic dynamic-v3 press load

Apply `0001-atomic-dynamic-v3-press-load.patch` after v18. The increment changes only the bridge command switch and its response test; the v18 simulator press scheduler remains unchanged.

`load_dynamic_v3_press_clock` accepts the existing dynamic-v3 request plus `press_period_ms` and `press_phase_ms`. It configures the fixed external press clock before the bridge performs its initial `AdvanceUntilDecision`, so an initially unattackable wave returns the first physical key at its configured phase instead of entering the default no-policy idle path. The existing `load_dynamic_v3` command is unchanged and still auto-advances an initial no-target interval.

From the `wowsims-turtle` root, verify with:

```text
go test -tags with_db ./sim/o2o ./cmd/o2obridge -count=1
```

Build new artifacts without replacing v18:

```text
go build -tags with_db -trimpath -o ../o2o-dps/bin/o2obridge.press-v19.exe ./cmd/o2obridge
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -tags with_db -trimpath -o ../o2o-dps/bin/o2obridge.press-v19.linux-amd64 ./cmd/o2obridge
```
