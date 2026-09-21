# Upper Kara 连续两波序列搜索 v7（2026-09-20）

## 结论

v7 全链路已完成，但没有找到优于 Cat 的非零策略。正式冻结结果是 exact-Cat zero wrapper；它在 256 个全新 held-out seeds 上与 Cat 逐 seed 完全相同，同时高于两条当前可执行的 Contra 基线。这个结果验证了训练、冻结、分片评估和 Cat 回退链的一致性，不代表新算法产生了提升。

## 实验与结果

- run：`wave-sequence-37-hetero-20260920-v7`
- 环境：`multi_two -> single_long` 连续 native dynamic-v3，两波间传递怒气、CD、buff、装备与 proc 状态。
- 搜索：每个 loadout 37 个 searched programs，即 1 个 exact-Cat zero 加 36 个非零序列。
- 训练：1024 个语义前置产物完整；freeze task `t97163` 正常完成。
- 评估：256 shards、1024 lanes，全部 COMPLETE；summary task `t97422` 正常完成。

| 策略 | held-out 平均有效伤害 | 平均 DPS | 相对冻结策略 |
|---|---:|---:|---:|
| frozen searched winner | 16895.38 | 526.78 | — |
| Cat | 16895.38 | 526.78 | 0.00，95% CI [0, 0]，0/256/0 |
| deployed Contra | 13211.33 | 409.95 | +3684.05，95% CI [+3227.80, +4140.29] |
| Contra_new / source | 13372.00 | 414.10 | +3523.38，95% CI [+3070.29, +3976.46] |

冻结程序 `cat-residual-overlay::contra_turtle_burst__mighty_rage::zero` 的 origin 虽为 SEARCHED，但其语义是 exact Cat fallback、无插入动作；它不是新发现的技能序列。六个到达延迟 strata 中冻结程序也都与 Cat 精确相同。

## 为何 36 个非零候选全部输给 Cat

v7 实际搜索空间只有两波的 GCD 排列：每波强制 BT、WW、Turtle Slam 各一次，形成 `6 x 6 = 36` 个组合。Cat、Contra、Contra_new 和 offline guide 只提供 provenance ID，动作内容没有参与候选生成。

所有非零候选还共享以下固定骨架：

- wave 1 固定目标顺序 `0 -> 1 -> 0`，三个步骤全部强制 Cleave；
- wave 2 固定目标 2，三个步骤全部强制 Heroic Strike；
- Death Wish 在 wave 1 满足 guard 时先占一个 GCD，否则到 wave 2 重试；
- 动作或 queue 暂时不 ready 时保持 cursor 并 WAIT，而不是让 Cat 使用当前可用技能；
- 不能省略 Slam、重复高价值技能、调整序列长度、动态换目标、选择 KEEP/CANCEL、改变爆发时机或直接在该决策回退 Cat。

四个 loadout 的 36 个候选在 130 个 proposal pairs 上全为负：最佳配对差分别约为 mighty rage `-880.05`、no potion `-638.11`、quickness `-364.73`、rage `-561.27`。中位差分别约为 `-2120/-1551/-1749/-1484`。因此非零候选没有进入 validation；门禁是 `ZERO_RESIDUAL_SELECTED_ON_TRAIN`，不是 validation 后失败。

## 工程修复

1024 规模下发现并修复了三个独立的 SSH `Argument list too long` 路径：计划输出存在性、semantic terminal projection、required remote paths。三处均按 128 个路径分批；对应 1024/2048 规模和失败停止测试已加入。相关远端/契约测试共 57/57 通过。

## 下一步

停止扩展 v7 的固定三技能排列。v8 改为 Cat-relative search teacher：从 exact Cat 实际访问的当前可观察状态出发，独立尝试 GCD、queue、等待、目标和爆发 ActionPlan，随后恢复同一个 Cat session 并评价完整两波结果；再把跨 seed 稳定正收益的分支蒸馏为少量 current-observation guard。exact Cat 继续作为候选零点和冻结下界，teacher 标签、蒸馏训练、selection validation 与 final held-out seeds 彼此分离。

当前证据仍是合成两波、同 build/loadout 的 simulator development evidence，不是 Upper Kara 实战优越性，也不是离线高手策略已被完整复现。
