# Upper Kara 连续波因果搜索：v5/v6 实验记录（2026-09-19）

## 本轮裁定

- 原 48-shard 训练的约 1 小时 ETA 不是尾声；停止旧任务并改为 256 shard 是正确决定。
- v5 的 256-shard 训练墙钟为 546.1 秒；v6 为 621.4 秒。两者都把训练从约 1 小时缩到约 10 分钟。
- v6 搜到的最佳 HP 条件路由在训练集有明显正均值，但没有通过独立 validation 门禁；正式冻结结果仍是 exact Cat。
- 因此当前结论不是“新算法超过 Cat”，而是：在该模型、同装备和同资源规则下，Cat 仍是保留策略；它在新的 held-out seeds 上显著高于两条公平 Contra 对照。
- 这仍是 development-only 的两波合成模型证据，不是 Upper Kara 实战、全副本或 Cat2 部署证据。

## 协议

- run：`hp-guarded-309-shard256-20260919-v6`
- build/loadout：`live_bonereaver` / `contra_turtle_burst__mighty_rage`
- 搜索候选：309 个；另有 Cat、部署 Contra、Contra_new 三个 imported baseline，共 312 条训练 lane。
- 训练 seeds：`720001..720256`；256 shard。
- held-out seeds：`820001..820256`；256 shard，与训练完全不重叠。
- 首波到达条件：`0/1000/3000/5000/7000/9000 ms`，平衡分配。
- 策略只观察当前 HP band、目标可攻击性、动作 ready、队列与怒气；`first_wave_arrival_ms` 不进入策略 observation。
- 当前 HP 阈值为 `20/35/50/65/80%`。候选可在不同 HP band 选择不同的 queue/GCD 原子修改，并保留 exact Cat fallback。

## 选择结果

训练最优候选为：

`cat-hp-guarded-sparse-router::contra_turtle_burst__mighty_rage::0125`

相对 exact Cat：

| 阶段 | 配对平均有效伤害差 | 95% CI | 裁定 |
|---|---:|---:|---|
| train ranking | +637.54 | — | 仅产生候选 |
| independent validation | +142.73 | [-219.69, +505.16] | 下界未过 0，拒绝 |

正式 freeze 状态为 `FALLBACK_ZERO_RESIDUAL_LCB_NOT_POSITIVE`，冻结程序：

`cat-residual-overlay::contra_turtle_burst__mighty_rage::0022`

它是 exact Cat 父策略，不是新的已获准 HP router。

## Fresh held-out 结果

1792/1792 lanes 为 `COMPLETE`，无失败 lane。

| 公平 shared-burst lane | 平均有效伤害 | 平均 DPS | 冻结策略配对差 | 95% CI | W/T/L |
|---|---:|---:|---:|---:|---:|
| frozen exact Cat | 17223.29 | 1016.85 | — | — | — |
| Cat | 17223.29 | 1016.85 | 0.00 | [0.00, 0.00] | 0/256/0 |
| deployed Contra | 16563.56 | 978.58 | +659.73 | [+267.67, +1051.80] | 136/0/120 |
| Contra_new | 16689.10 | 985.50 | +534.19 | [+133.06, +935.32] | 129/0/127 |

这里的正差值证明的是 frozen exact Cat 相对公平 Contra lane 的模型优势；它不能归功于被拒绝的 v6 候选。

分到达条件的 frozen Cat 相对公平 Contra 均值还会反转：

| 首波到达 | vs deployed Contra | vs Contra_new |
|---:|---:|---:|
| 0 ms | +1708.27 | +2518.39 |
| 1000 ms | +1974.44 | +914.70 |
| 3000 ms | +1180.98 | +1272.03 |
| 5000 ms | -1464.30 | -2528.44 |
| 7000 ms | +614.39 | +623.89 |
| 9000 ms | -73.48 | +403.63 |

这说明全局平均会掩盖波次上下文差异，也说明继续增加相同全局候选或只加 seeds 不是主要修正方向。

## 并行与存储

| 阶段 | 任务 | 结果 |
|---|---:|---|
| candidate | 1 | 54.6 秒，312 lanes manifest |
| train | 256 | 621.4 秒墙钟；256/256 done；单 shard 中位 321.1 秒，P90 391.9 秒 |
| freeze | 1 | 184.3 秒 |
| eval | 256 | 205.8 秒墙钟；256/256 done；单 shard 中位 88.4 秒，P90 97.4 秒 |
| summarize | 1 | 36.4 秒 |

训练 shard 在 node001..006 的完成数为 `46/34/44/45/44/43`。从 candidate 实际启动到 summary 完成的端到端墙钟约 32 分 36 秒，其中包含阶段间两次 inventory、提交和调度等待；纯任务运行合计约 18 分 23 秒。

compact-v2 把候选生成移到单一 candidate 阶段。每个训练 terminal 约 1.7 KB，lane sidecar 约 95 KB；远端 v6 结果总计约 102 MB（candidate 8 MB、train 25 MB、freeze 568 KB、eval 69 MB），不再产生旧 v1 的约 3 GB 重复程序回执。按项目规则，本地只拉取了 `frozen.json` 和 `summary.json`，没有拉训练/eval 明细或原始 offline data。

## 下一步

下一轮不应在同一 v6 family 上继续堆 seeds。应利用已经分离的 current/max HP，并把已揭示的 5000/9000 ms 反转转成只依赖在线可观测状态的预先冻结结构，例如剩余可攻击时间代理、当前目标/下一目标状态与 CD 保留价值；再使用全新的 train/validation/held-out seed namespace。任何候选仍必须先胜过 exact Cat 的独立 validation 下界，之后才进入 Cat/Contra/Contra_new 的 fresh held-out 与 Cat2 游戏内测试。
