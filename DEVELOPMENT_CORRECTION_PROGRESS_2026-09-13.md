# 实施纠偏进度（2026-09-13）

本轮落实 `implementation_correction_zh-CN.md` 的开发路径。所有新胜负数值均来自**模型定义**的 Upper Kara 仿真，不是指定历史窗口的精确重放，也不是游戏内真实优越性。旧严格历史门禁保持不变；未发布 Cat2 策略包，未运行正式 final-1000。

## 已执行的闭环

| 交付 | 实际结果 |
|---|---|
| A 独立开发波次 | `development_wave_case_v1` 构造受控战士 build、初始怒气/目标/团队背景和全目标死亡终止；watchdog 单列 censored。HP 来自观察到死亡前的伤害预算代理，不冒充 NPC 精确最大生命。 |
| B Cat 差距 | `cat_residual_candidate_rollout_v1` 的零残差与同一 Cat controller、执行器同构；旧 20.001 秒域有首个语义分歧与单决策分支诊断，但隐藏 RNG 快照未证实，故不称因果收益已识别。 |
| C 整波比较 | 原始一个模型波完成 32-seed 四方比较；另从 1197 波 capsule 中固定 3 个单目标波和 1 个双目标波，完成按直接 GUID 排除历史焦点战士伤害、队友速率延续至目标死亡的 v3 32-seed 配对。双目标只支持 Cat/13D 两方，不能写成四方。 |
| D 候选→评价→更新 | 13D 联合调整 HS 阈值×取消阈值，5 个参数向量、32-seed、160 个完整四方 panel。最佳向量比自身 incumbent 平均多 **50.74 有效伤害**，但比 Cat 少 **324.90**，比部署版 Contra 少 **446.73**；仅更新开发 incumbent，不授权采用。 |
| E teacher→蒸馏→复测 | 可观测状态 branch teacher 用 6 个训练标签蒸馏单一怒气 guard；仅 1 个标签支持残差，2 个留出种子结果为 −740.84、−1422.27。另据动作诊断构造尾窗 HS guard，以新 run 的 32 个未参与设计 seed 四方确认，相对 Cat −187.84 有效伤害（SE 133.28），拒绝采用；多状态/多动作 DAgger 尚未完成。 |
| F 多构筑与两波 | 两个受控构筑的连续两波状态保留 Bloodrage CD；远端 8-seed 配对中，13D 相对 Cat 的整段 DPS 分别为 −37.53、−39.00。尚未覆盖共享物品 CD、波间换装和真实历史 build 等价。 |
| G 实战诊断 | 修正 Shadow 诊断把 `25286` 误认成猛击的问题：它是英勇打击。现有客户端日志对齐嗜血 3、旋风斩 2、英勇打击 3 次 GO 与结果；猛击 0 次 GO。这些不是网络 RTT 或真人候选收益。独立真实 A/B 尚待游戏内验证。 |

## 32-seed 分层结果

每行的候选和所有列出的基线共享 seed、场景及受支持的原生执行模型；数字是候选减基线的平均**有效伤害**，括号内为配对标准误。4/4 固定波次都达到 32/32 完整结算。

| 模型波次 | 比 Cat | 比 Contra_new | 比部署 Contra | 比较域 |
|---|---:|---:|---:|---|
| 单目标短波，Cat 残差10 | −0.63 (0.63) | +236.22 (266.31) | +210.25 (234.90) | 四方 |
| 单目标中波，Cat 残差10 | −225.00 (108.92) | +380.16 (226.03) | +442.95 (230.50) | 四方 |
| 单目标长波，Cat 残差10 | −197.99 (172.34) | +2205.38 (570.56) | +1273.11 (552.10) | 四方 |
| 双目标波，13D | −320.16 (247.67) | 未运行 | 未运行 | Cat/13D 两方 |

原波的 32-seed Cat 残差搜索也未找到可信正收益：折扣 5 怒气相对 Cat 为 +16.69 有效伤害、SE 46.89，近似 95% 下界为 −78.97；保留零残差 Cat 回退。以上是**不同模型波次/候选**，不把行间数值合并成全副本 DPS。

