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

1. **工程链路已经打通很多环节**：Chronicle 官方 API、84-instance 描述性 union（其中 old50 等冻结 cohort 另行绑定）、本地紧凑数据、战斗标定、动态目标仿真、Cat/Contra 适配、多 seed 协议、六节点 HPC 和 Cat2_new 候选执行器都已有代码与测试。
2. **本次 68 分区训练是队友响应/团队环境模型训练**，它在拟合队友的下一事件、时延、目标和伤害，不是直接训练最终 Warrior 动作策略，也不是 Cat/Contra DPS 对局；当前结果尚未接回候选策略优化。
3. **此前“大量搜索”主要是大量重复抽样，不是大量不同策略学习**。近期 Cat-focused 批次一共记录 14,336 条 lane rollouts、3,584 条 candidate rollouts，但实际只覆盖 8 个不同参数向量；Horizon-v1/v2 只是从中复用 3 个候选做 fresh-seed 复核。seed 增加只降低随机误差，不会自动产生更好的动作策略。
4. **目前不能声称最终策略已经胜过 Cat**。在较早、较窄的仿真设置中出现过正结果，但 absolute-seed 补充回放中的优势显著缩小；另一个小型目标生命值 smoke 也输给 Cat。最新 Cat-focused 多 seed 合成诊断没有候选通过：三个候选对 Contra260817 的配对均值都显著为正，对 Cat 的点估计则全部为负（其中 1 个显著、2 个不显著）。
5. **GPT6 第二版诊断/实施方案已经作为当前主线采用，但按门禁逐层落地**：先把历史 build、请求组成、强 baseline 和高手 cohort 变成可审计输入，再进行 build-conditioned residual policy、两波 SMDP 和完整 raid 优化。不会因文件可读或一次 smoke 通过而跳到 superiority 结论。

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
| `o2o_dps/` | 236 | Python 数据、仿真、训练、搜索与评估模块 |
| `tests/` | 238 | 单元、契约、回归和小型集成测试 |
| `scripts/` | 15 | Windows/HPC 入口与验证脚本 |
| `configs/` | 24 | 评估协议、专家身份、仿真和 HPC 示例配置 |
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

为避免静态资料再要求两次 `/reload`，Logger 现在会在 `PLAYER_LOGIN` 自动采集包含未学习 rank-0 位置的完整三棵天赋树、`GetBuildInfo()` client build、装备与技能，并通过 Nampower `WriteCustomFile` 即时写入 `BrainOfCatStaticProfiles.jsonl`。最新一次 `/reload` 已接纳“意踟躇”（GUID `0x0000000000654E8A`）的 source seq `24282`、client build `7272`，P0 receipt 为 `P0_STATIC_ADMISSION_READY`；这是一次性登录提示，不恢复旧的常驻标定顶部面板，也不需要再为本阶段重复 `/reload`。

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

新的 `historical_build_catalog_v1` 已把 CombatantInfo 消息归并为 `(server, realm, player GUID, instance, build segment)`：84 个实例、1,821 名唯一玩家、3,541 个 player-instance、91,577 个 build segment；290,533 条原消息中有 198,957 条相同重复观测。目录保留每槽 `EQUIPPED/EMPTY/MISSING/AMBIGUOUS`、原始三树 rank、装备/附魔/特效覆盖和因果有效区间，不再用消息数冒充玩家数。旧 23-instance Fury decision cohort 的同源严格前缀 join 为 72,236/76,257（94.7270%）；排除唯一 404 实例的 3,610 行后为 99.4343%，另有 394 条 late-only INFO 和 17 条 GUID mismatch 被拒绝。该率只描述旧 decision cohort；84-instance build catalog 尚无同一 population 的决策流，二者没有被交叉拼接成虚假全局率。

9 月 3 日南北公会 Warrior 的 36 码攻击范围 bug 使用 raid 级 `guild + started_at` 规则处理。根据用户提供的信息，9 月 3 日上午已修复；项目使用 9 月 3 日中午作为保守 clean floor。由于日志本身没有攻击距离证据，无法再自动恢复到分钟级准确修复时刻。

命名参考层已把托尼牛、桃姬儿和围观群众三爷从 exact name 解析到唯一 GUID，之后只按 GUID 汇总。三人当前可用 clean 数据均来自 2026-09-09 同一场 raid，共 30 个 encounter observations、3 个 player-raid memberships，全部标为 `Arms`；另有修复前 110 条可疑观测被明确排除。DPS index 本身不含动作请求、next-swing queue 或 target-switch 意图，但本地同场 External-V2 event stream 已完成 exact GUID/window 连接：55 个 wave × 3 人形成 165 个 episode，保留 4,076 个 server-observed START、12,457 个 GO、864 个 FAIL 和 17,397 个严格前缀 transition。其中 3,980 个 START 映射到已知可控动作，作为**服务器时刻的策略标签代理**；96 个 unmapped START（包含自动效果）只保留为观察，不进入 policy label。Chronicle 没有暴露客户端按键、next-swing queue 设置/替换/取消或 target-switch 意图，这些不会从 START/GO/FAIL 倒推。该产物是具名 Arms 历史观察层，不是 Fury executable expert，也没有 matched build/team 反事实，不能直接参加同天赋胜负投票。

post-fix Fury cohort 的动作连接也已完成：936 条冻结 ranking observations 中，101 条 null encounter ID 显式保留为未解析，其余生成 835 个 exact-GUID/instance/encounter/window episodes、24 个流式分区。产物保留 88,320 个 server START，其中 81,974 个是 Fury-v3 可控 START 代理标签。补上“可观时间段内部空洞”判定后，195 个 encounter group 中 152 个完整、43 个部分覆盖；group 层共有 3 个 interior gaps、合计 16,082 ms、最大 9,425 ms，展开到玩家 episode 后为 27 个 gaps、144,738 ms，对应 646 个完整和 189 个部分覆盖 episode。该 episode manifest SHA 为 `26459c74d682d2a61d4713b41324df323e9fce33f17a843482890f5e3ad4e6b7`。GO/FAIL 仍只是结果，null encounter、queue intent、target-switch intent 均不猜测。

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

