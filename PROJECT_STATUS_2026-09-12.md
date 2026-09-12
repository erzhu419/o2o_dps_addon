# O2O-DPS / BrainOfCat 项目状态总结（2026-09-12）

## 1. 结论先行

本项目已经形成一条可运行、可审计但尚未闭环的研究链路：

```text
Chronicle 历史战斗数据 + Cat / Contra / Contra_new 专家策略
                              ↓
     波次与团队行为重建 + Turtle WoW 动态仿真 + 多 seed 搜索
                              ↓
              候选动作序列 / 反馈式策略
                              ↓
          Cat2_new 在 Windows WoW 客户端执行与 Shadow 验证
```

当前应当明确区分三件事：

1. **工程链路已经打通很多环节**：Chronicle 官方 API、50 个历史 raid、本地紧凑数据、战斗标定、动态目标仿真、Cat/Contra 适配、多 seed 协议、六节点 HPC 和 Cat2_new 候选执行器都已有代码与测试。
2. **本次 68 分区训练是队友响应/团队环境模型训练**，它在拟合队友的下一事件、时延、目标和伤害，不是直接训练最终 Warrior 动作策略，也不是 Cat/Contra DPS 对局。
3. **目前不能声称最终策略已经胜过 Cat**。在较早、较窄的仿真设置中出现过正结果，但 absolute-seed 补充回放中的优势显著缩小；另一个小型目标生命值 smoke 也输给 Cat。最新 Cat-focused 多 seed 合成诊断没有候选通过：三个候选对 Contra260817 的配对均值都显著为正，对 Cat 的点估计则全部为负（其中 1 个显著、2 个不显著）。

因此当前部署状态仍是：**候选策略不接管 Cat2_new 的实机动作；继续保留 Shadow/诊断边界。**

## 2. 项目边界与代码规模

`o2o-dps` 仓库只发布本项目自有的数据处理、建模、仿真适配、搜索、评估、测试和脚本。以下内容是本地输入或第三方工程，不进入仓库：

- `offline_data/` 和 Chronicle 原始/派生大数据；
- `Cat/`、`Cat2/`、`Cat2_new/`、`Contra/`、`Contra_new/`；
- `wowsims-turtle/`、`DPSSim/`；
- 构建出的 simulator 二进制、HPC 运行产物、机器本地站点配置；
- `GPT6_advice.md` 等本地评审材料。

边界详见 [NOTICE.md](NOTICE.md) 和 [.gitignore](.gitignore)。

截至本报告生成时，自有源码树规模为：

| 目录 | 文件数 | 作用 |
|---|---:|---|
| `o2o_dps/` | 192 | Python 数据、仿真、训练、搜索与评估模块 |
| `tests/` | 199 | 单元、契约、回归和小型集成测试 |
| `scripts/` | 15 | Windows/HPC 入口与验证脚本 |
| `configs/` | 21 | 评估协议、专家身份、仿真和 HPC 示例配置 |
| 父目录 `addon/` | 13 | BrainOfCat Lua 实机采集与 Shadow 运行模块；不属于本次 Git 发布树 |

当前研发主线只覆盖 **Fury Warrior**。`Contra_new` 虽然包含所有职业源码，但其他职业尚未完成本项目的建模、训练、验证或部署链路。

## 3. 已实现的系统组成

### 3.1 Windows WoW 实机采集与标定

父目录 BrainOfCat addon 已包含：

- 一键式 Warrior 标定任务、游戏内阶段提示和 reload 后断点恢复；
- client cast、技能 sink、server GO、伤害/未命中、怒气、白字和 next-swing 事件的区分；
- Slam、嗜血、旋风斩、斩杀、顺劈斩、英勇打击、白字怒气、挥击时序等有限状态标定；
- 诊断日志，记录一次样本为什么被接纳或拒绝；
- 当前角色装备、天赋、技能书、动作条、技能线和随身背包快照；
- Shadow pair 导出与后台解码。

宏连按已经不再按“按一次宏 = 一个样本”计数。采样计数依赖实际 sink/client/server 结果链，避免玩家习惯性连按制造伪样本。

当前 Shadow 证据仍只有 12 个 schema-v4 transition fragments：7 个 Whirlwind、3 个 Heroic Strike、2 个 Bloodrage。12/12 assumption-conditioned replay 不等于完整策略一致性；strict runtime conformance 为 `NOT_EVALUABLE`，simulator seedability 为 0 `EXACT`、12 `APPROX_ONLY`、0 `REJECT`。因此这些记录可用于机制诊断，不能授权部署。