独立新种子的 8 个环境敏感性分支共 256/256 四方完成；残差10 相对 Cat 的均值在全部分支均为负（−32.14 至 −276.35 有效伤害）。对旧波中改变结果的 14 个残差15 seed 做双策略动作诊断：10 负、4 正；5 个负例少一次斩杀，关联负向损失的 78.5%。这是优先研究 HS 排队/猛击/斩杀尾窗的线索，不是单次动作因果效应。

由此提出的可观测尾窗 guard 在独立 `20261009..20261040` 32-seed 四方结果中，24 个 seed 有干预、19 个 seed 有抑制；相对 Cat 为 −187.84（SE 133.28），相对 Contra_new +575.41（SE 295.21），相对部署 Contra +81.94（SE 297.53）有效伤害。三方正收益下界门禁未通过，策略不入选、不部署；这些确认 seed 不再拿来调同一 guard。

## 来源、运行与限制

- 本地来源 capsule：`offline_data/derived/fury_offline_scenario_capsules/v2/fury_offline_scenario_capsules_v2.23029ac5328e8c5e9e3009010e5d2876415d69b0e3d39e23e0ba4bf66a48e63e.json.gz`。覆盖诊断为 50 raid、1197 distinct waves、3269 target rows；目前只有原波加 4 个新波构造了可执行模型，不能把其余 1192 波标成 executable。
- 目标 HP、基础护甲、可攻击性和外生团队速度仍是模型假设。分层 v3 从本地原始 CSV 固定规则选择历史战士、逐目标扣除其直接 GUID 伤害；这避免了已识别部分的重复计算，但宠物/未归属伤害未识别，受控 live build 也不等同该历史战士。历史死亡锚点后的队友速率是外推假设，不是观测。旧 v1/v2 分层结果保留为失效模型诊断，不参与 v3 汇总。
- 部署 Contra 的开发重入适配器保留原有拒绝动作 sink，以 runner 侧 100ms 重试代理继续模拟；这不是历史玩家真实按键节奏，也不能替代游戏内等构筑对照。
- 远端 `node001` 的 attempt `attempt-20260913-003` 保留原生 panel 与小结果文件：`results/anchor_13d-full32.json`、`results/cat_residual-full32.json`、`results/stratified-20260913-32-direct-guid-loo-extrapolated-v3.json`、`results/uncertainty-full32.json`、`results/diagnostics/residual-d15-action-gap-20260913-v1/`、`results/two-wave-build-seeds-20261001-20261002-20261003-20261004-20261005-20261006-20261007-20261008.json`。独立 attempt `attempt-20260913-004` 保留 `results/cat-terminal-guard-fresh32.json` 与逐 seed panel。本地未拉原始 CSV、panel 或 checkpoint；新增上传仅源代码与 1,950,480-byte 派生 capsule。
- 下一步应扩大场景/构筑假设与可观测状态 teacher 的有效结构，并优先核实末端猛击/斩杀的模拟—真实执行差异；新的结构需预先固定、再用全新配对种子确认。当前不进入 Cat2 实战 A/B。真实优势还须专门游戏内对照，不能由 Shadow 或仿真自动授权。

## 续做记录（同日）