P0 新增了保守的 `wowsims_mechanics_coverage_registry_v1` 与五段式 `build_request_composer_v1`。请求必须显式提供 CharacterProfile、RaidContext、EncounterModel、ExecutionModel 和 Objective；第二个角色不能继承意踟躇模板中的 buff、consumes、rotation 或 Warrior options。目录存在、效果已实现、Turtle 已标定、可参与比较是四层不同状态；未知 item/proc/enchant/talent 不会被静默当作被动属性。衬衣和战袍仍保留为数据质量证据，但不再错误阻塞 simulator runtime。

本轮把 O2O simulator 改动封装为相对 upstream `64cfa6ae...` 的 69-path 累积补丁（68 个 Go 路径 + `proto/warrior.proto`），patch SHA 为 `356da5740ae8da563a27556f993f836e3d19e82f0020df002ddb73458b33687d`。静态解析不再把 shapeshift、目标生物类型等条件属性折成 Warrior 全局属性；runtime 已精确实现 item 55113 的恶魔目标 +45 AP、item 61194 的每 5 秒回复 5 生命，以及 enchant 93、362、3016、1068、1267、1257。55127、55131 的 proc 概率/触发语义和 55116 的额外伤害公式仍无充分依据，继续标为 unsupported，不用猜测常数填洞。

`DPSSim` 目前没有足够可信的 Warrior 技能模型，只作为架构和其他职业参考，不承担本项目 Warrior 主结果。

### 3.5 Cat、Contra、Contra_new 与 Cat2_new

- Cat、部署版 Contra 和 Contra260817（即 `Contra_new`）均有身份清单、状态映射、有序 sink 执行和 full-policy rollout 适配。
- Contra 没有因为 `UnitHealthMax`、百分比或 `UnitClassification` 等字段问题被丢弃；可由离线重建修复的输入继续补强。
- 当前适配器仍是对 Lua 源码分支的翻译，不等同于原插件在真实客户端里的 exact runtime、acceptance 和 result 语义。
- 当前游戏实际发现的顶层 `D:\WOW\Interface\AddOns\Contra\Contra.toc` 加载的是可读 `Contra_ALL.lua`；同目录 `Contra.lua` 并未加载。`BrainOfCat\Contra\Contra.toc` 才加载单行压缩/混淆但仍是明文 Lua 的 `Contra.lua`，它不是当前部署入口，因此无需“解密”才能建立当前 deployed baseline。嵌套两文件的 whole-file 等价仍未证明，但不应再被写成当前部署版不可比的原因。
- Cat2_new 已有能力 manifest、动作计划、候选执行器和反馈循环。它是最终动作执行端和候选生成器，不被当作第三个“专家投票器”。

本轮还发现一个旧 runtime snapshot 的来源错误：`4c70ae78...` 中 Cat/Contra SavedVariables 的文件哈希实际对应角色“畏了部落”，而固定 Warrior build 来自“意踟躇”；旧生成器把真实角色目录折叠成同一个 `%WOW_CHARACTER_SAVEDVARIABLES%`，因此没有证明同角色配置。生成器现强制 Cat/Contra 来自同一角色目录，读取精确 CustomData 源记录，并用该记录重新投影完整种族、职业、装备、天赋和 Ravager rank；任何一项与固定 build 不同都会拒绝。当前“意踟躇”的正式 snapshot 为 `e52f0714...`，runtime binding 为 `951b8faa...`，由新的 Cat-gap v2 执行计划强制绑定。旧结果的同角色 provenance 必须降级；后续只接受该新 snapshot/context，且仍不把配置闭合作为 Lua client runtime parity 证明。

### 3.6 搜索与评估协议

已经建立：

- 早期 beam/前缀搜索；
- 参数化 Fury 策略与 13 维 Cat-gap 搜索空间；
- 同 seed 配对评估；
- 256 development seeds、256 selection seeds、1000 final-confirmation seeds 的冻结协议；
- 单目标、多目标、总体及各 baseline 分层统计；
- HPC worker、reducer、dispatch 和结果门禁。

需要特别说明：协议中“准备了 1000 final seeds”不等于已经执行正式 1000-seed final confirmation。正式 final 只允许在候选与协议封存后，收集与当前 50 raid/组件不相交的 **恰好 50 个完整新 UTK raids**，并覆盖至少 20 个新的 guild/player leakage components；当前 84 个描述性 corpus 和 68 个训练 cohort 不能回填充当这批 final corpus。

当前 `fury_multiseed_protocol_v3.json` 仍是 `DRAFT_BLOCKED`，`scientific_runs_started=false`；256/256/1000 是预注册的开发、选择与最终预算，不是已经发生的策略成绩。当前 Cat、两版 Contra 和 Chronicle historical baseline 也都尚未获得该正式协议的 `comparison_eligible=true`。

`fair_baseline_gate_v1` 现在只做 declaration preflight：它检查 Cat、部署 Contra、Contra_new、历史高手和候选五条 lane 是否声明了同一角色、团队、encounter、execution、objective 与 seeds，但**不读取或接纳任何 receipt**，因此在 v1 中永远返回 `REFUSE_COMPARISON`，不会把调用方布尔值或非空路径误当证据。真正打开各 producer artifact、验证完整 controller/runtime parity 与 `scenario × seed × lane` 闭包的 evidence gate 将另建 v2。当前旧 v3 协议仍绑定 `4c70ae...`，而正确角色 snapshot 已是 `e52f07...`，所以保持 `STALE_OR_MISMATCHED_CHARACTER_CONTEXT`；旧协议不被原地篡改。

