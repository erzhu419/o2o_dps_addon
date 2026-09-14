# scheduleurm：退出码 0 的静默任务被误判失败并重复 retry

日期：2026-09-14
复现项目：BrainOfCat / factored-press-v1
结论：这是 scheduleurm 的终态判定错误，不是应用程序失败。

## 实际影响

一次 6 节点训练已经成功完成，但每个 task 都因为日志中没有 `DONE`、`Saved` 或 `complete` 字样而被标为 `failed`，随后自动重试到上限 3 次。

- 原任务：`t93778`–`t93783`
- retry 1：`t93785`–`t93790`
- retry 2：`t93791`–`t93796`
- retry 3：`t93797`–`t93802`
- 节点：`node001`–`node006`
- 所有任务记录的 `exit_code`：`0`
- 所有 task log：空
- 错误理由均为 `no success marker`；不同轮次分别显示短任务 `died after only ...` 或 `finished in ... (expected DONE/Saved/complete)`
- 最终错误分类：6 个错误的 `APP_BUG_CAP` pending escalation

应用侧结果并未失败：

- 训练 case：288/288 个存在且均为合法 JSON
- case 状态：288/288 为 `COMPLETE_MATRIX_CASE_NONVOTING`
- shard summary：6/6 为 `COMPLETE_BATCH_SHARD`
- 每个 shard：`assigned_item_count=48`、`failed_item_count=0`
- retry 能校验并跳过全部已有的 48 个原子结果，因此没有数据丢失；只是重复消耗了 CPU 和调度时间

`NONVOTING` 是该 development-only 训练阶段的预期科学状态，不代表程序错误。

## 最小复现

任何由 local backend 启动、正常静默退出的命令都可能复现：

```bash
python3 scheduler.py submit \
  --description "zero-exit silent repro" \
  --cmd "python3 -c 'from pathlib import Path; Path(\"/tmp/repro-ok\").write_text(\"ok\")'" \
  --cwd /tmp \
  --signature scheduleurm/repro/zero-exit-silent \
  --project ScheduleurmRepro \
  --vram 0 --ram-mb 128 --cpu 1 \
  --require-node local \
  --allow-no-ckpt --allow-no-resume
```

预期：launcher 的 token 匹配 exit-status sentinel 且 `exit_code=0`，任务为 `done`。
实际：如果日志为空或没有魔法单词，任务被启发式诊断为 crash，并进入自动 retry。

## 根因定位

### 1. local backend 已取得可信退出码，但只传播非零失败

文件：`skill/scheduler_backend/local_probe.py:242-263`

这里先校验 exit-status sentinel 的 token；校验通过后，`exit_code` 已是本次 launcher 的可信终态证据。但当前逻辑只有非零时才设置 `terminal_ok`：

```python
if exit_code != 0:
    results[task["id"]]["terminal_ok"] = False
```

因此 `exit_code == 0` 时，`terminal_ok` 保持 `None`。

同项目的 Windows task probe 已经采用正确的对称语义：
`skill/scheduler_windows/task_probe.py:19-35` 会写入
`terminal_ok = (exit_code == 0)`。这也说明 local Linux backend 当前行为并非统一的既定协议。
对应测试 `skill/tests/test_scheduler_windows_probe.py:158-175` 已明确断言
`rc=0 -> terminal_ok=True`。

### 2. lifecycle 随后退化为日志启发式判定

文件：`skill/scheduler_running/lifecycle.py:375-404`

`terminal_ok is True` 本来已有正常完成路径，而且仍会扫描已完成日志里的明确 crash pattern。由于上一步没有为零退出码设置 `True`，流程落入 marker-based diagnosis。

### 3. 空日志被直接判为 crash

文件：`skill/scheduler_failure/terminal_diagnosis.py:193-210`

即使 launcher 已记录 `exit_code=0`，短任务或小日志任务仍会因为没有 `DONE/Saved/complete` 而被标为 crash。

### 4. 假失败被重试三次并升级