- 固定的来源分层扩成 12 波（6 个单目标、6 个 2–16 目标），均为直接 GUID 焦点伤害排除后的模型波。`node001/attempt-20260913-005` 在 `2026091401..1432` 上完成 11 波的全部配对；5 目标波 Cat 因约 60 万团队伤害累计的 1.3×10⁻⁹ 浮点舍入而被旧回执误判。保持冻结 v2 不动、修正 v5 极小相对容差后，独立 `attempt-20260913-006` 只补这波，32/32 两路完成。两个版本的结果没有伪装成单一同代码全 12 波门禁。
- 同受控 build 下，现有残差10 在 6 个单目标波相对 Cat 的均值依次为 −6、−7、−127、+55、−174、−322 有效伤害；13D 在 6 个多目标波依次为 −331、−922、−1611、−518、−1964、−747。每波 32 seed；唯一正均值 +55 的标准误为 88，不能采用。多目标部署 Contra Raid-B 仍不支持，未按 0 分计。
- Cat 访问状态的 ActionPlan teacher 新增 queue 增删、WW/BT 替代、GCD 延后及完整 Cat continuation。`attempt-20260913-005` 双目标旧口径 32 seed 共 199 次完整分支重跑，回执复查只有 146 次动作获原生接受；首个 WW→BT 机会 14 胜/18 负，均值 −117、SE 325。当前 v2 只把接受且全波完成的分支计入标签；`attempt-20260913-006` 短单目标 32 seed 为 250 次分支、195 个有效标签，首个加 HS 机会均值 +25、SE 113。均未形成可采用控制器，策略更新轮次仍为 0。
- 历史 rank7 双持装备/天赋确实改变仿真初始状态，但现成部署 Contra lane 是 Raid-A：该四路数字仅是 Raid-A 在双持 build 上的行为移植，不能作源忠实四方胜负；因此取消了原拟 32-seed 远端任务。另找到 rank9 双手、已学嗜血的构筑，在同一模型波/seed 四路均完成（Cat 7063、Contra_new 6521、部署 Contra Raid-A 5713、Cat 零残差 7063 有效伤害），但消耗品、宏配置及历史玩家策略并未复原。rank1 无嗜血构筑的 Contra Fury 首决策请求未学嗜血，门禁保留。
- 103 个有效训练 ActionPlan 标签落入 8 个可观测条件格，没有规则通过预设支持度/收益门槛；控制器弃权并精确回退 Cat，全新 seed 原生整波差值为 0。最接近的 WW→BT/中怒气格在训练 16 seed 平均 +318、诊断 13 seed 平均 −528，仅 4/13 为正；这些是单分支 teacher 标签，不是最终策略收益。双目标团队死亡后原生改投的响应事件也已完成严格 receipt 闭合，3 个本地 seed 两路皆合格、胜负一正一平一负，尚无稳定优势。
- 安装版 Contra 的 TOC 身份已核对；双持宏 `b` 已有独立 Raid-B 原生执行链和策略 ID。受控双持的双目标单 seed 中 Cat 4900、Raid-B 2426、候选 5838 有效伤害，三路完整；但当前 autoselect=false 且未模拟手动切靶，Raid-B 全程只打目标0。Contra_new 的原始顺序在 v15 模拟假设下可提交 GCD 后顺劈队列，但后续仅队列成功、无 GCD 的宏重入时序未定，第11决策失败、分数仍为 null；多目标四方没有闭合。v15 Go 增量补丁和测试已单独保存，不把模拟接受当 WoW 客户端事实。
- 固定双目标波另用原始 CSV 的 495 条伤害事件建立 actor 前缀：32 个玩家类、2 个物体、1 个生物 GUID 独立保留，技能 ID/时间/伤害不改，只有目标死亡后的改投是假设。node001 8-seed 探针中 5 对完整、3 对因事件前缀耗尽而截尾；完整对候选−Cat 均值 +98 有效伤害、3胜1平1负，但旧脚本未留逐 seed 值，SE/置信下界不可计算，不能采用。远端只传 119 KB 派生 JSON 和必要代码/二进制，不传原始 CSV。
- 现阶段仍没有超过 Cat 的同 build 稳定反馈策略，也没有历史高手完整策略基线、跨波全本优化或 Cat2 实战发布证据。下一步需核实客户端 post-GCD 队列与宏重入、补目标切换/多目标完整基线，再研究跨波 CD 与物品机制；新策略必须经独立 seed 的整波/连续波门禁确认。

## 下一阶段实验（同日）

- 已安装开发用游戏内 Contra_new 排队探针，复用原“BoC标定”按键，记录同键旋风斩→顺劈、顺劈排队后再次按键的 typed client/GO 事件和按键间隔；另备专用 SavedVariables/战斗日志解码器。首次安装时 Lua 5.1 静态解析和解码测试通过，随后完成了游戏 `/reload` 与实测（结果见下）。
- 原生 `set_target` 可用，不必改 Go。只读当前 HP/存活/可攻击状态的开发组件在原双目标波新 32 seed 相对 Cat 为 +459.3 有效伤害（SE 162.3，21 胜/2 平/9 负）；另一来源短双目标波新 16 seed 为 +252.9（SE 266.6，10 胜/6 负），长双目标波原目标已是最高血量、16 次精确无动作。五目标波为 −1625.4（SE 468.4，3 胜/13 负）；其余 3/4/8/16 目标分层也没有跨场景稳定正收益。**不启用全局或双目标条件切靶规则**；它是独立的目标控制组件，非 Cat 自身的策略收益。
- 同原生实例连续两波保留怒气/CD、冻结“首波不使用血性狂暴”候选，node001 新 16 seed×2 构筑均完整结算：削骨之刃相对 Cat −170.7 有效伤害（SE 302.9），双持 −430.6（SE 292.1）。拒绝这条跨波候选，不把旧的单波重置当跨波证据。死亡之愿、共享物品 CD 及真正历史队友响应仍未闭合。
- 这些实验只从服务器读取小型终局摘要；未回传原始 CSV 或 checkpoint。Cat2 策略包仍不发布。