旧 `fury_baseline_readiness_gate_v3` 也继续复现它当时的 protocol v2、`4c70ae...` snapshot 和专家清单字节；其默认输入已指向独立 frozen copy，而不是把当前 `fury_experts_v1.json` 强行改回旧状态或悄悄更新旧 gate 的固定哈希。当前 runtime-bound 工作读取 living expert manifest，两条证据链不再互相污染。

当前 `feedback_loop` 在每个决策点只调用固定 policy 的 `decide`，没有 reward/update/learn；screen/horizon reducer 也只做排序和检验，不写回参数。已经准备的 64-candidate Cat-gap 空间包含 64 个不同的 13 维向量，但重型 successive-halving 尚未执行，原 v1 还只允许后续阶段保留初始 64 的子集、不会生成新邻点。这正是“评价很多、策略没继续变强”的直接工程原因。

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

另一个仅 1 family × 1 seed × 3 health × 3 policies = 9 rollouts 的 singleton-health smoke 中，candidate-Cat 加权 DPS 差为 −382.585196，TTK 胜/平/负为 1/0/2；smoke 失败，规划中的 144-rollout 扩展并未启动。这是独立于 absolute-seed 回放的目标生命语义诊断。

### 5.4 最近策略搜索与 3 × 256-seed Horizon-v2 确认

早一轮 8-arm、每 arm 256 matched-pair 的 non-voting screening 中，相对 Cat 的前四名为 `bt_hamstring_cat_timing` +16.120054、`bt_wait_cat_timing` +9.647119、`ww_hamstring_cat_timing` +3.624806、`bt_hamstring_disabled` +0.593353 DPS。这一阶段没有执行统计检验，不参与投票；它说明 8 个固定参数在 development seeds 上能筛出表面上高于 Cat 的候选，不等于搜索器学习了 2,048 个新策略。8-arm screening 为 8,192 条 lane rollouts，其中 candidate 2,048 条；后续两轮 Horizon 各 3,072 条 lane rollouts，但复用的仍是这 8 个参数向量中的 3 个。

当前最新完整结果是修复评分 horizon 右截断后的 fresh-seed 确认。每个 arm 使用 seeds 513–768 的 256 个 matched groups，并运行 candidate、Cat、Contra260817 和 deployed-Contra 四条 lane，即每 arm 1,024 rollouts，3 个 arm 共 3,072 rollouts。Cat 与 Contra260817 的可用均值分别为 415.077664 和 358.187963 DPS：

| Candidate arm | Candidate 均值 | 相对 Cat | 相对 Contra260817 | 选择 |
|---|---:|---:|---:|---|
| `ww_hamstring_cat_timing` | 399.566249 | −15.511415（−3.737%）；112/0/144 W/T/L；Holm `p=0.02146`，显著更差 | +41.378286（+11.552%）；170/0/86；Holm `p=1.32e-7` | 不选择 |
| `ww_wait_cat_timing` | 412.167491 | −2.910173（−0.701%）；115/6/135；Holm `p=1.0`，不显著 | +53.979528（+15.070%）；174/0/82；Holm `p=8.65e-12` | 不选择 |
| `bt_hamstring_cat_timing` | 410.489574 | −4.588090（−1.105%）；121/0/135；Holm `p=1.0`，不显著 | +52.301611（+14.602%）；171/0/85；Holm `p=1.17e-11` | 不选择 |

结论：在该 20.001 秒单场景、同一 simulator request/装备/天赋和 matched seeds 的合成假设下，三个候选都显著高于 Contra260817，但没有任何候选高于 Cat。按冻结 maximin 排名，`ww_wait_cat_timing` 是三者中最好的，但仍比 Cat 低 0.701%，因此结果必须是 `NO_SELECTION`，不能把它当成新的可部署策略。该 run 的 `scientific_result_available=false`、`deployment_allowed=false`。

第四条 deployed Contra lane 每个 arm 都 256/256 incomplete，但原始 rollout 已经执行并造成伤害。抽查样本在 5.16 秒前产生约 501.58 diagnostic DPS，随后被审计器终止；直接原因是它把“已处于狂暴姿态时的幂等 no-op”“冷却中 `QueueSpellByName` 的正常拒绝”和“HS 队列 10ms 延迟激活、立即 aura 尚不可见”当成 comparison-fatal，最后又拒绝模拟下一次宏输入前的 100ms 等待。这是 ordered executor 的分类/推进缺口，不是 Lua 加密，也不等于候选击败了部署版 Contra。

### 5.5 离线高手 baseline

历史 Fury 玩家 policy v4 使用 182,481 个 strict-controllable labels 做动作预测。低基数 backoff 将 contextual log-loss 从 2.420177 降到 2.407410、ECE 从 0.051123 降到 0.025927，但 top-1 accuracy 从 0.318828 降到 0.246174，top-3 从 0.577583 降到 0.528488；联合门禁失败，已保留为负结果。

v5 正温度校准实际上已经执行，旧文档“尚未执行”已过期。它将 log-loss 改善到 2.326210，top-1/top-3 保持 0.318828/0.577583，但 ECE 恶化到 0.077512；联合门禁仍失败，状态为 `RETAIN_NEGATIVE_NO_NEXT_HPC`，没有授权 runner 或部署。

更重要的是，当前 182,481 标签是 pooled clean Fury，不是按托尼牛、桃姬儿或高 DPS 分位冻结的“高手 cohort”；现有指标只是“能否预测历史玩家下一个动作”，不是同装备 DPS。旧 historical runner 只执行 GCD，遇到 stance、Bloodrage、HS/Cleave queue 会直接拒绝；旧模型也没有学习相邻动作时延/右删失，所以不能正确模拟真人等待节奏。