最终 export/reload 后，旧标定的持久顶部 guide 已隐藏；只有活动中的 campaign 才显示临时游戏内引导。

仍未由木桩直接覆盖的机制包括多目标、受击怒气、部分装备/附魔 proc、团队减益组合以及真实副本中的不可攻击阶段。这些只能由日志、源码或仿真假设补充，不能伪装成已经实测。

### 3.2 Chronicle 官方 API 与离线 ETL

当前实现包括：

- 无认证 external API 分页抓取；
- `upload_after` 增量同步与水位管理；
- ranking、encounter event stream、人物历史和 exact GUID 连接；
- protobuf 连续 encounter 解码；
- 原始响应留存、规范化事件与状态表；
- 玩家/宠物归属、波次、目标、团队时间线和 leave-one-player-out 背景伤害重建；
- 旧 50 raid 与新增官方 API 实例的 overlap/admission 管理。

最初人工下载的 50 个 raid 已经转为紧凑派生数据。当前 Upper Tower of Karazhan admission 集扩展到 84 个可描述实例，其中 68 个进入本次训练候选划分。服务器共享源曾完成一次数据传输，没有向六个节点各复制一份；本次正式 response run 复用了已有的服务端 Stage5 分区。

9 月 3 日南北公会 Warrior 的 36 码攻击范围 bug 使用 raid 级 `guild + started_at` 规则处理。根据用户提供的信息，9 月 3 日上午已修复；项目使用 9 月 3 日中午作为保守 clean floor。由于日志本身没有攻击距离证据，无法再自动恢复到分钟级准确修复时刻。

### 3.3 每波怪物与团队环境建模

已经实现：

- 从离线事件重建波次和 target identity；
- 逐目标 HP、死亡、retarget；
- 动态护甲和可攻击 schedule；
- leave-one-player-out 团队背景伤害；
- 每个队友的下一事件类型、事件时延、目标模式和正伤害量模型；
- old50 slot overlay，使同一历史波次可替换被研究 Warrior，而不是把整只怪交给单人打完。

仍不完整：

- 很多怪物初始 HP、站位、拉怪分组和战术延迟仍是具名推断，而非 Chronicle 直接真值；
- 目前队友模型还没有生成 source-bound 动态 kill-clock 校准结果；
- 还没有完成“每个同队玩家完整策略 + 全副本跨波冷却”的联合仿真。

### 3.4 Turtle WoW simulator 接线

Warrior 主实验使用 `wowsims-turtle`。动态桥已经支持：

- 持久 simulator 进程；
- 精确动作、queue/cancel/wait；
- 逐目标生命、死亡和切换目标；
- 护甲/可攻击变化；
- 团队背景伤害；
- 中央 idle 时间推进；
- 多 seed、同 seed 配对策略对比。

`DPSSim` 目前没有足够可信的 Warrior 技能模型，只作为架构和其他职业参考，不承担本项目 Warrior 主结果。

### 3.5 Cat、Contra、Contra_new 与 Cat2_new

- Cat、部署版 Contra 和 Contra260817（即 `Contra_new`）均有身份清单、状态映射、有序 sink 执行和 full-policy rollout 适配。
- Contra 没有因为 `UnitHealthMax`、百分比或 `UnitClassification` 等字段问题被丢弃；可由离线重建修复的输入继续补强。
- 当前适配器仍是对 Lua 源码分支的翻译，不等同于原插件在真实客户端里的 exact runtime、acceptance 和 result 语义。
- 实际运行的加密 `Contra.lua` 与 `Contra_ALL.lua`/当前源码适配器是否完全等价仍未证明；源码 manifest 不能替代加密 runtime parity 证据。
- Cat2_new 已有能力 manifest、动作计划、候选执行器和反馈循环。它是最终动作执行端和候选生成器，不被当作第三个“专家投票器”。

### 3.6 搜索与评估协议

已经建立：

- 早期 beam/前缀搜索；
- 参数化 Fury 策略与 13 维 Cat-gap 搜索空间；
- 同 seed 配对评估；
- 256 development seeds、256 selection seeds、1000 final-confirmation seeds 的冻结协议；
- 单目标、多目标、总体及各 baseline 分层统计；
- HPC worker、reducer、dispatch 和结果门禁。

