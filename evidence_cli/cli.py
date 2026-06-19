"""离线证据包移交 CLI 主入口"""

import os
import sys
import click
import datetime

from . import db
from . import manifest as manifest_mod
from . import precheck as precheck_mod
from . import report as report_mod
from . import snapshot as snapshot_mod


STATUS_LABELS = {
    "pending": "待处理",
    "signed": "已签收",
    "supplement": "待补件",
}

PRECHECK_LABELS = {
    "unchecked": "未检查",
    "passed": "通过",
    "failed": "失败",
}


def format_time(ts: float) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def ensure_db(ctx):
    """确保数据库已初始化"""
    work_dir = ctx.obj["work_dir"]
    db_path = ctx.obj["db_path"]
    if not os.path.exists(db_path):
        db.init_db(db_path)
    return db_path


def get_batch_or_exit(db_path: str, batch_no: str):
    """获取批次，不存在则退出"""
    batch = db.get_batch_by_no(db_path, batch_no)
    if not batch:
        click.echo(f"错误: 批次 '{batch_no}' 不存在", err=True)
        sys.exit(1)
    return batch


@click.group()
@click.option("--work-dir", "-w", default=None, help="工作目录（默认当前目录）",
              type=click.Path(file_okay=False, dir_okay=True))
@click.pass_context
def main(ctx, work_dir):
    """离线证据包移交 CLI 工具

    用于证据包的清单导入、完整性预检、复核签收、撤销和报告导出。
    状态保存在本地 SQLite 数据库中，换进程后数据稳定。
    """
    if work_dir is None:
        work_dir = os.getcwd()
    work_dir = os.path.abspath(work_dir)
    db_path = db.get_db_path(work_dir)
    ctx.ensure_object(dict)
    ctx.obj["work_dir"] = work_dir
    ctx.obj["db_path"] = db_path


@main.command()
@click.pass_context
def init(ctx):
    """初始化数据库"""
    db_path = ctx.obj["db_path"]
    work_dir = ctx.obj["work_dir"]
    if os.path.exists(db_path):
        click.echo(f"数据库已存在: {db_path}")
    else:
        db.init_db(db_path)
        click.echo(f"数据库已初始化: {db_path}")
    click.echo(f"工作目录: {work_dir}")


@main.command("import")
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--manifest", "-m", "manifest_path", required=True,
              type=click.Path(exists=True, dir_okay=False), help="manifest 清单文件路径")
@click.option("--evidence-dir", "-e", "evidence_dir", required=True,
              type=click.Path(exists=True, file_okay=False), help="证据目录路径")
@click.option("--description", "-d", default="", help="批次描述")
@click.option("--force", "-f", is_flag=True, help="强制重新导入（替换旧批次）")
@click.pass_context
def import_cmd(ctx, batch_no, manifest_path, evidence_dir, description, force):
    """导入 manifest 清单"""
    db_path = ensure_db(ctx)

    manifest_path = os.path.abspath(manifest_path)
    evidence_dir = os.path.abspath(evidence_dir)

    click.echo(f"正在解析清单: {manifest_path}")

    try:
        result = manifest_mod.parse_manifest(manifest_path)
    except manifest_mod.ManifestParseError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except FileNotFoundError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    has_errors = len(result.errors) > 0 or len(result.duplicates) > 0

    if result.errors:
        click.echo("")
        click.echo(f"清单解析发现 {len(result.errors)} 个问题:")
        for err in result.errors:
            click.echo(f"  第{err.line_no}行: {err.message}")

    if result.duplicates:
        click.echo("")
        click.echo(f"发现 {len(result.duplicates)} 条重复路径:")
        for line_no, path in result.duplicates:
            click.echo(f"  第{line_no}行: {path}")

    batch_exists = db.batch_exists(db_path, batch_no)
    if batch_exists and not force:
        click.echo("")
        click.echo(f"错误: 批次 '{batch_no}' 已存在，使用 --force 强制重新导入", err=True)
        sys.exit(1)

    if has_errors:
        if batch_exists and force:
            total_bad = len(result.errors)
            click.echo("")
            click.echo(f"错误: 清单包含 {total_bad} 条问题记录，无法安全替换旧批次。", err=True)
            click.echo("请修复清单后再试，旧批次数据保持不变。", err=True)
            sys.exit(1)
        if not result.items:
            click.echo("\n没有可导入的有效条目，导入终止。", err=True)
            sys.exit(1)

    if batch_exists and force:
        old_batch = db.get_batch_by_no(db_path, batch_no)
        old_total, old_signed, old_supplement, old_pending = db.count_reviewed(
            db_path, old_batch["id"]
        )
        click.echo("")
        click.echo(f"将替换批次 '{batch_no}' (原有 {old_total} 项，"
                   f"已签收 {old_signed}，待补件 {old_supplement})")

    batch_id, count = db.replace_batch(
        db_path,
        batch_no=batch_no,
        manifest_path=manifest_path,
        evidence_dir=evidence_dir,
        items=result.items,
        description=description,
    )

    click.echo("")
    if batch_exists:
        click.echo(f"已替换批次 '{batch_no}'")
    else:
        click.echo(f"导入完成: 批次 '{batch_no}'")
    click.echo(f"  成功导入: {count} 条")
    if result.errors:
        click.echo(f"  问题条目: {len(result.errors)} 条（已跳过）")
    click.echo(f"  证据目录: {evidence_dir}")