## 客户端排队实测裁定（同日 22:27–22:28）

- 已由本地角色存档的 Nampower typed 事件独立复核：A 同键旋风斩→顺劈调用间隔约 5 ms；旋风斩 client accepted、server GO，顺劈返回 `SPELL_FAILED_SELF=97`、client rejected、queue code 0，随后无顺劈 GO。故 v15 原生桥“同键 GCD 后可立即接受顺劈排队”的假设被此客户端试验反证，应撤销，不能拿该路径计算 Contra_new 分数。
- B 先单独顺劈 accepted；第 3 次尝试在主手剩余约 0.312 秒、排队后 1.772 秒重入，重入前 `IsCurrentAction=1`。旋风斩 accepted 后显示 `IsCurrentAction=false`，因此又请求顺劈，但该即时请求 rejected/code 0；原队列约 0.370 秒后 GO，并造成 793 伤害。GO 后 4 ms 的另一条 accepted 回执不可归给重入请求。修正解码器后总体状态为 `same_key_queue_rejected`，B 为 `prior_queue_survived_reentry_and_server_go`。
- 文本 `WoWCombatLog.txt` 在本次时段无技能行；GO/结果来自 typed 存档，而非文本日志。探针已补用现有 `LoggingCombat` 接口开启未来战斗日志，但本次不为此要求重测。探针只复现等价动作顺序，不是完整 Contra_new 宏或键频；多目标 baseline 仍待合法重入模型闭合。
- 已撤销现行 Contra_new runner 的 v15 同键强制接受入口，ordered executor 把 GCD 后顺劈记为拒绝；v15 补丁/旧二进制作为历史负结果保留，v16 增量补丁和 Go 回归覆盖先排顺劈→旋风斩→重发拒绝→原队列下一主手执行。本机原生单 seed `2026091401`：Cat 完成 4900、部署 Contra Raid-B 完成 2426、候选完成 5838 有效伤害，Contra_new `UNSUPPORTED`/分数空；这不是多 seed 胜负，更不是完整四方比较。

## 后续开发诊断：队列重入与按键时钟

- Contra_new 的顺劈成功排队不消耗决策；runner 现用明确标注的固定 100 ms 代理安排下一次宏调用，并记录每路仿真调用时刻。本机 seed `2026091401` 四路均终局：Cat 4900、Contra_new 5439、部署 Contra Raid-B 2426、候选 5838 有效伤害；`comparison_ready=false`。这是模型单 seed，不取代上面的旧版 `UNSUPPORTED` 记录。
- 修正前 v4.3 适配器的 node001 独立 `2026091801..1832` 开发批次，32/32 四路终局、0/32 通过公平比较门禁。候选相对 Cat 为 −233.84（SE 235.42，14 胜 18 负），相对 Contra_new +280.82（SE 186.86，21 胜 11 负），相对部署 Contra Raid-B +1500.70（SE 177.47，29 胜 3 负）有效伤害。四路平均仿真调用次数依次为 Cat 14.44、Contra_new 73.72、部署 Contra 105.00、候选 17.06；它们不是共同的物理按键机会，不能据此给四方真实胜负排序。逐 seed panel 只留远端 `node001/attempt-20260913-raidb-fourway-proxy-002/AddOns/BrainOfCat/o2o-dps/results/raid-b-fourway-proxy-2026091801-n32.json`，本地未拉原始数据或 checkpoint。
- 同键旋风斩之后的 `IsCurrentAction` 是动态值：等价动作客户端探针看到它改变。v4.4 已把适配器按源顺序改成动态顺劈 guard：旋风斩接受后会继续解释顺劈调用，拒绝时保留原队列、不提交新顺劈；40 项本机相关测试通过。上述远端批次仍是 v4.3，不用作 v4.4 的确认集。Go 已加入默认关闭的外生按键时钟核心及 bridge 命令，Go 两包回归通过；Python bridge 也可配置/结束单次机会并校验回执。四路 runner 尚未接入，动态 idle/响应队友路径明确不支持此时钟；策略尚不部署到 Cat2。