需要特别说明：协议中“准备了 1000 final seeds”不等于已经执行正式 1000-seed final confirmation。正式 final 只允许在候选与协议封存后，收集与当前 50 raid/组件不相交的 **恰好 50 个完整新 UTK raids**，并覆盖至少 20 个新的 guild/player leakage components；当前 84 个描述性 corpus 和 68 个训练 cohort 不能回填充当这批 final corpus。

## 4. 用户要求如何改变了项目

| 用户要求 | 已做改变 | 尚未完成 |
|---|---|---|
| 最终仍是 Windows WoW 插件，由 Cat2 执行 | 建立 Windows 原生采集、Shadow、Cat2 profile/runner、动作计划和反馈链 | 候选尚未获准接管实机动作 |
| 游戏内一键自动标定、少 reload | 自动任务、顶部提示、断点保存、后台 importer 和诊断日志 | 实机覆盖仍限于可触达状态 |
| 疯狂按宏不能制造样本 | 只有真实动作/结果链完成才计样本 | 还需要更多完整 episode/reward 证据 |
| 50 raid 全量处理，不逐页手工缩减 | 完成并行导出、导入和紧凑特征化 | 新增 raid 最终确认集未冻结 |
| Chronicle API 持续增量抓取 | no-auth API、分页、`upload_after` 和去重已实现 | leaderboard/非核心 stream 的使用仍可扩展 |
| 9 月 3 日后数据才可信 | raid 级 guild/time 清洗；中午为保守阈值 | 无法从日志识别上午的精确修复时刻 |
| 胜负以多 seed 为准 | 建立 256/256/1000 配对协议，已运行冻结合成 256-seed 诊断 | 正式科学验证与 1000 final 尚未运行 |
| 本机只 smoke，大规模去六节点 | node001–006 环境、源码/runtime 闭包、dispatch/reduce 已建立 | Cat-gap 64 候选重型搜索尚未启动 |
| 每波考虑全队 DPS 与怪物快速死亡 | 团队时间线、背景伤害、动态死亡/retarget、队友响应模型已实现 | 动态 kill-clock 和全队逐人策略尚未闭合 |
| Cat/Contra/Contra_new 都作为强 baseline | 三套 expert 都保留并持续补足输入与 sink 语义 | exact Lua runtime fidelity 未闭合 |
| 固定装备/天赋先比较策略 | 当前 Cat-gap 固定同一 simulator request、装备、天赋与 matched seeds；只改变 policy | 配装、天赋、消耗品和跨波规划未做 |
| 先优化内存和时长 | 最小真实分区及完整 68-output reducer 均完成流式优化、独立 source identity 和 full-core 等价核对；最终版同时降低墙钟与峰值内存 | 尚未对正式 Go 搜索做独立容量 benchmark |

## 5. 已有胜负证据及其边界

### 5.1 早期 instance-disjoint duration gate

较早的三策略固定时长 gate 从 1 个 Chronicle instance 训练/选择候选，留出其他 49 个 instance；它包含 392 个 scenario families、16 matched seeds 和 18,816 rollouts：

| 策略 | 加权 DPS |
|---|---:|
| Candidate | 610.338909 |
| Cat translation | 586.167895 |
| Contra translation | 464.059068 |

该候选对 Cat 为 +24.171014 DPS，对 Contra 为 +146.279841 DPS，并通过当时按 target-count 分层的 margin 与胜负门禁。但这只适用于固定护甲 1721、等级 60、历史持续时间和 `SOURCE_DERIVED` Lua 分支翻译等假设；它不是 exact Cat/Contra Lua runtime，也不能推广为真实 WoW 胜出。

### 5.2 literal/absolute-seed 补充回放

旧 bridge 将 reported seed 又作为 `Reseed` offset，使实际 RNG seed 为标记值的两倍；这不破坏同一错误 seed 内的配对，但标签不是 literal seed。修复后的四策略回放包含 392 families × 16 matched seeds × 4 policies = 25,088 rollouts：

| 比较 | 结果 |
|---|---:|
| Candidate 总体 | 597.735135 DPS |
| Cat 总体 | 593.061237 DPS |
| Candidate - Cat | +4.673897 DPS |
| Cat2 源码翻译 | 538.181211 DPS |
| Contra 源码翻译 | 458.230568 DPS |
| Candidate 单目标 | 556.436114 DPS |
| Cat 单目标 | 571.343624 DPS |

