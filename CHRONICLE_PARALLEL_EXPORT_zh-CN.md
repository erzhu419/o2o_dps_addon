# Chronicle 官方 UI 并行导出设计

串行收集器仍保留原来的命令和行为。并行路径使用
`o2o_dps.chronicle_parallel_export`，把一个队列实例交给一个 worker；每个
worker 拥有独立的 Chrome profile 和下载目录，因此同名 CSV、浏览器状态和
下载进度不会在 worker 之间串线。

## 队列状态

并行 worker 只执行下面的原子状态转换：

```text
pending -> claimed -> imported
                   -> pending   （未达到重试上限）
                   -> failed    （达到重试上限）
```

claim 写入 `claim_worker`、随机 `claim_token`、`claimed_at` 和
`claim_expires_at`。worker 等待 Chronicle 生成 CSV 时定期续租；进程异常退出
后，过期 claim 会自动回到 `pending`，最后一次失败则进入 `failed`。短时
`export_queue.json.lock` 保护所有并发 read-modify-write，导入提交还会核对
worker 和 token，防止旧 worker 覆盖新 claim。

查看并行状态：

```powershell
cd D:\WOW\Interface\AddOns\BrainOfCat\o2o-dps
py -B -m o2o_dps.chronicle_parallel_export status
```

显式重新尝试已经达到上限的条目：

```powershell
py -B -m o2o_dps.chronicle_parallel_export retry-failed
```

## 官方 UI worker

并行调度不调用 Chronicle 的 browser-only event API。`scripts/chronicle_ui_export.mjs`
通过本机 Chrome DevTools Protocol 操作官方实例页，并依次完成：

1. 点击 `button[title='Select all encounters']`；
2. 启用所有 event streams；完成时 17 个 stream 按钮都必须处于
   `aria-label='Disable ... stream (...)'` 状态；
3. 点击
   `button[title='Export all filtered activity pages to CSV']`；
4. 等待页面的 `Exporting n / total` 结束以及该 worker 下载目录中的 CSV
   完成写入；
5. 使用既有严格 importer 验证并生成 normalized JSONL，成功后才提交
   `claimed -> imported`。

如果 encounters 侧栏处于隐藏状态，worker 会先点击 `Show encounters`，再执行
`Select all encounters`。三个 source/ability/target 输入框必须存在且为空；任何
断言失败都会停止该实例的本次尝试，不会点击 Export。下载完成后仍须通过既有
CSV importer，才会把队列状态提交为 `imported`。

启动三路完整导出：

```powershell
.\scripts\chronicle_parallel_export_windows.ps1 `
  -Concurrency 3 `
  -TimeoutMinutes 360
```

接管一个已经在浏览器中运行、尚未下载完成的导出，并同时启动另外两路：

```powershell
.\scripts\chronicle_parallel_export_windows.ps1 `
  -Concurrency 3 `
  -TimeoutMinutes 360 `
  -AdoptInstance '0295db29-69e1-4a10-bc4a-486c200c0c3b' `
  -AdoptDownloads "$HOME\Downloads"
```

## 并发和运行边界

- `-Concurrency` 控制本次新启动的 worker 数，`-WorkerStart` 指定 worker 编号
  起点，避免扩容时与现有 profile 冲突。本机先验证 4 路，随后按用户要求扩容
  到 worker-01～10；单路早期约占 1.2 GB，导出末尾还要同时构造完整 CSV
  string 和 Blob，因此仍需保留内存余量。

例如已有 worker-01～04 时，再启动 worker-05～10：

```powershell
.\scripts\chronicle_parallel_export_windows.ps1 `
  -Concurrency 6 `
  -WorkerStart 5 `
  -TimeoutMinutes 360
```
- 每个 worker 的 profile、downloads 和 log 位于
  `offline_data/chronicle_raw/parallel_export/worker-NN`，不会使用当前正在处理
  第一条实例的浏览器 profile。
- 不要同时运行旧串行 collector 和并行 collector；旧 collector 不参加并行
  claim 协议，可能用旧队列快照覆盖并发 claim。
- `--timeout-minutes` 是单实例等待时间，`--max-retries` 是该实例允许的总尝试
  次数。失败原因和时间保存在队列条目中。

## 验证

```powershell
cd D:\WOW\Interface\AddOns\BrainOfCat\o2o-dps
py -B -m unittest tests.test_chronicle_parallel_export -v
py -B -m unittest discover -s tests -v
```

并发测试验证 12 个线程不会重复 claim，同样覆盖续租所有权、超时重试、达到
上限后的 `failed`、显式重置、接管已有导出、UI 子进程到严格 importer 的完整
worker 流程，以及带 receipt 的原子导入提交。官方 UI 选择器可用只读命令检查：

```powershell
node .\scripts\chronicle_ui_export.mjs --inspect-cdp-port <port>
```