## 外生按键时钟继续施工

- v18 Go 增量使动态 v3 波次和响应式队友事件可与固定按键网格并行：不可攻击窗口仍逐 tick 给策略机会，GCD ready／目标恢复／队友 wake 不额外造键；默认模式不变。Go `sim/o2o` 与 bridge 两包全回归通过，增量补丁存于 `simulator_patches/wowsims-turtle/o2o-v18/`。Windows 新桥 `bin/o2obridge.press-v18.exe` 独立构建，未覆盖旧版。
- 原生桥烟测确认动态 0/100/200/300ms 网格；500ms 同刻两条队友事件先结算，仍只有一次按键。Cat 和零残差 Cat 的独立非投票 pilot 在同 seed、100ms、1 秒静态单目标场景中均走 11 次完全相同的提案与 sink 回执；目标 HP 未知，结局严格标为 `DURATION_CENSORED_NONVOTING`，不是 Upper Kara 击杀或优势。
- 仍缺 Contra_new、部署 Contra Raid-B 和候选的共同按键 runner，以及四路相同场景的终局门禁；无可攻击目标时原生 `AvailableActions/Apply` 仍拦截自施法，必须先修该机制或把相关波次标为 unsupported。未运行新的四方 seed 搜索、未部署 Cat2；下一步先补其余 lane 的一次一键 source 调用，再以全新 seed 做可比开发 panel。

## 2026-09-14：机制路由与四路共同按键 smoke

- 新增从已声明 build/波次请求抽取机制特征的开发路由：武器模式/攻速、嗜血可用性、目标数、HP 假设与连续波拓扑；teacher 标签可在少数机制投影间共享，不构造逐装备×逐怪大表。Windows 本机对历史 rank7 双持/rank9 双手、单/双目标分别跑通 teacher→路由→新 seed 整波；每格仅 1–2 个标签，未更新策略，候选精确回退 Cat（差值 0）。
- Cat、Contra_new、部署 Contra Raid-B、条件 Cat 候选已分别接入 v18 动态整波 100ms 外生按键试点；source WAIT/队列重入不再造额外按键，终局键击杀目标后不再错误调用 `finish_press`。受控双持双目标 `2026091401..1404` 的 4 个本机 smoke seed 均四路完整、按键回执合格；首个 seed 的 Cat/Contra_new/部署 Contra/零规则候选分别为 6369/4665/4557/6369 模型有效伤害（四舍五入）。这只是同钟功能烟测，`comparison_ready=false`，不据此排名。
- 下一步把共享时钟用于可观察分支 teacher 和独立 seed 的多构筑/多波次更新；Contra_new 仍为源默认配置，部署 Contra 的手动切靶未复原，不能把当前试点当作真实同构筑基线或 Cat2 发布证据。

## 2026-09-14：分解搜索矩阵与 v19 原子按键时钟

- v19 新增动态波次＋按键时钟的原子加载：波首暂不可攻击时从 0 ms 保留物理按键，期间不调用策略；目标恢复后才调用。旧 `load_dynamic_v3` 行为不变。Go 两包、Python 整合回归和原生四路测试均通过。
- 第一批固定矩阵为 3 个精确历史构筑（rank 7 快双持、rank 11 慢双持、rank 9 双手）× 4 个来源分层波（q05/q60/q95/双目标）。同一 phase 的 sample seed 跨 12 格复用为 randomized block；training、transfer、fresh 使用互不重叠的 seed 空间。
- 每条分支只改变一个可观察状态下的 Cat `ActionPlan`，随后恢复完整 Cat continuation。训练只产生候选；8-seed 留出整波迁移才可授权 route；32-seed untouched fresh 只运行已授权规则，否则严格走 Cat/no-op 同一 artifact。execution contract 绑定按键周期、搜索预算、v19、输入路径及 router route/rule，旧 bridge／旧 router／不同参数结果不会被断点逻辑误用。
- 本机 1-block×12 格 smoke 的 training/transfer/fresh 均完成；因为只有 1 个 distinct seed，6/8 seed 门槛下 proposal=0、authorization=0，24 个 transfer/fresh case 都验证为 Cat 精确回退及合法语义/终局/时钟回执。这只证明管线与拒绝逻辑，不是“已超过 Cat”的结果。
- 紧凑远端闭包已放到共享 run `matrix-rb-20260914-v19-001`；未上传 Chronicle 原始 CSV 或 checkpoint。正式 training 共 288 case，已排为 `t93778..t93783` 六个 48-core 可恢复 shard。提交后均为 queued；当时 node001--node006 已认领 183--185/192 CPU，故没有强行抢占，等待 watcher 在资源释放后启动。transfer/fresh 尚未提交，必须先由 training reducer 冻结 proposal。