@main.command()
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--item", "-i", "item_id", type=int, default=None, help="只检查指定证据项 ID")
@click.pass_context
def precheck(ctx, batch_no, item_id):
    """完整性预检（路径/大小/sha256，只读不修改证据文件）"""
    db_path = ensure_db(ctx)
    batch = get_batch_or_exit(db_path, batch_no)

    evidence_dir = batch["evidence_dir"]
    if not os.path.isdir(evidence_dir):
        click.echo(f"错误: 证据目录不存在: {evidence_dir}", err=True)
        sys.exit(1)

    if item_id:
        item = db.get_evidence_item_by_id(db_path, item_id)
        if not item or item["batch_id"] != batch["id"]:
            click.echo(f"错误: 证据项 {item_id} 不存在或不属于该批次", err=True)
            sys.exit(1)
        items = [item]
    else:
        items = db.get_evidence_items(db_path, batch["id"])

    if not items:
        click.echo("批次中没有证据项")
        return

    click.echo(f"批次 '{batch_no}' 开始预检（共 {len(items)} 项）")
    click.echo(f"证据目录: {evidence_dir}")
    click.echo("")

    issues = []
    passed = 0
    failed = 0

    with click.progressbar(items, label="预检中", show_eta=False) as bar:
        for item in bar:
            status, actual_size, actual_sha256, item_issues = precheck_mod.precheck_item(
                evidence_dir, item
            )
            db.update_precheck_result(db_path, item["id"], actual_size, actual_sha256, status)
            if status == "passed":
                passed += 1
            else:
                failed += 1
            issues.extend(item_issues)

    click.echo("")
    click.echo(f"预检完成: 通过 {passed} 项，失败 {failed} 项")

    if issues:
        click.echo("")
        click.echo(f"问题详情 ({len(issues)} 条):")
        for issue in issues:
            click.echo(f"  [{issue.issue_type}] 第{issue.manifest_line_no}行 "
                       f"{issue.file_path}")
            click.echo(f"    {issue.detail}")

    if failed > 0:
        click.echo("")
        click.echo("提示: 预检失败的项可在复核时标记为'待补件'")


@main.command()
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--item", "-i", "item_id", type=int, required=True, help="证据项 ID")
@click.option("--status", "-s", "status", type=click.Choice(["signed", "supplement"]),
              required=True, help="复核状态: signed=已签收, supplement=待补件")