本轮已经把这个工程缺口补成可执行的 pooled baseline：`historical_behavior_clone_v1.py` 从 Stage5 构造 semi-Markov delay + 15-action mark + action-conditioned target 模型，先做 legal mask 再采样；`historical_behavior_clone_simulator_adapter_v1.py` 将 9 个 GCD、3 个姿态、Bloodrage 和 HS/Cleave queue 映射到 typed sinks；`historical_behavior_clone_full_rollout_v1.py` 和 `fury_multiseed_worker_registry_v4.py` 将它接入同一个 dynamic-v5 request。真实 Windows bridge 的 1-seed 五 lane smoke 中，Cat、部署 Contra、Contra260817、Cat2_new 和 pooled clone 均完成目标死亡且 5/5 runtime receipt 为 `COMPLETE_BOUND`。该 fixture 只有 200 HP，DPS 只证明接线，不能用于胜负。

因此离线高手不是原则上不可比。污染排除后的 cohort、五个行为原型和 clone 已经冻结；当前剩余的是 exact source build 的 simulator 覆盖、source-bound request 和 matched full-controller receipts。把 clone 放到标准装备上仍只能称为受控的行为移植，不能误写成原玩家真实换装后的反事实 DPS。

`historical_fury_expert_cohort_v2` 从 6,629 条 exact DPS rows 中按 `started_at`、污染标签、Fury 与 DPS role 冻结 936 条 post-fix encounter observations，覆盖 111 个 exact-GUID 玩家、121 个 player-raid membership、24 个 raids 和 15 个 guild strata；899 条有同场 Fury peer。只有 10 人有重复 raid，101 人只有一次 raid，因此不把单次极值伪装成稳定玩家。

在上述 exact episode 之上，已构造 5 个分开的行为原型：4 个“每次重复 raid 都高于同场 Fury 中位数”的单玩家原型，以及 1 个 27 人、按玩家等权的局部 DPS ratio Q4 汇总原型。全部源数据为 835 episodes / 862 waves / 81,974 个可控 START；进入原型成员并按 GUID/raid 去重后有 24,379 个 START，因 5 个原型成员有交叉，最终输出 29,534 个 weighted START records。时延学习保留 29,476 个完整区间、58 个左截断首动作、282 个可用右删失尾段和 3 个不可用的左+右截断尾段。排名只用于冻结成员，不作为样本权重；五原型 manifest SHA 为 `e0d141b557cd67b7aa9acdf176007822285ff93ba56bfc253e70b8e8c21e9a1d`。

`historical_behavior_clone_v2` 已将这 5 个原型编译为 5 个 content-addressed model：START-only mark、action-conditioned target 和含右删失的离散 product-limit delay 头；clone manifest SHA 为 `fa9b66157c71e38598313510438a96c8c90d01de2c1c310ff1683aa36adbcde8`。一次审计找到并修复了区间权重错误：Q4 末桶后 residual survival 由错误的 0.3299416 回到 0.001548654（22,485 个完整间隔、215 个可用 censor）。精确分数只用于离线审计，运行时投影是有界 finite float，residual 不得转成伪动作或伪有限等待。这些仍是明确假设下的历史行为重建模型，不是原玩家的可唯一恢复客户端策略。

这 5 个原型共涉及 28 个 unique GUID；当前 5 个 Bloodthirst development requests 来自 5 个不同 GUID，其中只有 1 个 GUID 与原型成员相交，但全部 81,974 个历史动作相对这 5 个请求的 exact-build route 仍为 0。因此四个请求是 cross-player/cross-build、相交的一人也仍是 cross-build；都不能称为历史高手在自身同构筑下的复现。

`historical_fury_decision_build_join_v1` 已把全部 81,974 个可控 START 逐个接到同一 server/realm/GUID/instance 下、动作时刻之前最近的 CombatantInfo segment：81,974/81,974 joined，missing=0，共 979 个 segment；独立重放中的 identity mismatch、future-causal violation 和 run mismatch 均为 0，并保留了 260 次 wave 内 build 变化。全源当前有 103 个 runtime-executable segments、覆盖 9,556 个动作；相对当前五个 Bloodthirst requests 的最佳路由为 exact 0、同天赋向量且同武器模式 7,043、不同已知 build 的 transplant 72,957、语义不足 1,974。prototype 成员去重 union 为 24,379 个动作、266 个 segment，其中 runtime/development eligible 均为 9 个 segment、608 个动作。portable manifest SHA 为 `254c962b2fbfde224e39a30f4b26c5feac8fe902f786df102c7879f26deaec33`；identity 不含主机绝对路径，跨 Windows/Linux 重定位测试通过。支持域已有实质增长，但当前五个代表请求仍无 exact route，所以仍没有同构筑 DPS 对局。

`historical_fury_prototype_build_gap_priority_v1` 已把这 266 个 exact causal segments 的阻塞项按 prototype-union 动作覆盖量排序，而不是继续随意挑装备。当前 257 个 segment / 23,771 个动作仍被 43 个 runtime mechanism blockers 阻塞：9 个 item definition、20 个 item effect、5 个 enchant definition、6 个 enchant effect和 3 个 talent effect；另有上述 9 个 segment / 608 个动作已可进入 development。最大剩余项为 item 55127（14,518 START / 165 segments / 3 prototypes）、55131（13,494 / 157 / 3）、`warrior.improvedCharge`（8,743 / 90 / 2）与 item 55116（4,133 / 47 / 1）。comparison calibration 的 157 个缺口单独保留，未混入 development blocker；每个 prototype 仍只从真实 source segments 选择候选，没有拼造不存在的全套装备。该报告 SHA 为 `13c49c8eec863921ca772f4f1d4accfe689af96543de15c6cfaf9edb4255d7dc`。