文件：`skill/scheduler_config.py:150`、
`skill/scheduler_failure/crash_requeue.py:207-218`

`MAX_AUTO_RETRY=3`，于是每个已经完成的 shard 又被执行三次，即原始执行加三次
retry，共四次尝试，最后产生 `APP_BUG_CAP`。

### 5. 本任务的 summary 不能补救该误判

`skill/scheduler_result/discovery.py:24-30` 当前不识别 `--summary`；本任务的
`--output-dir` 指向 `cases/`，而 `summary.json` 是它的同级文件，因此目录结果发现逻辑
也不会找到 summary。

不要把“看到 summary 文件存在”作为本问题的直接成功判据。BrainOfCat 的 batch 会先写
summary，随后在 `failed_item_count > 0` 时以 1 退出；忽略退出码反而可能把真实失败判成
成功。正确修复仍是对 token 匹配的可信 exit sentinel 对称传播零/非零退出码。

## 建议的直接修复

在 token 已匹配的 local exit-status sentinel 分支中，让退出码决定 `terminal_ok`：

```python
results[task["id"]]["terminal_ok"] = exit_code == 0
```

即用这一行替换当前只处理 `exit_code != 0` 的条件分支。

理由：

1. sentinel token 已绑定本次 launch，不是仅靠 PID 或日志猜测。
2. POSIX/进程契约中，零退出码就是应用声明的成功终态。
3. `lifecycle.py` 的 `terminal_ok is True` 路径仍会扫描明确的 crash pattern，因此不会丢掉已有错误日志检测。
4. `DONE/Saved/complete` 可以作为补充证据，但不应覆盖可信的零退出码。
5. 不应要求每个普通 CLI 都输出 scheduleurm 专用魔法单词。

## 最小回归测试

在 `skill/tests/test_scheduler_local_probe.py:140-168` 已有的 token 匹配、rc=0 测试中增加：

```python
assert out["a"]["terminal_ok"] is True
```

现有测试已经覆盖非零退出码、token 不匹配、lifecycle 成功路径和 crash-pattern 否决路径；
保留并运行它们即可。另可做一次可选端到端 smoke：静默写一个小文件并以 0 退出，等待
多个 watch 周期后确认只有一个 task 且状态为 `done`。

诊断文案可顺手校正：local rc=0 修复后当前会生成
`LEGACY_EXTERNAL_LOCAL_EXIT_STATUS` 一类标签；可将 `lifecycle.py:375-403` 与相关分析文案
泛化为 backend/local exit-status。这个文案问题不应阻塞核心终态修复。

## 本次状态修复请求

截至 2026-09-14，本机的 scheduleurm 工作树中已经出现上述候选代码修改：
`local_probe.py` 会在 token 匹配后写入 `terminal_ok = (exit_code == 0)`，相应 local
probe、lifecycle 与 Windows probe 的最小回归共 41 项通过。但这仍是未正式归档的工作树
状态，不能替代 scheduleurm 项目侧的提交、端到端静默任务验收，以及下面 6 个历史假告警的
关闭。

代码修复并通过回归后，请通过 scheduleurm 的正式状态/告警修复入口处理本次假告警，不要直接手改 JSONL：

- 将 `t93797`–`t93802` 的 6 个 `APP_BUG_CAP` pending escalation 标记为已解决；
- 不再为这些 task 创建 retry；
- 不删除 BrainOfCat 远端结果；
- 不把三轮仅做 existing-artifact validation 的 retry 时长写入正常训练耗时历史；
- 如支持终态纠正，可将原始 6 个任务按 `exit_code=0` 和已存在的完整 summary 纠正为成功；否则保留历史但注明为 scheduler false negative。

## 验收标准

- 上述回归测试通过；
- 一个 token 匹配、零退出码、空日志的 local-backend task 只执行一次并进入 `done`；
- 非零退出码仍进入失败处理；
- 明确 crash pattern 仍能否决零退出码；
- 本次 6 个 pending escalation 被正式关闭，288 个 BrainOfCat case 保持不变。