总体 candidate-Cat 优势缩小到 +0.788%，且单目标方向反转为 −14.907510 DPS。该 artifact 保存的配对检验以 Cat2 为 focal policy，所以它不是新的 candidate-vs-Cat 正式 gate。

### 5.3 singleton-health 小型 smoke

另一个仅 9 rollout 的单族 singleton-health smoke 中，候选同样输给 Cat；规划中的 144-rollout 扩展并未启动。这是独立于 absolute-seed 回放的目标生命语义诊断。

### 5.4 最近策略搜索与 3 × 256-seed Horizon-v2 确认

早一轮 8-arm、每 arm 256 matched-pair 的 non-voting screening 中，相对 Cat 的前四名为 `bt_hamstring_cat_timing` +16.120054、`bt_wait_cat_timing` +9.647119、`ww_hamstring_cat_timing` +3.624806、`bt_hamstring_disabled` +0.593353 DPS。这一阶段没有执行统计检验，不参与投票；它说明搜索能在 development seeds 上找到表面上高于 Cat 的参数，不证明可复现。

当前最新完整结果是修复评分 horizon 右截断后的 fresh-seed 确认。每个 arm 使用 seeds 513–768 的 256 个 matched groups，并运行 candidate、Cat、Contra260817 和 deployed-Contra 四条 lane，即每 arm 1,024 rollouts，3 个 arm 共 3,072 rollouts。Cat 与 Contra260817 的可用均值分别为 415.077664 和 358.187963 DPS：

| Candidate arm | Candidate 均值 | 相对 Cat | 相对 Contra260817 | 选择 |
|---|---:|---:|---:|---|
| `ww_hamstring_cat_timing` | 399.566249 | −15.511415（−3.737%）；112/0/144 W/T/L；Holm `p=0.02146`，显著更差 | +41.378286（+11.552%）；170/0/86；Holm `p=1.32e-7` | 不选择 |
| `ww_wait_cat_timing` | 412.167491 | −2.910173（−0.701%）；115/6/135；Holm `p=1.0`，不显著 | +53.979528（+15.070%）；174/0/82；Holm `p=8.65e-12` | 不选择 |
| `bt_hamstring_cat_timing` | 410.489574 | −4.588090（−1.105%）；121/0/135；Holm `p=1.0`，不显著 | +52.301611（+14.602%）；171/0/85；Holm `p=1.17e-11` | 不选择 |

结论：在该 20.001 秒单场景、同一 simulator request/装备/天赋和 matched seeds 的合成假设下，三个候选都显著高于 Contra260817，但没有任何候选高于 Cat。按冻结 maximin 排名，`ww_wait_cat_timing` 是三者中最好的，但仍比 Cat 低 0.701%，因此结果必须是 `NO_SELECTION`，不能把它当成新的可部署策略。该 run 的 `scientific_result_available=false`、`deployment_allowed=false`。第四条 deployed encrypted Contra lane 在每个 arm 都 256/256 incomplete，不具备比较资格；这不等于候选击败了部署版 Contra。

### 5.5 离线高手 baseline

历史 Fury 玩家 policy v4 使用 182,481 个 strict-controllable labels 做动作预测。低基数 backoff 将 contextual log-loss 从 2.420177 降到 2.407410、ECE 从 0.051123 降到 0.025927，但 top-1 accuracy 从 0.318828 降到 0.246174，top-3 从 0.577583 降到 0.528488；联合门禁失败，已保留为 `RETAIN_NEGATIVE_NO_NEXT_HPC`。v5 正温度校准只是预注册计划，还没有执行。

更重要的是，这些是“能否预测历史玩家下一个动作”的指标，不是同装备 DPS。历史玩家行为模型尚未接成同 request/装备/天赋的 dynamic-v5 simulator lane，所以当前没有候选与托尼牛、桃姬儿或其他离线高手之间的合法 DPS 比较数字。

### 5.6 当前科学结论

当前胜负可以压缩成一张表：

| Baseline | 当前最强直接证据 | 结论 |
|---|---|---|
| Cat | fresh 256 matched seeds 下最优 maximin arm 仍为 −2.910 DPS（−0.701%，不显著） | **没有击败 Cat** |
| Contra260817 / `Contra_new` | 三个 arm 均为 +41.378 至 +53.980 DPS（+11.552% 至 +15.070%），Holm 显著 | **只在该受限合成诊断中胜出** |
| 部署版加密 Contra | 当前 lane 256/256 incomplete | **不可比** |
| 离线高手 | 只有行为预测校准；尚无同装备 dynamic simulator lane | **不可比** |