`historical_behavior_clone_simulator_adapter_v2` 和 `historical_behavior_clone_full_rollout_v2` 已把五个模型接到现有 15-action typed sinks 与 dynamic-v5 开发 rollout。运行时只读 bounded-float projection，mark/target 零平滑，source 中零支持的动作不会凭空生成；residual survival 为 `WAIT_TO_BOUNDARY / NO_DECISION`；HS/Cleave 显式标为 server-START-timed queue proxy。五个 prototype ID、policy ID 和 model SHA 均 5/5 唯一。持久化校验现要求外部 exact model binding，并将 `damage_done`、顶层 damage/DPS、dynamic candidate-damage receipts 与 terminal lifecycle damage 互相闭合；负伤害、布尔值冒充零计数、丢失已产生 damage receipt 的 ACTION epoch、跨 adapter pending decision 和重寻址篡改均会被拒绝。只有 result-bearing ACTION 能由 bridge receipt 独立闭合；WAIT、Battle Shout、Death Wish 等无外部 result receipt 的 epoch 及其计数只能声明 content-addressed artifact 内部自洽，不能进入后续执行数量门禁。

实际 Windows Go 子进程 bridge v8 与 v11 都已各跑通 Q4 的一个 seed=4 smoke；在 receipt-bound damage 修复后又用 v11 重跑，结果仍为 `COMPLETE_BOUND`、`ALL_TARGETS_DEAD`、200 damage / 1,025 ms、1 decision（diagnostic 195.121951 DPS），且顶层 damage、candidate receipt 求和与 terminal lifecycle 三者均为 200。强制 Battle Shout 与 Death Wish 的 v11 native smoke 也均完成目标死亡、200 damage，且这两个 state-only GCD 不再错误携带 result attempt ID。请求是完整 Warrior 但仍属 synthetic Dynamic-v3 fixture，因此这些只证明 adapter/receipt 接线可执行，不是 source-bound raid 或 DPS 比较。强制 Sunder 的 native smoke 则正确暴露了另一项未闭合语义：candidate 内生破甲改变 target armor 后，外部 absolute effective-armor schedule 的 terminal equality 检查失败；在把外部队友减甲与候选自身破甲拆开前，含 Sunder lane 不得进入比较。先前 v11 + Python-only `request_v4` 在 attackability 初始化阶段 nil-player panic 的根因也已定位：该测试 fixture 缺 class/race/warrior，Go 跳过 ClassUnknown 后又直接取第一个 player；这不是 v11 回归。full-rollout v2 现在会在启动子进程前拒绝此类非可执行 request；第三方 Go 仍应将 panic 改为清晰 error，但完整 source-bound composer 本身已强制 Warrior 字段，因此不被这个 fixture 问题阻塞。

`historical_fury_source_bound_prototype_bundle_v1` 已从这 9 个可运行的真实 source segments 生成 9 个 exact-source requests，绑定 608 个历史动作支持和 32 个 seeds，共 288 个请求单元；current-character transplant 数为 0。bundle SHA 为 `21f5bd7194567245c582c3dc5792e4512ec33d5f1ee56e814b82c929dd26ddaf`，状态严格为 `PREPARED / NOT_RUN`，training、comparison、deployment 和 superiority 均未授权。

`historical_fury_source_bound_dynamic_config_v1` 又把这 9 个 request 逐一连接到实际产生决策的 exact instance/encounter/wave；9/9 映射唯一，其中 1 条的 catalog `valid_from` encounter 与真正 decision wave 不同，证明不能拿 build 生效边界代替决策波次。当前 0/9 为 `READY`、9/9 为 `BLOCKED`：duration-mode base request 没有声明 `useHealth` 且 `stats[34]=0`，前缀证据也未给出可唯一恢复的初始 HP、基础/外生护甲、可攻击区间和已经校准的团队 kill-clock。observed damage/最终死亡没有被冒充初始 HP，候选自身 Sunder 和武器 proc 也没有混入外生护甲。准备产物 SHA 为 `d15a999c351ef4f4cffaf6420e19ac2642ed1d166f10dfda880059eb9022ae0c`。

`historical_fury_source_bound_prototype_smoke_runner_v1` 只接受上述内容寻址准备层中的 `READY` 派生 request/config 对，并只能从 bundle 声明的 seed 列表中替换 request/load 的两个 seed 字段。对真实 0-READY 产物的集成检查在解析 bridge 或 simulator 路径之前正确拒绝；因此本轮没有用 200 HP fixture 或其他 caller hypothesis 伪造 source-bound smoke，也没有 native/HPC run。

### 5.6 本轮将评价接回改进

- `fury_cat_gap_candidate_update_v1.py` 不再把 reducer 结果停在排名表：完整 stage reduction 会保留 elite，并生成从未评估过的单轴邻域 child；64→16、16→4 都是一半 incumbent + 一半新 child，最终 4→2 才冻结已评估 top-2。
- `fury_cat_gap_stage_transition_v2.py`、`fury_cat_gap_hpc_worker_v2.py` 和 `fury_cat_gap_hpc_reducer_v2.py` 已形成可执行闭环；32/64/256 个阶段 seeds 在结果出现前预声明，彼此以及旧 seeds 不重叠。Windows 实桥的 1 candidate × 1 seed smoke 已通过；远端重型 stage 尚未启动。
- deployed Contra 新执行链已精确处理“姿态已满足”、冷却中 queue no-op、10 ms 延迟 queue activation、以及源码 `Contra_ALL.lua:31672/31677` 的低怒 BT/Slam 宏重试。group `008d4c...` 实桥 smoke 已跑到 19.901 秒 horizon：9,015.792 damage、453.032 diagnostic DPS、0 execution-fatal、0 unclassified rejection；这是单 seed 接线诊断，不是对局结论。
- 合并回归 163/163 通过，新增代码 `compileall` 通过；旧 frozen executor/worker 未修改。