@click.option("--remark", "-r", default="", help="复核备注")
@click.option("--operator", "-o", default="", help="操作人")
@click.pass_context
def review(ctx, batch_no, item_id, status, remark, operator):
    """复核签收：标记为已签收或待补件"""
    db_path = ensure_db(ctx)
    batch = get_batch_or_exit(db_path, batch_no)

    item = db.get_evidence_item_by_id(db_path, item_id)
    if not item or item["batch_id"] != batch["id"]:
        click.echo(f"错误: 证据项 {item_id} 不存在或不属于批次 '{batch_no}'", err=True)
        sys.exit(1)

    old_status = item["review_status"]
    old_remark = item["review_remark"] or "(无)"

    log_id = db.review_item(
        db_path,
        batch_id=batch["id"],
        item_id=item_id,
        new_status=status,
        remark=remark or None,
        operator=operator or None,
        action="review",
    )

    status_label = STATUS_LABELS.get(status, status)
    old_label = STATUS_LABELS.get(old_status, old_status)

    click.echo(f"复核完成 (日志 #{log_id})")
    click.echo(f"  证据项: #{item_id} {item['file_path']}")
    click.echo(f"  原状态: {old_label}  备注: {old_remark}")
    click.echo(f"  新状态: {status_label}  备注: {remark or '(无)'}")

    if operator:
        click.echo(f"  操作人: {operator}")


@main.command()
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--operator", "-o", default="", help="操作人")
@click.pass_context
def undo(ctx, batch_no, operator):
    """撤销上一条复核（恢复状态和备注，非仅数字回退）"""
    db_path = ensure_db(ctx)
    batch = get_batch_or_exit(db_path, batch_no)

    undo_log = db.undo_last_review(db_path, batch["id"], operator or None)

    if undo_log is None:
        click.echo(f"错误: 批次 '{batch_no}' 没有可撤销的复核记录", err=True)
        sys.exit(1)

    prev_label = STATUS_LABELS.get(undo_log["prev_status"], undo_log["prev_status"])
    new_label = STATUS_LABELS.get(undo_log["new_status"], undo_log["new_status"])

    click.echo("撤销成功")
    click.echo(f"  证据项: #{undo_log['item_id']} 第{undo_log['manifest_line_no']}行 "
               f"{undo_log['file_path']}")
    click.echo(f"  原操作: {prev_label} → {new_label}")
    click.echo(f"  原备注: {undo_log['new_remark'] or '(无)'}")
    click.echo(f"  恢复为: {prev_label}，备注: {undo_log['prev_remark'] or '(无)'}")

    total, signed, supplement, pending = db.count_reviewed(db_path, batch["id"])
    click.echo(f"  当前统计: 共{total}项，已签收{signed}，待补件{supplement}，待处理{pending}")


@main.command("resume")
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--show-items", "-n", type=int, default=10, help="显示最近 N 条复核历史")
@click.pass_context
def resume_cmd(ctx, batch_no, show_items):
    """继续处理：恢复会话上下文，显示批次状态"""
    db_path = ensure_db(ctx)
    batch = get_batch_or_exit(db_path, batch_no)

    click.echo(f"批次: {batch_no}")
    if batch.get("description"):
        click.echo(f"描述: {batch['description']}")
    click.echo(f"清单文件: {batch['manifest_path']}")
    click.echo(f"证据目录: {batch['evidence_dir']}")
    click.echo(f"创建时间: {format_time(batch['created_at'])}")
    click.echo(f"更新时间: {format_time(batch['updated_at'])}")
    click.echo("")

    total_pc, passed, failed, unchecked = db.count_precheck(db_path, batch["id"])
    click.echo("预检状态:")
    click.echo(f"  总计: {total_pc}  已通过: {passed}  失败: {failed}  未检查: {unchecked}")
    click.echo("")

    total_rv, signed, supplement, pending = db.count_reviewed(db_path, batch["id"])
    click.echo("复核状态:")
    click.echo(f"  总计: {total_rv}  已签收: {signed}  待补件: {supplement}  待处理: {pending}")

    history = db.get_review_history(db_path, batch["id"], limit=show_items)
    if history:
        click.echo("")
        click.echo(f"最近 {min(show_items, len(history))} 条复核记录:")
        for log in history:
            action_label = "撤销" if log["action"] == "undo" else "复核"
            from_label = STATUS_LABELS.get(log["prev_status"], log["prev_status"])
            to_label = STATUS_LABELS.get(log["new_status"], log["new_status"])
            time_str = format_time(log["created_at"])
            click.echo(f"  [{time_str}] {action_label} #{log['item_id']} "
                       f"{log['file_path']}")
            click.echo(f"      {from_label} → {to_label}  "
                       f"备注: {log['new_remark'] or '(无)'}")


