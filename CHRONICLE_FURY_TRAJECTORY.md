# Chronicle Fury partial trajectory / Chronicle 狂暴战局部轨迹

## 中文

这个 ETL 从 `offline_data/normalized` **只读、流式**读取一个 Chronicle
实例，按玩家 GUID 生成第一版 Fury partial trajectory。它不修改 raw CSV
或 normalized JSONL，也不生成 hash。

Windows 命令：

```powershell
py -3 -B -m o2o_dps.chronicle_fury_trajectory `
  --instance 043b4d65-9c58-4643-b601-62f1684af04a `
  --player-guid 0x000000000066ADAE `
  --player-name Narcissly
```

`--instance` 也可传公开 slug；工具会通过
`offline_data/chronicle_raw/export_queue.json` 对齐三个身份字段：

- `canonical_instance_id`：规范 UUID；
- `source_instance_ref`：normalized 行中实际保存的 instance ref；
- `public_slug`：Chronicle 公开页面 slug。

默认输出到
`offline_data/derived/chronicle_fury_partial_trajectory/v1/`，包含 versioned
JSONL 和同名 manifest。可重复 `--encounter <uuid>` 只选部分 encounter；
`--normalized` 可显式指定输入文件。

语义边界：

- `START` 只是 `candidate_action`，不是按键或已执行动作；
- `GO/FAIL` 是独立的 `action_result`。只有同 encounter、同 spell 且仅有一个
  pending START 时才建立唯一关联；否则保存为 `ambiguous` 或 `unlinked`；
- Turtle WoW 的斩杀是已验证的双 ID 链：动作 `START` 与 wrapper `GO` 使用
  `20662`，触发结果 `GO` 及伤害/未命中使用 `20647`。v1 的同 spell 精确匹配
  因而会把 `20647` 标成 `unlinked`；这不是第二个动作，映射事实记录在 mechanics
  registry，后续重建应按该映射合并；
- `DMG/DEAD` 保存为 reward event，千分位数值会正确解析；`DEAD` 是 Chronicle
  导出的致死伤害行，不自动等同于 Boss 战结束；
- `INFO` 按 encounter 解析 class、race、talent tree 和 gear slot count。这个
  CSV 没有每个装备槽的 item ID，因此 `gear_items=MISSING`；
- `CONS` 保留所有 `key=value`、bare marker、`synthetic`、`PROJECTED` 和
  `confidence`；
- `RES Gain · Rage` 保留 Chronicle 原始单位。`/10` 仅作为
  `INFERRED + uncalibrated_candidate_only` 输出；当前 Narcissly CSV 中没有
  `Loss · Rage`，所以不能把累计 gain 或 `/10` 当成 observed absolute rage；
- 每条记录的 identity、event 和 payload 字段都在 `field_provenance` 中带
  `OBSERVED / RECONSTRUCTED / INFERRED / MISSING`，并保留 `event_index` 与
  原 CSV `csv_line`。

这份 v1 产物适合做事件证据、动作候选和后续 reconstruction 输入；在 O2O
Logger/标定补足按键、queue intent、absolute rage、GCD/swing timer、移动、
距离、目标 HP/armor 前，不是 full-state offline-RL trajectory。

## English

This ETL streams one immutable Chronicle normalized JSONL and emits a
player-scoped Fury partial trajectory. It never edits raw/normalized inputs and
does not create hashes.

Run the PowerShell command above, or pass a public slug to `--instance` and let
the export queue reconcile the canonical UUID, source instance reference, and
public slug. Outputs are versioned JSONL plus a manifest under
`offline_data/derived/chronicle_fury_partial_trajectory/v1/`.

`START` remains only a candidate action. `GO/FAIL` is linked only when there is
one exact pending candidate; ambiguous linkage stays partial. `DMG/DEAD`, INFO,
CONS, AURA, CLASS, and RES evidence retain source anchors and explicit field
provenance. Chronicle rage `/10` is an uncalibrated inferred candidate, never an
observed absolute resource state. The output is therefore suitable for later
state reconstruction, not yet a complete training trajectory.

Execute has a verified dual-ID event chain: action `START` and wrapper `GO` use
`20662`, while the triggered result `GO` and damage/miss use `20647`. The v1
exact-spell matcher therefore reports `20647` as `unlinked`; it is not a second
action, and later reconstruction should merge it through the mechanics registry.
