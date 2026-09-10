# Deployed expert boundary

The expert of record is the addon actually loaded by WoW from the sibling
directories under `D:\WOW\Interface\AddOns`, not the research copies nested in
`BrainOfCat`.

## Current deployment

- `Cat`, `Cat2`, and `Contra` have deployed sibling directories.
- The nested Cat and Cat2 copies have substantial file drift from those
  deployed directories. Training provenance must name which copy produced a
  trace.
- The installed sibling
  `D:\WOW\Interface\AddOns\Contra\Contra.toc` loads `Contra_ALL.lua`. It does
  not currently load its same-folder `Contra.lua`.
- The research copy `BrainOfCat\Contra\Contra.toc` does the opposite and loads
  the single-line `Contra.lua`. That nested directory is not the top-level addon
  WoW currently discovers, so trace provenance must include the resolved addon
  directory as well as the filename.
- `Contra.lua` is single-line minified/obfuscated Lua source, not Lua bytecode.

The nested minified `Contra.lua` and nested readable `Contra_ALL.lua` have a
high-confidence match for the Warrior rotation, macro dispatch, and talent
segments after lexical normalization, but the whole files are not equivalent.
The installed readable `Contra_ALL.lua` is a behaviorally different third
artifact. In particular, its queued Warrior action path and access table differ
from both nested files. Current expert labels must therefore come from the
installed file or from an opt-in black-box action trace, never from an assumed
whole-file equivalence.

## `Contra_new` expert value

`BrainOfCat\Contra_new` is newer readable source (`ContraDBDefault.Version =
20251017022403`, versus `202510170223` in the current Contra family). It is
useful, but it is not currently a runnable independent expert:

- its TOC references missing `Contra_Debuff.lua` and `Contra_UI_DB.lua`;
- the loaded `Contra_TrinketManager.lua` is empty;
- it hard-codes many `Interface\AddOns\Contra\...` paths and reuses global
  `Contra` plus `ContraDB`, so copying it beside the installed addon would
  collide rather than create a safe second expert;
- class files do not equal callable policy coverage: Warrior is the most
  complete path; Rogue, Hunter and Mage are substantial; Druid is feral-only
  with non-feral routing problems; Shaman, Warlock and Paladin are partial or
  prototype paths; Priest has no policy file.

For O2O training it should therefore be labeled as a **Contra-family corrected
candidate**, not counted as an independent ensemble vote. Its immediate value
is readable state-variable discovery, action ordering, and candidate rule
generation. The installed Contra trace remains the deployed-behavior label;
any selected `Contra_new` fix becomes a separate candidate policy and must be
scored in the calibrated simulator and Shadow evaluation.

Contra remains a required strong baseline. The readable two-hand raid body in
the installed `Contra_ALL.lua`, the corresponding body in `Contra_new`, and the
minified `Contra.lua` all contain the boss/dummy branch plus three non-boss
branches selected by `UnitHealthMax(target)` (`>=51000`, `25000..50999`, and
`<25000`). A simulator adapter that implements only the boss/dummy branch is an
adapter coverage gap, not evidence that Contra lacks the policy. Target
classification, maximum health, true health percentage, and the six-piece
`Contra.ZSSDW` count must be reconstructed from the offline sidecar/loadout
before those branches are compared. `Contra_new` does not correct those health
or classification definitions; its useful additions include automatic
single-target/AoE routing and newer talent synchronization.

The character/guild allowlist is a separate runtime-entry gate. Failing to
attest that gate prevents a claim that the local client executed Contra, but it
does not invalidate a clearly labeled source-derived policy-body baseline.
Whole-file equivalence between minified `Contra.lua` and readable
`Contra_ALL.lua` is still not claimed.

Reachable source bugs also prevent treating every rule as expert truth. The
current tree contains always-true `x ~= A or x ~= B ...` conditions, mixed
`and`/`or` expressions whose precedence changes the intended gate, and a PvP
path that calls a boolean as a function. Those sites are candidate corrections,
not labels to imitate blindly.

