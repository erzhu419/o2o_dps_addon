# wowsims-turtle O2O v12 cumulative patch

This directory contains only the O2O-owned simulator delta. It reconstructs the
current source changes on top of upstream commit
`64cfa6ae2777b20bc95f3573dd9b2572634dddd7`; it is not a copy of the third-party
repository.

The patch includes 68 changed Go files and `proto/warrior.proto`. Fourteen of
the Go files are generated `sim/core/proto/*.pb.go` files because the pinned
upstream commit does not contain that package and a clean patched checkout
cannot compile without it.

## Apply and verify

```sh
git clone https://github.com/isfir/wowsims-turtle.git
cd wowsims-turtle
git checkout 64cfa6ae2777b20bc95f3573dd9b2572634dddd7
git apply --check --whitespace=error-all /path/to/0001-o2o-cumulative.patch
git apply --whitespace=error-all /path/to/0001-o2o-cumulative.patch

go run ./tools/database/gen_db -- -outDir=assets
go test -tags with_db ./cmd/o2obridge ./sim/o2o ./sim/warrior ./sim/common/item_effects ./tools/database
```

The `with_db` tag is required for tests that instantiate historical equipment;
without it, wowsims intentionally starts with an empty runtime database.

## Boundary

The patch excludes generated `assets/database/*`, binaries, test-result files,
local build receipts, and the unchanged upstream tree. It includes the current
dynamic O2O environment/bridge, Warrior mechanics, Turtle item handling
(including exact 55113 and 61194 behavior while leaving 55116 unimplemented),
and the current enchant overrides. See `manifest.json` for the exact artifact
identity and verified counts.