因此，算法已经找到“对 Contra_new 有明显改善、对 Cat 非常接近”的参数化策略，但没找到通过双 baseline 门禁的策略。当前正确产物是 `NO_SELECTION`，不是把 `ww_wait_cat_timing` 强行当作新 brain。即使合成 lanes 使用同一 simulator request/装备/天赋和 matched seeds，也没有真实客户端 exact proc/runtime fidelity 或历史玩家同装备对照。

## 6. 本次完整并行队友响应训练

### 6.1 固定输入与调度

- source closure SHA-256：`088fb1ee9bff05c5334eacc538fe69d73084857b0eb5def4f88c80073aec482b`
- dispatch SHA-256：`509bfe9c5645994825e4fe88348cfc0d9fe5caf093f04b10ab5e1141c95bdb1b`
- 分区：68（65 TRAIN + 3 VALIDATION）
- 分配：node001–006 为 `11 / 11 / 12 / 11 / 11 / 12`
- runtime：同一 Python 3.10.14 环境；每个 partition 单线程 Python worker；`GOMAXPROCS=1`
- 监控：每节点 5 秒采样 worker 数、聚合 RSS、聚合 CPU、load1 和可用内存；每任务保存 `/usr/bin/time -v`

这里的“训练”是纯 Python hierarchical marked semi-Markov empirical count/backoff 聚合：map worker 单遍扫描分区并产生可合并 count tables。它不是神经网络、RL 或 GPU 训练，因此 GPU 空闲是预期行为。

### 6.2 完成与性能

<!-- FORMAL_TRAINING_RESULT_BEGIN -->

正式 run 已完整结束：

| 项目 | 结果 |
|---|---:|
| worker 完整性 | 68/68 成功，0 failed，0 nonempty stderr |
| 并行 map 墙钟 | 1,506 秒（25:06） |
| reducer 墙钟 | 1,776.91 秒（29:36.91） |
| 端到端墙钟 | 3,403 秒（56:43，含 map→reduce 启动间隔） |
| 单任务耗时 | min 323.70 秒；median 905.64 秒；P90 1,120.94 秒；max 1,505.00 秒 |
| 单任务 peak RSS | median 1.40 GiB；P90 1.83 GiB；max 2.44 GiB |
| 同步集群峰值 | 68 workers；约 68.2 CPU cores；55.55 GiB RSS |
| 冻结基线 reducer peak RSS | 47,495,580 KiB（45.30 GiB） |
| 最终 gzip | 134,425,063 B（约 128.20 MiB） |
| 冻结基线完成时 attempt 目录占用 | 939,225,525 B（约 895.71 MiB） |
| 冻结基线结果 content SHA-256 | `49e1cd09be736f2eb2e6976451cc65330772b2a70a4771f69f51f9049bc1fd0f` |

完整性汇总：4,811 waves、27,257,822 compiled exact-player rows、31,874,870 Stage5 events、32,108,577 exact trace rows；输入为 6,967,035,135 compressed bytes、108,519,977,824 logical bytes。Arm A 只保存 4,590 个训练和 221 个验证 schedule descriptors，没有执行或指标。

B/C/D development metrics 如下；NLL/MAE/Brier 越低越好，accuracy 越高越好：

| Variant | mark NLL | delay log-MAE | delay Brier | target accuracy | damage log-MAE |
|---|---:|---:|---:|---:|---:|
| B no-GUID class/spec backoff | 1.773095 | 1.885780 | 0.559946 | 0.967418 | 0.505811 |
| C GUID→class/spec hierarchy | 1.773095 | 1.885780 | 0.559946 | 0.967418 | 0.505811 |
| D without other-team action/damage intensity | 1.769371 | 1.886172 | 0.559328 | 0.967882 | 0.511905 |

B 与 C 在断开身份组件的 validation 上只剩浮点尾差。D 的 mark NLL、Brier、target accuracy 略好，且 target/damage 缺失训练支持较少，但 delay 和 damage log-MAE 更差。由于没有预注册的选择阈值，不能据此挑选 D；结果状态仍是 `DEVELOPMENT_VALIDATION_INCOMPLETE_NO_ADOPTION`。

<!-- FORMAL_TRAINING_RESULT_END -->

### 6.3 单进程 reducer 优化 A/B

