# Source and data boundary

This repository contains the original O2O research pipeline, tests, contracts,
and policy-adapter implementations maintained in `o2o-dps`.

It intentionally does **not** redistribute the following local inputs or
third-party project trees:

- `offline_data/` and raw Chronicle exports;
- built simulator binaries under `bin/`;
- Cat, Cat2, Contra, Contra_new, wowsims-turtle, and DPSSim source trees.

Some modules are source-derived behavioral adapters: they translate bounded
branches observed in locally supplied Cat/Cat2/Contra-family code into the
project's normalized action contract. Their provenance and non-equivalence
boundaries are recorded in `configs/experts/fury_experts_v1.json` and
`experts/DEPLOYED_EXPERT_AUDIT.md`. Inclusion of an adapter is not a claim of
ownership over, redistribution permission for, or whole-file equivalence with
the referenced third-party addon.

No license is granted here for excluded third-party projects or datasets. This
notice is provenance documentation, not a replacement for their authors'
license terms.