### 5.7 当前科学结论

当前胜负可以压缩成一张表：

| Baseline | 当前最强直接证据 | 结论 |
|---|---|---|
| Cat | fresh 256 matched seeds 下最优 maximin arm 仍为 −2.910 DPS（−0.701%，不显著） | **没有击败 Cat** |
| Contra260817 / `Contra_new` | 三个 arm 均为 +41.378 至 +53.980 DPS（+11.552% 至 +15.070%），Holm 显著 | **只在该受限合成诊断中胜出** |
| 当前部署版 Contra (`Contra_ALL.lua`) | 新 executor 的单 seed 实桥已完整到 horizon；旧 256 lanes 仍是旧语义下的 incomplete | **用新 producer 重跑 matched seeds 后再比较** |
| 离线行为/高手 | exact Fury episodes、5 个高表现原型与 censor-aware clone v2 已完成；9 个 exact-source segments / 608 个动作已生成 288-cell build bundle，但真实 dynamic config 为 0/9 READY | **build 可构造，source-bound 环境仍被证据门禁阻塞，科学胜负尚未产生** |

因此，算法已经找到“对 Contra_new 有明显改善、对 Cat 非常接近”的参数化策略，但没找到通过双 baseline 门禁的策略。当前正确产物是 `NO_SELECTION`，不是把 `ww_wait_cat_timing` 强行当作新 brain。即使合成 lanes 使用同一 simulator request/装备/天赋和 matched seeds，也没有真实客户端 exact proc/runtime fidelity 或历史玩家同装备对照。

“同装备”只能在各自实验内部理解：旧 duration/literal 回放使用旧 proc-free clean-dual profile，最新 Horizon-v2 使用当前削骨之刃 loadout，不能把两批绝对 DPS 横向相减；而历史高手目前没有任何同装备 DPS lane。

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
- 已学习天赋的 tooltip，以及完整三树（含 rank 0）的 tab/index/name/tier/column/rank/maxRank；
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
- “gear 非空”不等于每个装备槽都完整；现在已完成槽位、玩家、build segment 与因果 INFO 边界级审计，但 simulator admission 仍会被未翻译天赋和未覆盖装备机制阻塞。

当前在线角色快照只能补当前角色，不能回填历史玩家缺失数据。

### 8.4 当前 HistoricalBuildCatalog 与代表构筑门禁

正式目录中的 Warrior 数据为 358 名玩家、19,014 个因果 build segments、5,431 个装备签名、207 个原始天赋签名；武器模式为 14,062 个双手、3,934 个双持、128 个单手无副手、890 个未知。14,440/19,014（75.9440%）在“真实 end-game 装备形态”层面合格。

Chronicle 天赋字符串的位置顺序是 Turtle 客户端 `GetTalentInfo(tab,index)` 顺序，不是 wowsims protobuf 字段顺序。最新一次游戏内 `/reload` 已写出“意踟躇”完整位置表；`turtle_talent_position_map_v1` 按预期 GUID/build 过滤共享 JSONL，并选中 source seq `24282`。P0 管线据此接纳 client build `7272` 的 18/17/19 共 54 个位置：40 个 wowsims talent field、1 个 Ravager option field、13 个 Turtle-only unsupported 位置，不做静默裁剪，receipt 为 `P0_STATIC_ADMISSION_READY`。

重建后，Warrior 19,014 个 segment 中 18,896 个为 `TRANSLATED_EXACT`、118 个仍缺 talents；1,015 个 runtime-executable，715 个 development-eligible，comparison-eligible 仍为 0。这里的 715 只是开发准入，不代表 Turtle calibration、baseline parity 或胜负准入。

`historical_representative_build_selector_v1` 现为 `READY`：715 个合格 segments 覆盖 56 名玩家和 201 个 exact builds，经玩家等权的确定性 k-medoids 选出 64 个 representatives；不再是旧的 5 人/11-build 支持域。所有 representative 仍为 development-only，comparison-eligible 为 0。

代表角色转换不再只信任 selector 里嵌入的对象：loader 会沿 selector → catalog manifest → gzip catalog 精确行重新验证同一 segment，并保留装备槽、enchant、suffix、talent 和 Ravager 语义。当前 ranks 2/7/9/11/14 的 5 个 Bloodthirst build segments 来自 5 名不同玩家，均能生成五段式 development request；32 个确定性 seeds × 5 个代表 × 5 个请求 lane = 800 个 requested cells。bundle SHA 为 `ccc91c5706ec9061987ce7dbc69d488c458205f82556152569bdf1893dcb8b65`，状态严格为 `PREPARED / NOT_RUN`，完成 receipt 仍为 0，不是实验结果。

这 800 cells 目前不会发往 node001–006：它们是当前代表构筑上的五 lane 请求，而历史高手相对它们仍为 exact route 0。另一方面，prototype union 自身已有 9 个 exact-source runtime/development segments、608 个动作，并已生成不含 transplant 的 288-cell source-bound bundle；它同样仍是 `PREPARED / NOT_RUN`。两套 bundle 都没有 comparison authorization，不能用扩展 seeds 代替机制、runner 和公平回执闭合。

## 9. 当前主要问题