旧 reducer 使用冻结源码跑完作为基线。优化版只复用完全相同的 68 份 immutable worker outputs，不重扫 Chronicle 或 Stage5；它必须单独记录新 reducer implementation identity，不能冒充旧 source closure。A/B 同时比较科学 payload 等价性、wall time 与 peak RSS。

<!-- REDUCER_OPTIMIZATION_RESULT_BEGIN -->

两轮完整 68-output strict replay 都复用同一 dispatch 和完全相同的 68 份 immutable worker outputs，没有重扫 Chronicle/Stage5。第一轮 source closure 为 `002499aa53bcc5dfac48ed2da2028eca3a0cc3249d1346321f9c9c6ae5bf7c93`；最终第二轮为 `95a6e040f73202bfb47e8e5fd40731dbcf34ec3018404bf1cdd6f7ad8520dffe`。

| 指标 | 冻结基线 | 第一轮 strict replay | 最终 strict replay | 最终版相对基线 |
|---|---:|---:|---:|---:|
| wall time | 1,776.91 秒（29:36.91） | 1,903.84 秒（31:43.84） | 1,556.88 秒（25:56.88） | `1.1413x`；减少 220.03 秒（−12.383%） |
| peak RSS | 47,495,580 KiB（45.30 GiB） | 38,411,240 KiB（36.63 GiB） | 36,476,896 KiB（34.79 GiB） | 减少 23.199% |
| result gzip | 134,425,063 B | 134,425,017 B | 134,425,021 B | 科学 payload 等价；provenance 不同 |

三版中正式选用最终第二轮实现：它同时具有最短墙钟和最低峰值 RSS，且通过 full-core 等价门禁。冻结基线与第一轮只作为对照证据保留，不再作为后续 reducer 入口。

第一轮总计时额外包含旧结果的完整解压、巨型 JSON 树物化及多次 canonical 校验，所以虽然内存下降 19.127%，总墙钟反而增加 7.143%。最终版使用冻结 addressed-result SHA 和 stable/addressed 字节一致性，不再解析旧大结果，同时保留更强的 `FULL_CANONICAL_CORE_SHA256_EQUAL` 门禁。

最终版分阶段耗时为：replay preflight 0.645 秒，worker merge 与 core materialization 1,298.132 秒，full-core 等价核对 73.330 秒，content address + gzip + publish 169.278 秒；四项共 1,541.385 秒，函数端到端 1,556.607 秒中剩余约 15.22 秒为未分段的返回/大对象释放，`/usr/bin/time -v` 的 1,556.88 秒只再多 0.273 秒进程起停开销。同时排除 replay preflight 和专属等价核对后，production-like 阶段和为 1,467.410 秒，比冻结基线约快 17.418%；这是 phase 近似，不是另一个独立进程 benchmark。当前主要单进程瓶颈已明确收敛到 worker merge/core materialization，而不是 API、原始日志重扫或 GPU。

最终 replay 以 exit 0、空 stderr 发布；stable/addressed 文件字节完全一致，`gzip -t` 通过。结果 content SHA-256 为 `0b44dffc7611103bfa829141e66ae301f3ba75115242623004cc4db86a2f794e`；它因新增 reducer provenance 与基线 SHA 不同，不表示科学结果发生变化。模型状态仍是 `DEVELOPMENT_VALIDATION_INCOMPLETE_NO_ADOPTION`，`model_adoption_authorized=false`。

<!-- REDUCER_OPTIMIZATION_RESULT_END -->

### 6.4 训练指标与含义

该模型的 development-validation 指标包括：

- next-mark negative log-likelihood；
- inter-event delay log-MAE 与 Brier；
- target-mode accuracy；
- positive-damage log-MAE；
- A 仅保留 source-bound 固定历史 schedule 描述符且不执行；B/C/D 才物化 development metrics。

`dynamic_rollout_team_kill_clock_log_mae` 当前仍为空，正式状态是 `DEVELOPMENT_VALIDATION_INCOMPLETE_NO_ADOPTION`，`model_adoption.authorized=false`。即使 68 个 worker 和 reducer 全部成功，也只表示完整性通过并成功物化开发指标，不代表模型被接受，更不代表 Cat2 候选策略已经获胜。

## 7. 性能、并行与服务器占用

### 7.1 为什么 TUI 看起来负载很低

本次 68 分区使用 68 个单核 Python 进程，平均每节点 11–12 个。每台节点有 192 个逻辑核，整个集群共 1,152 个逻辑核，因此理论占用约：