@main.command()
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--output", "-o", "output_path", required=True, help="输出文件路径")
@click.option("--format", "-f", "fmt", type=click.Choice(["csv", "json", "auto"]),
              default="auto", help="导出格式（默认根据扩展名自动判断）")
@click.pass_context
def export(ctx, batch_no, output_path, fmt):
    """导出报告（CSV 或 JSON）"""
    db_path = ensure_db(ctx)
    batch = get_batch_or_exit(db_path, batch_no)

    if fmt == "auto":
        try:
            fmt = report_mod.detect_format_by_ext(output_path)
        except ValueError as e:
            click.echo(f"错误: {e}", err=True)
            sys.exit(1)

    items = db.get_evidence_items(db_path, batch["id"])
    if not items:
        click.echo("警告: 批次中没有证据项")

    total_pc, passed, failed, unchecked = db.count_precheck(db_path, batch["id"])
    total_rv, signed, supplement, pending = db.count_reviewed(db_path, batch["id"])

    precheck_stats = {
        "total": total_pc,
        "passed": passed,
        "failed": failed,
        "unchecked": unchecked,
    }
    review_stats = {
        "total": total_rv,
        "signed": signed,
        "supplement": supplement,
        "pending": pending,
    }

    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if fmt == "csv":
        count = report_mod.export_csv(items, output_path, batch_info=batch)
    elif fmt == "json":
        count = report_mod.export_json(
            items, output_path,
            batch_info=batch,
            precheck_stats=precheck_stats,
            review_stats=review_stats,
        )
    else:
        click.echo(f"错误: 不支持的格式: {fmt}", err=True)
        sys.exit(1)

    click.echo(f"报告已导出: {output_path}")
    click.echo(f"  格式: {fmt.upper()}")
    click.echo(f"  条目数: {count}")
    click.echo(f"  已签收: {signed}  待补件: {supplement}  待处理: {pending}")


@main.command("list")
@click.pass_context
def list_cmd(ctx):
    """列出所有批次"""
    db_path = ensure_db(ctx)
    batches = db.list_batches(db_path)

    if not batches:
        click.echo("暂无批次")
        return

    click.echo(f"共 {len(batches)} 个批次:")
    click.echo("")
    for b in batches:
        total, signed, supplement, pending = db.count_reviewed(db_path, b["id"])
        progress = f"{signed}/{total}" if total > 0 else "0/0"
        click.echo(f"  {b['batch_no']}  进度: {progress}  "
                   f"更新: {format_time(b['updated_at'])}")
        if b.get("description"):
            click.echo(f"    描述: {b['description']}")


@main.command()
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--filter", "-f", "filter_status",
              type=click.Choice(["all", "pending", "signed", "supplement", "failed_precheck"]),
              default="all", help="筛选状态")
@click.option("--limit", "-n", type=int, default=20, help="最多显示数量")
@click.pass_context
def status(ctx, batch_no, filter_status, limit):
    """查看批次证据项状态"""
    db_path = ensure_db(ctx)
    batch = get_batch_or_exit(db_path, batch_no)

    items = db.get_evidence_items(db_path, batch["id"])

    if filter_status == "failed_precheck":
        items = [i for i in items if i["precheck_status"] == "failed"]
    elif filter_status != "all":
        items = [i for i in items if i["review_status"] == filter_status]

    displayed = items[:limit]

    click.echo(f"批次 '{batch_no}': 共 {len(items)} 条（显示前 {len(displayed)} 条）")
    click.echo("")

    for item in displayed:
        pc_label = PRECHECK_LABELS.get(item["precheck_status"], item["precheck_status"])
        rv_label = STATUS_LABELS.get(item["review_status"], item["review_status"])
        click.echo(f"  #{item['id']}  {item['file_path']}")
        click.echo(f"      清单行: {item['manifest_line_no']}  "
                   f"预检: {pc_label}  复核: {rv_label}")
        if item.get("review_remark"):
            click.echo(f"      备注: {item['review_remark']}")

    if len(items) > limit:
        click.echo(f"  ... 还有 {len(items) - limit} 条")


@main.group()
@click.pass_context
def snapshot(ctx):
    """批次状态快照管理：保存、列出、恢复"""
    pass