1. 本次队友响应模型只完成预测头；9 个 exact decision waves 已绑定，但 source-bound dynamic preparation 为 0/9 READY，初始 HP、基础/外生护甲、可攻击区间和团队 kill-clock 尚未由前缀证据闭合。
2. Cat/Contra/Contra_new 仍缺 Lua VM 级 exact runtime/full-policy fidelity；Contra 源码可读问题已解除，deployed Contra 的已知 no-op、输入重试与 delayed queue activation 阻塞已修，但 Raid-B/部分 helper 和 matched-seed complete receipt 尚未闭合。
3. 最新 Cat-focused 多 seed 测试没有击败 Cat。
4. post-fix exact Fury cohort 已完成 GUID/instance/encounter/window 动作连接，81,974/81,974 动作均接到当时 build，并产生 5 个分开的 censor-aware 高表现行为原型/模型；typed adapter/full-rollout 已通过 synthetic native-bridge smoke。prototype union 的 266 个 segments 中已有 9 个 / 608 个动作可作 exact-source development，其余 257 个 / 23,771 个动作仍被机制 gap 阻塞；当前五个 Bloodthirst requests 虽来自五人且与 prototype 人群有一名玩家重合，但 exact-build route 仍为 0，所以尚无同构筑多 seed 胜负结果。三名用户指定的南北 Warrior 已完成 clean exact-GUID Arms episode 化，只能作为跨天赋 raid/action 参考，不能冒充 Fury baseline。
5. 场景 HP、护甲、分堆、站位和不可攻击期仍含假设。
6. 13 维 Cat-gap 的 64 候选 successive-halving 重型搜索尚未开始；v2 已能用上一阶段结果生成新邻点，但策略表达能力仍局限于这 13 个手写轴。
7. 全副本跨波规划、消耗品、合法换武器、天赋与装备外层搜索尚未实现。
8. 候选未蒸馏成可部署 Cat2_new live policy，也未经过真实 raid 配对验证。
9. 正式 final-1000 还缺封存后的恰好 50 个完整新 UTK raids，且需覆盖至少 20 个新的 guild/player leakage components。
10. 装备/天赋目录已升级到角色、槽位、segment 和因果时点，build 7272 完整天赋位置语义表已经接纳；条件属性错误折叠已修正，item 55113、61194 和已识别的六个 enchant 已实现。prototype union 仍有 43 个 runtime mechanisms 未闭合，首要是 55127、55131、`warrior.improvedCharge` 和 55116；物品方面，55127/55131 缺 proc 概率/触发语义，55116 缺伤害公式，不能以臆测实现进入公平 request。
11. dynamic-v5 的 absolute effective-armor schedule 当前包含总减甲，而候选 Sunder 也会在 simulator 内生减甲；未完成来源拆分时会双计数。含 Sunder 的历史 lane 必须阻塞，不能通过放宽 terminal check 绕过。
12. 正式五 lane 公平比较为 0/5 admitted；旧 v3 protocol 的角色 snapshot 已过期，必须另建新版本而非改写冻结协议。

## 10. 下一阶段建议顺序

1. 游戏内静态捕获 seq `24282`、build 7272 admitted map、registry、HistoricalBuildCatalog 和 64 个 k-medoids representatives 均已完成；当前阶段无需再次 `/reload`。
2. exact Fury episode 的 interior-gap 修正、决策时点 CombatantInfo 前缀连接、4 个稳定重复玩家原型、1 个玩家等权 Q4 原型、censor-aware clone v2 以及 v8/v11 typed dynamic-v5 smoke 已完成。illegal action、server-START-timed queue proxy 与 residual wait 继续显式记录；不从 GO 倒推 queue，也不把 101 条 null encounter 猜到最近波次。
3. 建立新的、绑定 `e52f07...` 当前角色和五段式 request 的开发协议；旧 v3 保持冻结。用修复后的 deployed-Contra producer 补完 Raid-A matched-seed baseline，旧 256 条 incomplete 不当作成绩。
4. 冻结并预注册 exact source-bound dynamic rollout kill-clock 的选择与准入规则，再将队友模型接入动态波次，用留一 Warrior 的真实结束时间验证。
5. 9 个 exact-source requests 与 runner/runtime 的 fail-closed 接线已完成；下一步要以显式假设层拟合并验证初始 HP、source-owned/exogenous 护甲、可攻击区间和团队 kill-clock，再把 `useHealth=true`、正且相等的 request `stats[34]`/config health 作为一个新的内容绑定对发布。288-cell 和 800-cell bundle 当前都只表示 `PREPARED / NOT_RUN`；similar/transplant 只作开发诊断，`fair_baseline_gate_v1` 仅保留 declaration preflight。
6. 只有 exact-source smoke、Cat/部署 Contra/Contra_new/historical prototype 的完整 matched receipts 和 evidence gate v2 都通过后，才启动 node001–006 的 build-conditioned residual policy adaptive stage。连续两个阶段无改善时扩展状态条件/动作分支，不用更多重复 seeds 掩盖表达能力不足。
7. 从两波连续 SMDP 开始加入跨波 CD、物品、旅行和合法换武器，再扩展到完整 raid；全本结果按总有效伤害/总声明时长计算，不平均 per-wave DPS。
8. 通过者蒸馏为默认零搜索的 Cat2_new Lua 策略包，Windows 客户端先 Shadow，再做受控真实验证；本地 companion 仅作可选导入/小规模精修。
9. 候选与协议封存后再收集恰好 50 个完整新 UTK raids、至少 20 个新泄漏组件，运行正式 1000-seed confirmation；多 build 更广声明另立协议。

## 11. 可复现性与发布状态