## Fury mechanics that expert code cannot calibrate

The Fury policy sources distinguish state-to-action choices from physical game
mechanics:

- Deployed Cat changes its next-swing action from Heroic Strike to Cleave when
  its nearby-enemy scan reports more than one enemy, and includes Whirlwind in
  the AoE priority and rage budget (`Cat/WarriorFury.lua:61-75,342-353,400-404`).
  Deployed Cat2 has a group Whirlwind card at three or more nearby enemies and
  a Sweeping Strikes card above one (`Cat2/Cards/Warrior/WhirlwindGroup.lua:50-55`
  and `Cat2/Cards/Warrior/SweepingStrikes.lua:53-59`). These are useful action
  priors; they do not establish Cleave's second-target selection, Whirlwind's
  target cap, or per-target hit rolls.
- The installed Contra addon currently loads the readable `Contra_ALL.lua`.
  Its Warrior AoE rotation is selected by macro mode rather than by the defined
  eight-yard enemy-count helper, so it is another conditional action prior, not
  evidence that enemy counting or an AoE cap was exercised.
- Cat, Cat2, installed Contra, and `Contra_new` contain no damage-taken-to-rage
  equation. Their incoming-combat handlers support policy state such as
  Revenge/Overpower windows and fear handling, but cannot calibrate simulator
  rage generation from damage received.
- Both the readable installed Contra source and `Contra_new` reference the
  undefined global `IsTargetOfTargetMeor` in the Warrior Berserker Rage branch
  after assigning a differently named local. That reachable branch is a
  candidate source bug, not an expert label.
- Cat's queued Heroic Strike/Cleave helper cancels a queued attack by clearing
  and restoring the same target. None of the expert sources establishes that a
  next-swing queue is preserved across a genuine target change.
- Execute rules below 20 percent target health are action priors. They do not
  make an ordinary full-health training dummy a valid Execute calibration
  environment.

Consequently, single-dummy calibration excludes multi-target fan-out,
damage-taken rage, cross-target next-swing preservation, and a fresh Execute
station. Those boundaries remain explicitly unresolved or are backed only by
separately labeled prior observations; they do not count toward simulator
mechanism calibration or authorize a simulator patch.

## Callable entry points

- Cat: `MPCat(type)`, `MPFuryDPS()`, and `MPEvilDPS()` are the stable inner
  policy entry points used by their bindings.
- Cat2: `Cat2.ExecuteConfiguration(name)` executes the ordered profile steps;
  only a literal `true` returned by a card stops the sequence.
- Contra: `Contra_MacroHandler(cmd)` is the main handler and
  `Contra_Macro(param)` is public. Contra's slash registration holds a function
  reference, so a temporary trace hook must also rebind and later restore
  `SlashCmdList["CONTRAMACRO"]`.

An expert keypress can request an ordered list containing target/control,
form/stance, auto attack, item/off-GCD, next-swing queue, and GCD actions.
Flattening that list to one categorical action loses reachable Cat, Cat2, and
Contra behavior. Cat2's `true` means "stop this configuration pass"; it does
not prove that the server accepted a cast.

The deployed Contra handler has access/profile gates before its Fury dispatch.
BrainOfCat therefore marks entry into the concrete `Contra_SSKBZ_[A-E]` and
`Contra_SCKBZ_[A-E]` functions; a handler trace with
`policyEntered = false` is not a Fury expert label.

## SavedVariables snapshot boundary

The previously inspected Cat2 SavedVariables selected a feral-druid profile. The first
BrainOfCat policy card is deliberately Fury-Warrior-only, and no Warrior Cat2
profile is present in the inspected SavedVariables. The addon can load on the
current character, but a meaningful first validation requires a Warrior
character and a Cat2 configuration containing `warrior_o2o_policy_brain`.

Contra's macro dispatcher supports Mage, Rogue, and Warrior, not Druid, so it
cannot be a same-state expert for the currently selected feral profile.
