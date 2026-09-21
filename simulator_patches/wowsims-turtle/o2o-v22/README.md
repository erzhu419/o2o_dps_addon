# O2O v22 precombat, late-arrival, cooldown metadata, and Rapid Growth

Apply `0001-precombat-late-arrival-cooldownmeta-rapidgrowth.patch` after
the v19 patch chain, then apply `0002-effective-armor-floor.patch`. Together
they form the incremental v19-to-v22 capsule: the v20 and v21 binaries were
intermediate local builds and do not need separate patches.

The increment adds the native pieces used by the current causal burst replay:

- `load_dynamic_v3_precombat` atomically opens an explicit, current-observation
  pre-pull window. Only declared self actions are legal while every target is
  unattackable. `pull_time_ms` is distinct from player reachability, so the
  first target may become attackable at the pull or later.
- bridge state now reports the current precombat clock, autoattack state,
  Warrior stance, and pending/active next-swing queue. Available actions report
  native cooldown duration and whether the action is result-bearing.
- Turtle item 56113 (Elixir of Rapid Growth) is selectable through
  `MiscConsumes`: +30 Strength for 120 seconds, followed by -25 Strength and
  Stamina for 120 seconds, on its own 120-second cooldown. It does not consume
  the combat-potion cooldown.
- Juju Flurry and Recklessness are marked helpful for declared precombat use,
  and Potion of Quickness applies its intended melee haste as well as spell
  haste.
- dynamic target semantics exports `Unit.Armor()`, the same non-negative armor
  used by physical damage, rather than the raw stat that can temporarily fall
  below zero when capped Sunder Armor exceeds a low-armor target.

The two patches contain 17 text paths: 13 implementation/generated-proto paths
and four focused test paths. They intentionally exclude databases, binaries,
result files, build receipts, and the rest of the third-party repository.
`sim/core/proto/common.pb.go` is included because the pinned patch chain keeps
generated Go protobuf sources in-tree.

## Base and apply

The base is the exact v19 state produced from upstream commit
`64cfa6ae2777b20bc95f3573dd9b2572634dddd7` by applying the existing capsules
in this order:

```text
o2o-v14/0001-o2o-cumulative.patch
o2o-v15/0002-player-checkpoint.patch
o2o-v15/0003-post-gcd-queue.patch
o2o-v16/0001-client-post-gcd-rejection.patch
o2o-v17/0001-external-press-clock.patch
o2o-v18/0001-dynamic-press-clock.patch
o2o-v19/0001-atomic-dynamic-v3-press-load.patch
```

On the reconstructed v19 tree, check and apply v22 with:

```text
git apply --check --whitespace=error-all /path/to/0001-precombat-late-arrival-cooldownmeta-rapidgrowth.patch
git apply --whitespace=error-all /path/to/0001-precombat-late-arrival-cooldownmeta-rapidgrowth.patch
git apply --check --whitespace=error-all /path/to/0002-effective-armor-floor.patch
git apply --whitespace=error-all /path/to/0002-effective-armor-floor.patch
```

The historical v17 patch has whitespace-sensitive context after v16 on Git for
Windows 2.47. If strict context matching rejects `cmd/o2obridge/main.go`, use
`git apply --check --ignore-space-change --ignore-whitespace` and the same
options for that v17 patch only. The v22 patch itself passes the strict command
above.

## Source and behavior checks

After applying, run the focused native tests from the `wowsims-turtle` root:

```text
go test -p=1 -tags with_db ./sim/o2o ./cmd/o2obridge -run "Precombat|WarriorStateExportsStanceQueueAndAutoattack|DynamicSemanticsReportsCombatEffectiveArmorFloorForSunder" -count=1
```

For the wider affected surface, run:

```text
go test -p=1 -tags with_db ./sim/core ./sim/o2o ./cmd/o2obridge ./sim/warrior -count=1
```

Packaging verification on 2026-09-14 reconstructed v19 from the pinned commit,
strictly checked and applied the first patch, and compared its 15 patched text
paths against the then-current v22 source after normalizing CRLF/LF: 15
matched, zero mismatched. The four existing native Python integration tests for composed
precombat burst, late-arrival skip/later-wave reuse, Mighty Rage scheduling, and
Rapid Growth independence passed against the already-built v22 Windows bridge.
The second patch's focused Go test was run on 2026-09-19 with Go 1.23.4; its
strict apply check is part of this capsule's verification commands below.