## 2026-09-14：首批矩阵裁定与 teacher 选点纠正

- `matrix-rb-20260914-v19-001` 的 288/288 个 training case 与 6/6 shard summary 实际完整，fit 也成功；但 46 个可观察条件格均未通过预设 `n>=6、正值比例>=0.75、95% 下界>0`，所以 proposal 为 0 route，未提交 transfer。这个结果保留为 `max_states=1` 的负结果，不降低门槛，也不把 Cat identity/no-op 当改进。
- 复查发现该批多目标 teacher 经常选到 Cat 已提交、但因 GCD 锁而被原生 bridge 拒绝的首个按键。现改为只采 baseline 对应 sink 已 `ACCEPTED`、候选精确 ActionRef 在当前 `available_actions` 中 `legal=true/ready_in_ms=0` 的状态；按当前目标 HP 分 EARLY/MIDDLE/LATE，在 `max_states=6` 下给三段固定额度，只发出此前未覆盖的 phase×branch kind。selection contract 已绑定进 matrix execution contract，因此旧产物可读但不会被新批次断点复用。
- Windows v19 原生 smoke：rank7 双目标取得 3 个物理状态、4 个接受分支（EARLY `BT_TO_WW/DEFER_GCD`，MIDDLE `SUPPRESS_QUEUE/DEFER_GCD`）；rank9 q60 取得 3 个状态、4 个接受分支，其中 EARLY `WW_TO_BT` 单 seed 相对 Cat 为 +1193.33 有效伤害。后者只是搜索信号，不是多 seed 胜负或可采用策略。后续相关 teacher/router/matrix/remote-planner 定向回归为 82/82 通过。
- 新闭包 `matrix-rb-20260914-v19-hpcover-ms6-001` 只含代码、派生 capsule/构筑、item DB 与 Linux v19 bridge。training 的 `t93826..t93831` 在 node001--006 并行完成：288/288 case、6/6 summary、0 failed，墙钟约 50--75 秒；共取得 1057 个物理状态和 1352 个原生接受且完成的单分支。scheduleurm 对更早一批静默 `exit_code=0` 任务误判并 retry 的问题已单列交接到 `SCHEDULEURM_ZERO_EXIT_FALSE_RETRY_2026-09-14.md`；本批用显式 DONE marker 与 run/phase 独立 signature 完成，未复用旧 retry 链。
- 首次 fit 产生 6 条 transfer proposal，但审查发现投影层把 `weapon_mode=TWO_HAND,target_count=one` 的规则路由到 4 条双持路线及一条双目标路线；这会让 `rule_active` 仅表示“配置了规则”，却没有可能形成接受的实际干预。旧 proposal 和 `t93842..t93847` 的 96-case 批次均保留作诊断：只有两手单目标路线形成 3 个完整比较，平均 −348.64，其余不是有效零收益。它们不进入授权。
- Router 现要求武器模式一致、BT 替换仅用于已学嗜血构筑，并按干预时存活目标数允许多目标路线降桶；若最具体投影不兼容，会继续寻找较低维的兼容投影。修正后复用同一 288 个 teacher case 重新 fit，只剩两手单目标和两手双目标 2 条结构可执行 proposal。authorization 也改为只统计完整、comparison-ready、动作接受且严格单干预的有限 delta；支持不足与统计失败分开报告，缺失不再记成 0 收益。
- 为避免复用已经查看过的 transfer seed，batch/remote plan 新增 `sample_start` cohort。全新 indices 8--39 按 32 case/route 预置预算，在 `t93854..t93859` 上以 6×64 worker 完成 384/384 case、0 failed，约 60 秒。单目标路线 32 个 assigned 中 22 个形成完整比较，候选−Cat 平均 −22.32，有 10/22 为正，95% 正态下界 −530.32，拒绝授权；双目标路线 32 个 seed 中没有命中该单目标尾段条件，标为 `INCOMPLETE_TRANSFER_EVIDENCE`。授权数为 0，故未提交 untouched fresh，也未发布 Cat2 策略。
- 全仓 2468 项广泛回归并未完成：运行至 61% 后因慢原生组停止，期间出现既有失败；首个独立复现为 `test_chronicle_external_team_background_generator_v2` 的写死 config digest 与当前 bridge/config 输出不一致。它不在本轮 teacher/contract 改动面内，但在宣称全仓 green 前仍需单独归档修正。