@snapshot.command("save")
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--output", "-o", "output_path", default=None,
              help="快照输出文件路径（默认保存到 .snapshots 目录）")
@click.option("--name", "-n", "snapshot_name", default=None,
              help="快照名称（保存到 .snapshots 目录时使用）")
@click.pass_context
def snapshot_save(ctx, batch_no, output_path, snapshot_name):
    """保存批次状态快照"""
    db_path = ensure_db(ctx)
    work_dir = ctx.obj["work_dir"]

    batch = get_batch_or_exit(db_path, batch_no)

    if output_path is None:
        if snapshot_name is None:
            import time
            timestamp = datetime.datetime.fromtimestamp(time.time()).strftime("%Y%m%d_%H%M%S")
            snapshot_name = f"{batch_no}_{timestamp}"
        output_path = snapshot_mod.get_snapshot_path(work_dir, snapshot_name)

    output_path = os.path.abspath(output_path)

    try:
        snapshot_data = snapshot_mod.save_snapshot(db_path, batch_no, output_path)
    except snapshot_mod.SnapshotNotFoundError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    item_count = len(snapshot_data["items"])
    log_count = len(snapshot_data["review_logs"])

    click.echo(f"快照已保存: {output_path}")
    click.echo(f"  批次: {batch_no}")
    click.echo(f"  证据项: {item_count} 条")
    click.echo(f"  复核记录: {log_count} 条")


@snapshot.command("list")
@click.pass_context
def snapshot_list(ctx):
    """列出所有快照"""
    work_dir = ctx.obj["work_dir"]

    snapshots = snapshot_mod.list_snapshots(work_dir)

    if not snapshots:
        click.echo("暂无快照")
        return

    click.echo(f"共 {len(snapshots)} 个快照:")
    click.echo("")
    for s in snapshots:
        size_str = _format_size(s["size"])
        created_str = format_time(s["created_at"])
        click.echo(f"  {s['name']}")
        click.echo(f"    批次: {s['batch_no']}  大小: {size_str}  创建: {created_str}")


def _format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


@snapshot.command("restore")
@click.option("--snapshot", "-s", "snapshot_path", required=True,
              type=click.Path(dir_okay=False), help="快照文件路径或名称")
@click.option("--force", "-f", is_flag=True, help="强制覆盖已存在的同名批次")
@click.option("--evidence-dir", "-e", "evidence_dir", default=None,
              type=click.Path(file_okay=False), help="重映射证据目录路径")
@click.option("--work-dir", "-w", "target_work_dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True),
              help="目标工作目录（默认使用当前工作目录）")
@click.pass_context
def snapshot_restore(ctx, snapshot_path, force, evidence_dir, target_work_dir):
    """从快照恢复批次到数据库"""
    work_dir = ctx.obj["work_dir"]

    if not os.path.isabs(snapshot_path) and not os.path.exists(snapshot_path):
        candidate = snapshot_mod.get_snapshot_path(work_dir, snapshot_path)
        if os.path.exists(candidate):
            snapshot_path = candidate

    snapshot_path = os.path.abspath(snapshot_path)

    if target_work_dir:
        target_work_dir = os.path.abspath(target_work_dir)
        target_db_path = db.get_db_path(target_work_dir)
        if not os.path.exists(target_db_path):
            db.init_db(target_db_path)
    else:
        target_db_path = ensure_db(ctx)
        target_work_dir = work_dir

    if evidence_dir:
        evidence_dir = os.path.abspath(evidence_dir)

    try:
        batch_no, item_count = snapshot_mod.restore_snapshot(
            db_path=target_db_path,
            snapshot_path=snapshot_path,
            force=force,
            evidence_dir=evidence_dir,
        )
    except snapshot_mod.SnapshotNotFoundError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except snapshot_mod.SnapshotFormatError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except snapshot_mod.SnapshotVersionError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except snapshot_mod.SnapshotConflictError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    click.echo(f"批次已恢复: {batch_no}")
    click.echo(f"  目标数据库: {target_db_path}")
    click.echo(f"  证据项: {item_count} 条")
    if evidence_dir:
        click.echo(f"  证据目录(重映射): {evidence_dir}")


if __name__ == "__main__":
    main()
