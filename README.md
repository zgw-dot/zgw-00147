# Evidence CLI — 证据复核与快照恢复工具

一个基于 SQLite 的命令行证据复核工具，支持清单导入、预检、复核、撤销、快照保存/恢复、链路追踪和报告导出。

## 安装

```bash
pip install -e .
```

## 快速开始

```bash
# 1. 导入清单
evi import --batch mybatch --manifest ./manifest.csv --evidence-dir ./evidence

# 2. 预检文件完整性
evi precheck --batch mybatch

# 3. 查看批次列表
evi list

# 4. 交互式复核
evi resume --batch mybatch
```

## 快照与恢复

### 保存快照

```bash
evi snapshot save --batch mybatch --output ./mybatch.json
```

### 恢复快照（预演）

恢复前先预演，**此时就可以看到完整的对账摘要**，判断这份快照值不值得落下去：

```bash
evi snapshot restore ./mybatch.json --dry-run
```

预演输出中的**统一恢复摘要**包含：

| 字段 | 含义 |
|------|------|
| 来源快照 | 快照文件路径 + 是否存在（OK / MISSING） |
| 覆盖差异 | 强制覆盖时，旧批次与新批次的描述、item 数、复核统计对比 |
| 最后复核记录 | 快照内最后一条复核的时间、状态、备注、操作人 |
| 恢复后新增操作 | 恢复后发生的 review / undo 次数 |
| 对账状态 | `[对账OK]` 或 `[!对账]`（提示 item 数、复核统计、操作计数是否自洽） |
| 警告 | 来源快照缺失、清单文件丢失、强制覆盖待确认等 |

### 普通恢复

```bash
evi snapshot restore ./mybatch.json
```

若目标目录已存在同名批次，会报错并提示使用 `--force`。

### 强制覆盖恢复

```bash
evi snapshot restore ./mybatch.json --force
```

强制覆盖时，摘要的**覆盖差异**区会详细列出被覆盖批次的原状态与快照状态的对比。

### 目录重映射

快照里记录的证据目录路径可能已不存在，可在恢复时重新映射：

```bash
evi snapshot restore ./mybatch.json --evidence-dir /new/path/to/evidence
```

---

## 核对恢复结果（对账流程）

恢复完成后，可以用以下**四个独立入口**交叉核对，它们都从同一份持久化数据聚合，保证展示一致：

### 1. `list` — 批次列表概览

```bash
evi list
```

每个恢复过的批次会显示：

- 快照状态：`[OK]` 源快照文件仍存在，`[MISSING]` 已丢失
- 恢复后操作数：`+R3 U1` 表示 3 次 review + 1 次 undo
- 对账标记：`[!对账]` 提示数据不自洽（需要进一步检查）

示例输出：

```
  batch_no        items  预检  signed  supplement  pending  恢复
  mybatch         5      5/0/0 2       1           2        [OK]+R3 U1 [!对账]
```

### 2. `resume` — 恢复信息详情

```bash
evi resume --batch mybatch
```

进入交互前会先打印完整的**统一恢复摘要**：

```
═══ 恢复摘要 (对账OK) ═══
批次:       mybatch
来源快照:   [OK] /data/snap/mybatch.json (2025-01-15 10:30:00)
覆盖差异:   [强制覆盖] 旧 5 项 (已签 2) → 新 5 项 (已签 1)
最后复核:   2025-01-15 10:35:22  signed  "确认无误"  (operator: alice)
恢复后操作: 3 次  (review: 2,  undo: 1)
对账详情:
  ✓ item_count(5) == precheck.total(5) == review.total(5)
  ✓ signed(2) + supplement(1) + pending(2) == total(5)
  ✓ post_restore_ops.count(3) == actual_logs(3)
警告:
  ! 快照来源文件请妥善归档，避免意外删除后无法回溯
```

### 3. `snapshot trace` — 恢复链路追踪

```bash
evi snapshot trace --batch mybatch
```

除了链路事件详情，**头部同样会输出统一恢复摘要**。可看到：

- 每次 force restore 的父子关系链
- 每次恢复的快照文件是否还存在
- 每次恢复是否做了目录重映射
- 恢复后所有操作的明细日志列表

### 4. `export` — JSON 报告导出

```bash
evi export --batch mybatch --output ./report.json --format json
```

导出的 JSON 顶层包含 `recovery_summary` 字段，和 CLI 展示的是同一份数据：

```json
{
  "recovery_summary": {
    "batch_no": "mybatch",
    "has_restore": true,
    "source_snapshot": {
      "path": "/data/snap/mybatch.json",
      "exists": true,
      "created_at": 1736917800.0
    },
    "overwrite_diff": { ... },
    "last_review_log": { ... },
    "post_restore_ops": {
      "count": 3,
      "review_count": 2,
      "undo_count": 1
    },
    "review_stats": { "total": 5, "signed": 2, "supplement": 1, "pending": 2 },
    "precheck_stats": { "total": 5, "passed": 5, "failed": 0, "unchecked": 0 },
    "reconciled": true,
    "reconciliation_details": { ... },
    "warnings": []
  },
  "restore_trace": { ... },
  "items": [ ... ]
}
```

### 对账自检清单

每次恢复后建议做以下检查：

1. **来源快照是否仍存在** — 如果 `[MISSING]`，尽快归档或重新导出
2. **`reconciled` 是否为 true** — false 说明 item 数、复核统计、操作计数之间有矛盾
3. **`post_restore_ops.count` 是否符合预期** — 不该有的操作说明有人在恢复后动了数据
4. **`overwrite_diff`（强制覆盖场景）** — 确认被覆盖的旧批次数据确实是打算丢弃的
5. **跨 CLI 重启一致性** — 退出再进入，执行 `list` / `trace` / `resume`，摘要数据应完全相同（全部从 SQLite 持久化表动态聚合，不靠内存状态）

---

## 其他命令

```bash
evi undo --batch mybatch              # 撤销最后一条复核
evi export --batch mybatch -o out.csv # 导出 CSV 报告
evi --help                            # 查看所有命令
```