## 2026-09-14：完整稀疏基准与 32-seed transfer

- 完整稀疏基准运行 `matrix-rb-20260914-v19-sparse-basis-ms64-001` 已在 node001--006 完成：576/576 case、48 个 randomized-block seed、12 条精确 route，所有首个机会均可观测，`UNKNOWN=0`。原先严格要求每折正收益比例/Top-2 rank identity 的门禁被诊断为不适合作为稀疏策略采用条件，保留其负结果但不再使用。
- v5 expected-effect learner 在上述全量训练闭包上冻结：8233 个候选中形成 10 条 development shortlist，覆盖 5 条 exact route；reducer 用时 51.58 s，最大 RSS 546916 KB。Cat no-trigger 的零差值仍计入期望效果，正收益比例只作为诊断，不作为硬门禁。
- 对冻结 shortlist 做了真正不重叠的 32-seed transfer：`t93876..t93881` 分布在 node001--006，每节点 64 workers，384/384 case artifacts、6/6 compact summaries、0 failed/retry；scheduler 观测墙钟约 121.8 s。只在服务器完成紧凑分析，未拉取 CSV 或 checkpoint。
- transfer 结果为 0/10 条规则获得 authorization，因此没有进入 untouched fresh，也没有发布 Cat2。代表性配对结果（候选相对 Cat 的有效伤害）为：慢双持 `ADD_HS_QUEUE / HP=MIDDLE` 均值 +178.87、SE 96.22、LCB −9.73；快双持均值 +53.18、SE 29.87、LCB −5.36；双手双目标 `WW_TO_BT / swing=LATER` 均值 −51.21、SE 263.56、LCB −567.78。下界未过采用门禁，不能宣称优于 Cat。
- transfer 后出现的 observable split：`flurry=INACTIVE` 候选均值 +103.04、SE 45.64、LCB +13.58，仅是候选生成阶段的探索性信号；它没有经过冻结规则的 untouched actual-policy validation，不作因果结论、授权或部署依据。
- 当前结论是训练覆盖和 transfer 管线已闭合，但首批 32-seed transfer 尚未证明稳定收益。后续应基于预先冻结的候选/路由，在 untouched seed 上验证 split 信号，并继续保持 Cat 精确回退与独立 seed 门禁；不复用本次 transfer seed 调参。

## 2026-09-14：静态 refinement 的实际策略反证

- transfer 后只冻结了一条标准两谓词规则：慢速双持、单目标、模型血量 50k--200k 的 exact route；仅在 `HP=MIDDLE` 且 `flurry=INACTIVE` 时把 Cat 的当前动作改为 `ADD_HS_QUEUE`。该规则仍是 development-only、nonvoting，且没有写入 Cat2。
- 全新 265e9 seed 空间完成 64 个 exact-route 整波实际策略比较；其余 11 个矩阵格只保留显式 no-policy artifact。64/64 个 exact-route seed 完整、`UNKNOWN=0`，29 次实际触发、35 次精确回退 Cat。候选相对 Cat 的期望有效伤害差为 +0.30，SE 51.48，95% 正态下界 −100.61；未通过正下界门禁，因此拒绝采用。
- 这不是“多跑一些 seed 就会自动变好”的方差问题。探索投影对应的是在父规则第一次合法机会时立即裁决：若乱舞未激活则干预一次，若乱舞已激活则本波永久放弃；标准两谓词实现却会跳过当前 ACTIVE 机会，并可能在之后变为 INACTIVE 时重新触发。两者不是同一个策略，后者已被 265e9 实验反证。
- 下一项冻结假设因此改为可在线执行的 first-opportunity latch，并使用新的 266e9 seed 空间做 64-seed actual-policy 验证；它必须继续满足 Cat/no-op identity、当前 action legality、单次 accepted sink、按键时钟与终局语义门禁。即便通过，也只进入后续多 baseline/跨波验证，不直接发布 Cat2。