```text
68 / 1152 = 5.90%
```

所以 load1 每节点约 10–12、CPU 每节点约 1,100%–1,200% 是符合预期的；在 192 核节点顶部看起来变化很小。

这批工作不是 Go simulator，因此不是“Go 无法并行”。Python worker 本身单线程；`GOMAXPROCS=1` 是环境/契约 pin，并不是限制这些 Python worker 相互并行的机制。

当前入口通过低层 `scheduler.run_on(node, command)` 直发节点，再由节点上的 detached manager 管理本批进程。它不注册 scheduleurm 任务表，所以 queue/TUI 中可能看不到名为 o2o 的作业；节点 OS 的 CPU/load/RSS 仍真实增长。真正的 Cat-gap Go 搜索按“一 worker 一持久 bridge”设计，可以使用远多于 68 个并发 worker。

### 7.2 历史所谓“1000 次”应如何解释

正式 `1000 master-seed final confirmation` 从未执行。最近完整记录是：

- 3 个 candidate arm；
- 每 arm 256 matched seeds × 4 policies = 1024 rollouts；
- 总计 3072 rollouts；
- 每节点每 arm 使用 `xargs -P40`，3 arm 并行，理论最多 120 worker/节点、720 worker/集群。

实测时长：

| 阶段 | 时长 |
|---|---:|
| 3 个 arm 的 rollout 生成 | 30.215 秒 |
| 随后的单进程 horizon analysis | 1,764.628 秒（29:24.628） |
| 从生成开始到最终分析结束 | 1,830.837 秒（30:30.837） |

结论不是 simulator rollout 慢，而是当时严格汇总/分析成为主要瓶颈。后续若做大规模搜索，应继续放在六节点；本机只做 smoke。正式 final-1000 则必须等冻结候选和新 held-out corpus，而不是为测吞吐提前消费 final 协议。

### 7.3 早期最小真实分区优化证据

同一个最小真实 teammate-response 分区的 A/B：

| 指标 | 优化前 | 优化后 | 改善 |
|---|---:|---:|---:|
| wall time | 566.80 秒 | 310.23 秒 | 1.827×，减少 45.266% |
| peak RSS | 10.90 GiB | 1.36 GiB | 减少 87.517%，约 8× |
| 输出大小 | 3,764,905 B | 2,488,458 B | 减少 33.904% |

服务端严格语义核对通过：B/C/D 重建模型一致、Arm A 描述一致、source bindings 一致，11 项 stream counter 一致，包括 13 waves、129,514 exact player rows、140,971 Stage5 events、142,422 exact trace、31,187,300 compressed bytes 和 493,264,743 logical bytes。

## 8. 装备、天赋和 Turtle WoW 数据库覆盖

### 8.1 角色在线时能读取什么

BrainOfCat 当前可读取当前角色：

- 装备槽 1–19 的 item link 与基础 `GetItemInfo` 字段；
- 已学习天赋的 tab/index/name/tier/column/rank/maxRank 和 tooltip；
- 技能书、动作条、技能线；
- bag 0–4 的随身物品；
- 当前已知物品的 tooltip 和静态面板信息。

它不能仅凭“角色在线”自动枚举整个 Turtle WoW 物品数据库，也不能读取其他玩家、银行、邮件，或从文本描述可靠推导所有 proc/PPM/ICD/隐藏效果。

### 8.2 simulator 数据库能提供什么

本地 `wowsims-turtle/assets/database/db.json` 是较广的静态目录，当前快照约含 11,000 items、207 enchants、27,917 spells、172 consumables 和 359 sets，但不保证和当前服务器版本完全同步，也不保证每个物品效果都已实现或标定；zones/npcs/factions 与 random suffixes 仍为空或有明显缺口。数据库载入还区分 simmable 与 leftover。

合理做法不是把全数据库大文件到处复制，而是：

1. 为离线数据实际出现的 item ID 建小型索引；
2. 合并 `db.json` 与 leftover 信息；
3. 区分“目录里知道”“sim 已实现”“实机已标定”“Cat2 可执行”四种状态；
4. 只对缺失或高影响物品做定向 override、效果实现和实测。

### 8.3 offline 是否包含每个角色完整装备/天赋

不保证。