- 本报告随 `o2o-dps` 自有源码、配置、测试和脚本提交。
- Chronicle 大数据、HPC 运行产物、第三方工程和机器本地配置不进入 Git。
- 正式训练通过 source closure 与 dispatch SHA 绑定；worker、reducer、时间和资源日志留在服务器 run 目录。
- 冻结基线与优化 replay 使用各自独立的 source closure；二者通过同一 dispatch、68 份 worker receipts 和 full-core replay-equivalence contract 关联。
- 既有 Windows 完整集成回归快照仍为：Go simulator `with_db`、Python 1,812 tests（2 skipped）、BrainOfCat TOC 与已安装 Cat2 合同均成功；Python 段耗时 1,013.688 秒。它是此前快照，不冒充本轮 simulator delta 的复核。
- 本轮 O2O simulator delta 已封装为 69-path 累积补丁；patch SHA `356da5740ae8da563a27556f993f836e3d19e82f0020df002ddb73458b33687d`。在干净 upstream checkout 上 `git apply --check`、69/69 source path 重建比对、数据库生成及 `go test -tags with_db ./cmd/o2obridge ./sim/o2o ./sim/warrior ./sim/common/item_effects ./tools/database` 均通过；同一组 5 个 Go package 也已在当前工作树复跑通过。补丁不包含生成数据库、二进制或第三方完整源码树。
- episode、prototype、clone、join、gap、五 Bloodthirst request、exact-source request 与 9-row dynamic preparation 均已按最新输入重建；join、gap 与两类 request bundle 的内容身份不含主机绝对路径。受影响 Python 联合套件 140/140 通过，其中 dynamic/runner 联合门禁 15/15 通过；真实产物在 bridge 启动前因 0 READY 正确拒绝。没有启动本地重型仿真、node001–006 训练、superiority run 或 final-1000。
- 本报告所述自有源码、配置和测试随当前 Git HEAD 发布；第三方源码、离线原始数据和本机/服务器运行产物不进入仓库。
- 最新 `/reload` 只完成 source seq `24282` / build `7272` 的 P0 静态接纳；Shadow/静态检查仍不授权实机部署。当前没有任何新候选接管 Cat2_new，也没有“击败 Cat”的新证据。
- `cat_fury_full_policy_readiness_v4.py` 中的 Cat SavedVariables 默认路径是冻结历史字节的有意例外；为了便携性修改它会改变既有 source identity。新调用者应通过 CLI/config 显式传入 Cat root/SavedVariables 路径，而不是更新历史 pin。

## 12. 2026-09-13：exact-source 环境证据闭合 v1

新增 `historical_fury_source_bound_environment_evidence_v1.py`，把 9 个 exact-source Fury segment 同时绑定到 expert episode、External-V2 reconstruction 与 team timeline。它逐分区流式核验逻辑 SHA、字节数和记录数，只解码身份命中的记录；本轮实际读取 4 个相关 timeline 分区并保留 9 个命中 wave（单分区最多 4 个），没有网络请求、仿真或 HPC 任务。

真实重建结果：

| 指标 | 结果 |
|---|---:|
| exact-source requests / READY | 9 / 0 |
| 绑定的专家决策 | 608 |
| reconstruction voting targets | 548 |
| 已观察死亡 / 右删失目标 | 489 / 59 |
| 单一 generic target 与历史 registry 不符 | 9/9 |
| 20.001 秒诊断窗覆盖全部绑定决策 | 3/9 |
| 最后绑定决策前已见多个目标 | 8/9 |
| 窗内 exact-player LOO damage events | 39,866 |
| 事件时 nonvoting、事后才归入目标的目标 | 57 |
| 保留的目标 classification transitions | 1,103 |
| 位于 materialized union 窗外的减甲 START/GO 候选 | 662 |

独立复核后已修正完整 EventMeta 边界：旧的纯毫秒比较曾把最后决策同毫秒但顺序更晚的 5 条 exact-player 伤害计入窗口，共 8,439 damage；新版排除了其中 4 条 LOO 和 1 条 focal 事件。减甲候选现在明确拆成执行 union 与 full-wave retrospective 两层；unattributed voting damage 仍不进入 exact-player runtime schedule，但会贡献目标 registry 证据。classification transition、event-time voting lifecycle 与 retrospective final-identity lifecycle 也已分开，不再用事后分类悄悄回填因果 lane。

分堆证据发布两种 registry scope 和 0/1.5/3/5 秒 gap sensitivity：full reconstruction wave 分别给出 event-time voting 与 final-identity retrospective 分支；last-bound-decision 前已见目标只使用真正的 strict-prefix lifecycle，存活目标在截止点右删失，不读取未来后缀。全部标为 hypothesis，不选择 simulator truth。死亡标记早于末次伤害时，区间终点取二者 EventMeta 顺序较晚者。same-entry leave-instance-out kill-budget prior 只使用“已死亡且观察治疗为 0”的其他实例样本；带治疗死亡不再混入 HP 先验。

因此状态保持 `EVIDENCE_AUDIT_COMPLETE_NOT_EXECUTABLE`：exact initial/max HP、基础/外生护甲、精确可攻击区间和会响应候选策略的 team kill-clock 仍未识别；`derived_request_template` 与 `dynamic_load_config` 均为空，simulator/HPC/training/comparison/deployment/superiority 均未授权。当前内容地址 manifest 为 `a5dbcff04f310897f949fd8b850f4b8fdf96a5089fea7598e94ddf3af2713ccf`，partition logical SHA 为 `eeb8a36a85ff9af7cb099e20c87954e5960292bf63ac050694ef46d9a4e589f4`；派生产物继续留在 ignored offline-data 目录。

相关 source-bound dynamic/prototype/runner、timeline、episode 与新证据层联合回归为 38/38 OK。下一步是在这个冻结证据层上预注册显式的执行窗、target-registry、HP prior、外生 armor 与 attackability 假设分支，发布 development-only v2 对；完成 counterfactual team kill-clock 验证前仍不启动六节点大规模搜索。本阶段不需要游戏内 `/reload`。