## 2026-09-14：first-opportunity latch 与追加开发证据

- first-opportunity latch 已按真实 v19 QueueDelay 时序闭合：立即 `Apply` 只证明提交时，必须在原主手挥击 deadline 前由后续按键的 before-state 观察到精确 `spell_id=25286, tag=1`；若其间 Cat 又独立接受一次英勇打击，则结果为 UNKNOWN，不能把后续动作冒认为首次干预。strict reducer 会重验动作、延迟确认、Cat/no-op、终局/时钟回执并重算差值。
- 冻结策略在全新 266e9 的正式 indices 2--65 完成 64/64 exact-route seed（矩阵 768/768、UNKNOWN=0）：21 次干预、43 次 ACTIVE 永久回退；候选相对 Cat 的模型有效伤害均值 +47.50、SE 46.81、95% 正态下界 −44.25。正式门禁未通过，保留为完整负裁定，不追认授权。
- 随后保持同一冻结策略，在未重叠 indices 66--257 追加 192-seed **development-only** 精度扩展（矩阵 2304/2304、UNKNOWN=0）：48 次干预、143 次 ACTIVE 回退、1 次无父机会；均值 +60.85、SE 28.96、下界 +4.09。六节点各 384 case/64 worker，scheduler 观测总墙钟约 73.25 秒；子进程实际 CPU/RAM 峰值未被 scheduler telemetry 捕获，不能把 `peak_ram_mb=0` 当真实占用。
- 严格联合学习器重新验证 256 seed×12 格和两份 summary 后，只观察到 `rage_band`、`swing_timing_band` 两个会变化的干预特征，共枚举 broad 加一/二谓词 10 个候选。broad 总体均值 +57.51、SE 24.63、下界 +9.24；`rage=LOW` 为 +62.94、SE 23.97、下界 +15.97，但二者均有一折留出均值为负，最终 0/10 通过预设四折稳定性筛选。该结果不选 post-hoc 子集；下一步只把语义从未改变的 broad latch 冻结为候选，在独立 267e9 seed 空间确认，不能把追加开发结果写成 confirmatory 通过。

## 2026-09-14：267e9 独立实际策略确认

- `subset-0000` 是 266e9 前已经固定的 broad latch，而不是从 10 个事后子集中择优。冻结 artifact 仍显式保留 `candidate_screen_passed=false`、四折诊断和 `candidate_authorized=false`；空谓词也使用新的 subset policy identity。原生 smoke 分别闭合 ACTIVE 精确 Cat 回退，以及提交后下一按键 before-state 的 tag-1 英勇打击延迟确认。
- 全新 267e9 `0..255` 由 `t93916..t93921` 在 node001--006 以 6×64 worker 并行完成：每 shard 512/512 case、0 failed/skip，合计 3072/3072 矩阵 case；scheduler 从最早启动到最后完成约 64.47 秒。server-only strict reducer 再验 256 个 exact-route seed，结果 256/256 完整、UNKNOWN=0、87 次干预、168 次 ACTIVE 回退、1 次无父机会。
- 独立确认中候选相对 Cat 的模型有效伤害均值为 **+47.00**、SE **25.53**、95% 正态下界 **−3.03**，因此 `SUBSET_ACTUAL_LOWER_BOUND_NOT_POSITIVE`，门禁失败。它不是“输给 Cat”的负均值，但仍不能声称稳定优于 Cat；不追加同一确认批次直到显著，不授权、不发布 Cat2，也不把该 exact-route Cat 比较外推为 Contra/Cat2 或 Upper Kara 全副本胜负。
- 下一轮若继续改策略，先把本批作为已揭示的开发诊断，定位 87 次干预的负尾部与可观测前缀差异，再冻结新结构并改用新的 seed namespace；不能在 267e9 上调完规则后仍称其为独立验证。