- 原始 All Activity CSV 只有职业/种族、天赋树点数、装备槽数量等粗信息，没有完整 item ID 和逐天赋 rank。
- 官方 API `combatant_info` 可给 item/enchant/temp-enchant/gem ID 和 talent 字符串，明显更完整，但没有物品名称、属性、proc 机制。
- 当前 84 个实例共有 290,533 条 CombatantInfo：gear 字段均非空；289,139 条带 talents，按 CombatantInfo 消息计覆盖 99.520192%，缺 1,394 条。
- 62/84 个实例至少有一条 talent 缺失消息；另有 1 名 metadata roster 玩家完全没有 CombatantInfo，并被正确保留为 `UNAVAILABLE_NOT_IMPUTED`。
- “gear 非空”不等于每个装备槽都完整；当前 admission 还需要升级为槽位、玩家和决策时点级覆盖审计。

当前在线角色快照只能补当前角色，不能回填历史玩家缺失数据。

## 9. 当前主要问题

1. 本次队友响应模型只完成预测头，source-bound 动态 kill-clock 尚未物化。
2. Cat/Contra/Contra_new 仍缺 exact Lua runtime/full-policy fidelity。
3. 最新 Cat-focused 多 seed 测试没有击败 Cat。
4. 历史高手模型尚未成为同装备 simulator baseline。
5. 场景 HP、护甲、分堆、站位和不可攻击期仍含假设。
6. 13 维 Cat-gap 的 64 候选 successive-halving 重型搜索尚未开始。
7. 全副本跨波规划、消耗品、合法换武器、天赋与装备外层搜索尚未实现。
8. 候选未蒸馏成可部署 Cat2_new live policy，也未经过真实 raid 配对验证。
9. 正式 final-1000 还缺封存后的恰好 50 个完整新 UTK raids，且需覆盖至少 20 个新的 guild/player leakage components。
10. 装备/talent admission 仍需从“字段非空”升级为角色/槽位/时点完整性。

## 10. 下一阶段建议顺序

1. 报告本次 teammate-response B/C/D 开发指标；当前没有预注册接受阈值，不能仅凭这些指标选择模型。
2. 冻结并预注册 exact source-bound dynamic rollout kill-clock 的选择与准入规则，再将队友模型接入 old50 动态波次，用留一 Warrior 的真实结束时间验证。
3. 补足 Cat/Contra/Contra_new 最影响胜负的 runtime/sink 差异，冻结 baseline 版本。
4. 对 64 个固定装备/天赋 Cat-gap 候选执行 successive-halving：低 seed 筛选后才增加预算，避免所有候选直接上 1000 seeds。
5. 候选必须同时通过 Cat 与 Contra_new 门槛；Cat 仍是当前更强的主要 baseline。
6. 将通过者蒸馏为 Cat2_new 可执行反馈策略，在 Windows 客户端先 Shadow，再做受控真实验证。
7. 候选与协议封存后再收集恰好 50 个完整新 UTK raids、至少 20 个新泄漏组件，最后运行正式 1000-seed confirmation。
8. 最后才开展装备、天赋、消耗品和跨波资源的外层优化，避免把配装收益误写成动作策略收益。

## 11. 可复现性与发布状态

- 本报告随 `o2o-dps` 自有源码、配置、测试和脚本提交。
- Chronicle 大数据、HPC 运行产物、第三方工程和机器本地配置不进入 Git。
- 正式训练通过 source closure 与 dispatch SHA 绑定；worker、reducer、时间和资源日志留在服务器 run 目录。
- 冻结基线与优化 replay 使用各自独立的 source closure；二者通过同一 dispatch、68 份 worker receipts 和 full-core replay-equivalence contract 关联。
- Windows 完整集成回归已通过：Go simulator `with_db`、Python 1,812 tests（2 skipped）、BrainOfCat TOC 与已安装 Cat2 合同均成功；Python 段耗时 1,013.688 秒。独立 source-only 回归为 1,486 tests（2 skipped）通过。
- 本批功能代码 commit 为 `92d4e9a196d7b0111472ecac1c03ffa32d84d2fd`，已推送到 `origin/main`。
- Shadow/静态检查不授权实机部署；真实 WoW 行为变更仍需 Windows 客户端 `/reload` 和指定场景验证。
- `cat_fury_full_policy_readiness_v4.py` 中的 Cat SavedVariables 默认路径是冻结历史字节的有意例外；为了便携性修改它会改变既有 source identity。新调用者应通过 CLI/config 显式传入 Cat root/SavedVariables 路径，而不是更新历史 pin。
