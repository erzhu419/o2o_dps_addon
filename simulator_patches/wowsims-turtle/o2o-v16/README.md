# O2O v16 client queue correction

Apply `0001-client-post-gcd-rejection.patch` after the v15 cumulative patch sequence. It supersedes only v15's unobserved same-invocation post-GCD queue acceptance; the v15 patch and binary remain historical negative evidence. The corrected command records a rejected same-key queue attempt without casting or clearing an earlier accepted queue. A Go regression covers prequeued Cleave surviving Whirlwind and firing on the next main-hand swing.

The active Contra_new Raid-B runner no longer calls `act_post_gcd_queue`. One observed macro reentry interval does not close the full Contra_new client cadence, so that lane remains unscored. Verify the applied patch with `go test -tags with_db ./sim/o2o -run 'PostGCDQueue|CleaveQueuedBeforeWhirlwind' -count=1` before building a new versioned bridge; do not overwrite v15.
