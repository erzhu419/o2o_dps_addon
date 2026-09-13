# O2O v15 simulator deltas

Apply the owned [v14 cumulative patch](../o2o-v14/0001-o2o-cumulative.patch) to the pinned upstream `64cfa6ae2777b20bc95f3573dd9b2572634dddd7`, then apply `0002-player-checkpoint.patch` and `0003-post-gcd-queue.patch` in order. These incremental patches do not duplicate the 1.5 MB v14 patch or the third-party tree.

`load_dynamic_v5` takes the same dynamic-v4 target config plus `player_state` with schema `o2o_player_state_checkpoint/v1`. It returns the restored state at time zero **before** any combat event; call `advance` to reach the next input decision. Rage, Warrior stance aura, GCD, explicitly listed spell cooldowns, and MH/OH swing deadlines are restored. A nonempty next-swing queue, self aura/proc set, or candidate-owned debuff set is rejected. No pre-pull actions are supported. This is a limited restoration contract, not a general simulator snapshot.

Current `brainofcat_shadow_checkpoint/v1` rows only capture the visible target. The Python importer marks their target-registry scope incomplete; `player_checkpoint_from_shadow_v1` rejects them rather than promoting a partial historical checkpoint to exact replay.

Verification against the native `with_db` simulator: `go test -p=1 -tags with_db ./sim/o2o ./cmd/o2obridge ./sim/warrior` and `python -B -m unittest tests.test_sim_bridge_dynamic_v5`.

`0003-post-gcd-queue.patch` adds the `act_post_gcd_queue` bridge command. It accepts the usual `action` and returns `apply` plus `state`, but only for one Heroic Strike/Cleave tag-1 queue immediately after an accepted consuming-GCD action at the same simulator timestamp, before `advance`. The command preserves the source sink order; acceptance is a simulator assumption, not a WoW client observation. Its directed Go tests are `go test -tags with_db ./sim/o2o -run PostGCDQueue -count=1`. After the Go tests pass, build a new versioned binary with `scripts/build_simulator_windows.ps1` or `scripts/build_simulator_linux.ps1` from `o2o-dps`; do not overwrite earlier binaries.

Patch application was checked in an empty temporary Git directory by reconstructing the two affected existing files from the v14 and `0002` patches, then running `git apply --check --whitespace=error-all 0003-post-gcd-queue.patch`. Applying it produced the same three Go file contents as the tested local simulator. The full pinned-upstream checkout was not rebuilt for this patch; the native local source passed `go test -tags with_db ./sim/o2o ./cmd/o2obridge -count=1`.
