# Chronicle 批量导出助手（Windows）

这个助手解决五件事：从本地排行榜 PDF 或已知角色生成实例清单、跨记录按
instance 去重、通过公开 External API 解析官方 UUID、逐条打开正确的官方导出
页面，以及监视 `Downloads` 后自动校验、归档和导入 CSV。队列每成功一条就
保存，关闭窗口或按 `Ctrl+C` 后可以继续。

## 用排行榜 PDF 建立本项目队列

当前 `dps board` 中的 PDF 已经保留每条排行榜记录的 instance 超链接。首次在
PowerShell 中执行：

```powershell
cd D:\WOW\Interface\AddOns\BrainOfCat\o2o-dps
.\scripts\chronicle_export_windows.ps1 `
  -LeaderboardPdfDirectory '..\dps board' `
  -DiscoverOnly
```

脚本使用本机的 `pdftotext`、`pdftohtml` 和 `pdfinfo`，按页面几何位置关联角色
文字与链接，不根据文件名猜职业或天赋。它保留所给 PDF 中全部真实行；`All
Specs` 仍记为 `null`，不会伪装成某专精独立榜。当前 11 份 PDF 生成 463 条
排行榜记录和 50 个去重实例。

PDF 链接使用 slug，而官方 CSV 文件名使用 UUID。脚本会通过 Chronicle 的公开
External API 查询榜单角色并解析 UUID。若个别公开实例未被该 API 返回，队列会
明确标为 `unresolved_external_api`，收集时只接受打开该页面之后新生成的官方
UUID CSV；无需手工改文件名。

先处理一条验证浏览器流程：

```powershell
.\scripts\chronicle_export_windows.ps1 -Limit 1 -TimeoutMinutes 60
```

## 没有排行榜 PDF 时按角色发现

直接运行 `.\scripts\chronicle_export_windows.ps1` 且尚无队列时，会列出 Chronicle
当前认可的 server 和 realm，然后依次询问：

1. `Server`：直接回车时使用 `Capybara`；
2. `Realm`：照着脚本刚输出的名称填写；
3. 角色名、GUID 或十进制 game ID；多个角色用英文逗号分隔。

角色必须确实出现在 Chronicle 中。API 返回 404 通常表示 server、realm、
角色写错，或该角色没有 Chronicle 可见记录；本机 WoW 角色目录名不保证能查到。

也可以不走问答，直接运行：

```powershell
.\scripts\chronicle_export_windows.ps1 `
  -Server 'Capybara' `
  -Realm 'Eversong Wilds' `
  -Character 'YourWarrior'
```

同一 realm 的多个角色：

```powershell
.\scripts\chronicle_export_windows.ps1 `
  -Server 'Capybara' `
  -Realm 'Eversong Wilds' `
  -Character 'WarriorOne','WarriorTwo'
```

不同 realm 分两次运行发现命令即可；新结果会合并进现有队列，不会清空进度。

## 每个网页上需要做什么

脚本会打开下一条 instance。UUID 已解析时，它等待精确文件名
`all-activity-<instance-uuid>.csv`；未解析时只接受页面打开后新生成的官方 UUID
文件。页面会尽量直接落在全部 encounters 的 `All Activity` 面板。

完整训练数据不能直接使用页面默认设置。Chronicle 默认只启用 `Damage`、
`Healing`、`Slain`、`Resurrection` 四类 stream；每个 instance 还要点击启用
下面 13 个灰色或带删除线的 stream 图标：

```text
Resource, Extra Attack, Aura, Spell Go, Aura Cast, Spell Start,
Spell Fail, Classification, Combatant Info, Dispel, Interrupt,
Absorbed, Consume
```

然后：

1. 确认 source、target、ability 和 time range 没有残留筛选；
2. 点击 `Export CSV`；
3. 不要改下载文件名，也不必手工移动文件。

网站自己会翻完当前 instance 的事件分页并生成 CSV。助手检测到下载完成后会
调用现有严格 importer；只有 17 列格式和所有行都通过校验，才会把这一项记为
`imported` 并打开下一条。下载失败或 CSV 不完整时不会误记完成，也不会删除
`Downloads` 中的原文件。

CSV 格式本身无法证明页面当时启用了哪几类 stream。因此助手默认忽略启动前
已经在 `Downloads` 中的同名旧文件，只接收显示上述提示后新下载或发生变化的
文件，避免把以前按默认四类 stream 导出的合法但不完整 CSV 静默收入训练集。
如果你已确认某个旧文件确实启用了全部 17 类，可显式运行：

```powershell
.\scripts\chronicle_export_windows.ps1 -AcceptExisting
```

## 暂停、续跑和查看进度

按 `Ctrl+C` 停止。以后在同一目录直接运行同一脚本，会从第一个 `pending`
实例继续：

```powershell
.\scripts\chronicle_export_windows.ps1
```

只查看状态，不打开网页：

```powershell
.\scripts\chronicle_export_windows.ps1 -Status
```

只发现历史、先不进入下载循环：

```powershell
.\scripts\chronicle_export_windows.ps1 `
  -Realm 'Eversong Wilds' `
  -Character 'YourWarrior' `
  -DiscoverOnly
```

限制本次最多处理 10 个，或单条等待 15 分钟后退出：

```powershell
.\scripts\chronicle_export_windows.ps1 -Limit 10 -TimeoutMinutes 15
```

队列和数据都留在 BrainOfCat 项目中：

```text
offline_data/chronicle_raw/export_queue.json
offline_data/chronicle_raw/leaderboard_manifest.json
offline_data/chronicle_raw/<UTC-import-id>/<official CSV>
offline_data/normalized/<instance>__<UTC-import-id>.jsonl
```

确实无权查看的实例可显式跳过：

```powershell
py -B -m o2o_dps.chronicle_export_assistant skip '<instance-id>' `
  --reason 'not viewable'
```

## 串行助手和并行助手的边界

公开 External API 只能从“已知角色”反查其 raid history，没有全站 instance、
leaderboard 轨迹或跨 instance 事件导出端点。本项目由用户保存的排行榜 PDF
固定选样范围，再用 External API 解析 UUID；PDF 是选择索引，不是动作事件。

本文件描述的串行助手仍需上述每实例操作。项目另有
`scripts\chronicle_parallel_export_windows.ps1`：它以独立 Chrome profile 操作
官方页面，选择全部 encounters、验证 17 个 streams 和空过滤器、点击官方
Export，并在严格导入成功后原子更新队列。命令、接管已有导出和恢复方法见
`CHRONICLE_PARALLEL_EXPORT_zh-CN.md`。

并行助手同样不调用 browser-only 内部事件接口。若要把规模继续扩大到数百个
instance 或多机采集，应向 Chronicle 申请研究用途的 bulk dataset 或正式 API
授权；研究用途本身不替代网站条款可能要求的许可。官方联系入口：
<https://capy.chronicleclassic.com/contact>。
