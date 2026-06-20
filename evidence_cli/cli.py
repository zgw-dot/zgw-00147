"""离线证据包移交 CLI 主入口"""

import os
import sys
import click
import datetime
from typing import Dict, Optional, List

from . import db
from . import manifest as manifest_mod
from . import precheck as precheck_mod
from . import report as report_mod
from . import snapshot as snapshot_mod
from . import playbook as playbook_mod
from . import handoff as handoff_mod


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
    os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

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


def _show_restore_info(batch: Dict, db_path: Optional[str] = None) -> None:
    """显示恢复来源信息，使用统一恢复摘要"""
    if not batch.get("restored_from") and not db_path:
        return

    if db_path:
        recovery_summary = snapshot_mod.build_recovery_summary(db_path, batch["batch_no"])
        if recovery_summary and recovery_summary.get("has_restore"):
            src = recovery_summary.get("source_snapshot")
            if src:
                snap_marker = "[OK]" if src.get("exists") else "[MISSING](已丢失)"
                click.echo(f"来源快照: {snap_marker} {src.get('path', '')}")
                handoff_pkg = src.get("handoff_package")
                if handoff_pkg:
                    pkg_marker = "[OK]" if handoff_pkg.get("exists") else "[MISSING](已删除)"
                    click.echo(f"交接包来源: {pkg_marker} {handoff_pkg.get('path', '')}")
                    click.echo(f"  打包人: {handoff_pkg.get('operator', '未知')}")
                    source_summary = src.get("source_summary", {})
                    if isinstance(source_summary, dict) and source_summary.get("work_dir"):
                        click.echo(f"  来源 work-dir: {source_summary['work_dir']}")
                    if isinstance(source_summary, dict) and source_summary.get("operator"):
                        click.echo(f"  打包操作人: {source_summary['operator']}")
            evt = recovery_summary.get("restore_event")
            if evt and evt.get("restored_at"):
                click.echo(f"恢复时间: {format_time(evt['restored_at'])}")
                if evt.get("operator"):
                    click.echo(f"操作人: {evt['operator']}")
                tag_parts = []
                if evt.get("was_force"):
                    tag_parts.append("强制覆盖")
                if evt.get("was_remapped"):
                    tag_parts.append("目录重映射")
                if tag_parts:
                    click.echo(f"恢复方式: {'、'.join(tag_parts)}")

            diff = recovery_summary.get("overwrite_diff")
            if diff:
                old_desc = diff.get("old_batch", {}).get("description", "(无)")
                new_desc = diff.get("new_batch", {}).get("description", "(无)")
                click.echo(f"覆盖差异: 旧批次「{old_desc}」→ 新批次「{new_desc}」")
                old_rv = diff.get("review_stats", {}).get("old", {})
                new_rv = diff.get("review_stats", {}).get("new", {})
                click.echo(
                    f"            复核: 已签收 {old_rv.get('signed', 0)} → {new_rv.get('signed', 0)}  "
                    f"待补件 {old_rv.get('supplement', 0)} → {new_rv.get('supplement', 0)}"
                )

            trace = snapshot_mod.build_trace(db_path, batch["batch_no"])
            if trace and trace["has_restore_chain"]:
                click.echo(f"恢复链路: 共 {len(trace['events'])} 次恢复")
                ops = recovery_summary.get("post_restore_ops", {})
                op_count = ops.get("count", 0)
                if op_count > 0:
                    click.echo(
                        f"  [!] 恢复后有 {op_count} 条新操作"
                        f"（复核 {ops.get('review_count', 0)} 条，撤销 {ops.get('undo_count', 0)} 条）"
                        f"（使用 trace 命令查看详情）"
                    )
                else:
                    click.echo("  恢复后未再修改（使用 trace 命令查看完整链路）")

            recon = recovery_summary.get("reconciled", False)
            if recon:
                click.echo("对账: [OK] 通过")
            else:
                click.echo("对账: [!] 存在告警（使用 trace 命令查看详情）")
            click.echo("")
            return

    click.echo(f"来源快照: {batch['restored_from']}")
    click.echo(f"恢复时间: {format_time(batch.get('restored_at'))}")
    if batch.get("restore_diff"):
        import json
        try:
            diff = json.loads(batch["restore_diff"])
            old_desc = diff.get("old_batch", {}).get("description", "(无)")
            new_desc = diff.get("new_batch", {}).get("description", "(无)")
            click.echo(f"覆盖差异: 旧批次「{old_desc}」→ 新批次「{new_desc}」")
            old_rv = diff.get("review_stats", {}).get("old", {})
            new_rv = diff.get("review_stats", {}).get("new", {})
            click.echo(f"            复核: 已签收 {old_rv.get('signed', 0)} → {new_rv.get('signed', 0)}  "
                       f"待补件 {old_rv.get('supplement', 0)} → {new_rv.get('supplement', 0)}")
        except (json.JSONDecodeError, TypeError):
            pass
    click.echo("")


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
    _show_restore_info(batch, db_path)
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

    restore_trace = snapshot_mod.build_trace(db_path, batch_no)

    if fmt == "csv":
        count = report_mod.export_csv(items, output_path, batch_info=batch)
    elif fmt == "json":
        count = report_mod.export_json(
            items, output_path,
            batch_info=batch,
            precheck_stats=precheck_stats,
            review_stats=review_stats,
            restore_trace=restore_trace,
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
        status_tag = ""
        recovery_summary = snapshot_mod.build_recovery_summary(db_path, b["batch_no"])
        if recovery_summary and recovery_summary.get("has_restore"):
            trace = snapshot_mod.build_trace(db_path, b["batch_no"])
            if trace and trace["has_restore_chain"]:
                evt_count = len(trace["events"])
                ops = recovery_summary.get("post_restore_ops", {})
                mod_tag = ""
                if ops.get("count", 0) > 0:
                    mod_tag = f" [已修改+{ops['count']}]"
                if evt_count > 1:
                    status_tag = f" [已恢复×{evt_count}{mod_tag}]"
                else:
                    status_tag = f" [已恢复{mod_tag}]"
            else:
                status_tag = " [已恢复]"
            if not recovery_summary.get("reconciled", True):
                status_tag += " [!对账]"
        click.echo(f"  {b['batch_no']}{status_tag}  进度: {progress}  "
                   f"更新: {format_time(b['updated_at'])}")
        if b.get("description"):
            click.echo(f"    描述: {b['description']}")
        if recovery_summary and recovery_summary.get("has_restore"):
            src = recovery_summary.get("source_snapshot")
            if src:
                snap_marker = "[OK]" if src.get("exists") else "[MISSING]"
                click.echo(f"    来源快照: {snap_marker} {src.get('path', '')}")
            evt = recovery_summary.get("restore_event")
            if evt and evt.get("restored_at"):
                click.echo(f"    恢复时间: {format_time(evt['restored_at'])}")
            ops = recovery_summary.get("post_restore_ops", {})
            if ops.get("count", 0) > 0:
                click.echo(
                    f"    恢复后操作: {ops['count']} 条"
                    f"（复核 {ops.get('review_count', 0)}，撤销 {ops.get('undo_count', 0)}）"
                    f"（trace 查看详情）"
                )
            for w in recovery_summary.get("warnings", []):
                click.echo(f"    [!] {w}")


@main.command()
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.pass_context
def trace(ctx, batch_no):
    """查看批次恢复链路来龙去脉"""
    db_path = ensure_db(ctx)
    trace_data = snapshot_mod.build_trace(db_path, batch_no)
    if trace_data is None:
        click.echo(f"错误: 批次 '{batch_no}' 不存在", err=True)
        sys.exit(1)

    _format_trace_output(trace_data)


def _format_trace_output(trace_data: Dict) -> None:
    """格式化输出恢复链路追踪信息"""
    recovery_summary = trace_data.get("recovery_summary")
    if recovery_summary:
        _format_unified_recovery_summary(
            recovery_summary,
            title=f"批次恢复链路: {trace_data['batch_no']}",
        )
    else:
        click.echo("=" * 60)
        click.echo(f"批次恢复链路: {trace_data['batch_no']}")
        click.echo("=" * 60)

    batch = trace_data["batch"]
    click.echo(f"清单文件: {batch['manifest_path']}")
    click.echo(f"证据目录: {batch['evidence_dir']}")
    if batch.get("description"):
        click.echo(f"批次描述: {batch['description']}")
    click.echo(f"创建时间: {format_time(batch['created_at'])}")
    click.echo(f"最后更新: {format_time(batch['updated_at'])}")
    click.echo("")

    warnings = trace_data.get("warnings", [])
    if warnings:
        click.echo("[!] 链路告警:")
        for w in warnings:
            click.echo(f"    {w}")
        click.echo("")

    if not trace_data["has_restore_chain"]:
        if batch.get("restored_from"):
            click.echo("该批次由旧版本恢复，仅有基础来源信息，无完整链路：")
            click.echo(f"  来源快照: {batch.get('restored_from')}")
            click.echo(f"  恢复时间: {format_time(batch.get('restored_at'))}")
        else:
            click.echo("该批次从未从快照恢复，为原始导入批次。")
        click.echo("=" * 60)
        return

    events = trace_data["events"]
    click.echo(f"恢复链路：共 {len(events)} 次恢复（从早到晚）")
    click.echo("")

    for idx, ev in enumerate(events, start=1):
        click.echo("-" * 60)
        tag_parts = []
        if ev["was_force"]:
            tag_parts.append("强制覆盖")
        if ev["was_remapped"]:
            tag_parts.append("目录重映射")
        tag = f" [{'、'.join(tag_parts)}]" if tag_parts else ""
        click.echo(f"[#{idx}] 恢复事件 #{ev['event_id']}{tag}")
        click.echo(f"    恢复时间: {format_time(ev['restored_at'])}")

        snap_marker = "[OK]" if ev["snapshot_exists"] else "[MISSING](已丢失)"
        click.echo(f"    来源快照: {snap_marker} {ev['snapshot_path']}")
        if ev.get("snapshot_created_at"):
            click.echo(f"    快照创建: {format_time(ev['snapshot_created_at'])}")
        if ev.get("operator"):
            click.echo(f"    操作人: {ev['operator']}")

        handoff_import = ev.get("handoff_import")
        if handoff_import:
            pkg_path = handoff_import.get("path", "")
            pkg_marker = "[OK]" if handoff_import.get("exists") else "[MISSING](已删除)"
            click.echo(f"    交接包来源: {pkg_marker} {pkg_path}")
            if handoff_import.get("operator"):
                click.echo(f"    交接包操作人: {handoff_import['operator']}")
            source_summary = handoff_import.get("source_summary", {})
            if isinstance(source_summary, dict):
                if source_summary.get("work_dir"):
                    click.echo(f"    来源 work-dir: {source_summary['work_dir']}")
                if source_summary.get("operator"):
                    click.echo(f"    来源打包人: {source_summary['operator']}")
            restore_result = handoff_import.get("restore_result", {})
            if isinstance(restore_result, dict) and restore_result.get("manifest_path"):
                click.echo(f"    目标端清单: {restore_result['manifest_path']}")

        if ev.get("parent_event_id"):
            if ev["chain_ok"]:
                click.echo(f"    父事件: #{ev['parent_event_id']}（链路连续）")
            else:
                click.echo(f"    父事件: #{ev['parent_event_id']} [BROKEN](链路断档)")
        else:
            click.echo("    父事件: 无（链路起点）")

        click.echo("")
        click.echo("    路径映射:")
        if ev["was_remapped"]:
            click.echo(f"      证据目录: {ev.get('evidence_dir_before', '(未知)')}")
            click.echo(f"                v (重映射)")
            click.echo(f"                {ev['evidence_dir_after']}")
        else:
            if ev.get("evidence_dir_before") and ev["evidence_dir_before"] != ev["evidence_dir_after"]:
                click.echo(f"      证据目录: {ev['evidence_dir_before']} → {ev['evidence_dir_after']}")
            else:
                click.echo(f"      证据目录: {ev['evidence_dir_after']}")
        if ev.get("manifest_path_before") and ev["manifest_path_before"] != ev["manifest_path_after"]:
            click.echo(f"      清单文件: {ev['manifest_path_before']} → {ev['manifest_path_after']}")
        else:
            click.echo(f"      清单文件: {ev['manifest_path_after']}")

        if ev["was_force"] and ev.get("old_batch_snapshot"):
            old = ev["old_batch_snapshot"]
            old_desc = old.get("batch", {}).get("description", "(无描述)")
            old_rv = old.get("review_stats", {})
            click.echo("")
            click.echo(f"    覆盖前批次:「{old_desc}」")
            click.echo(f"      证据项: {old.get('item_count', '?')} 项")
            click.echo(
                f"      复核: 已签收 {old_rv.get('signed', 0)}  "
                f"待补件 {old_rv.get('supplement', 0)}  "
                f"待处理 {old_rv.get('pending', 0)}"
            )
            click.echo(f"      复核日志: {old.get('review_log_count', '?')} 条")

        if ev.get("restore_diff"):
            diff = ev["restore_diff"]
            old_rv = diff.get("review_stats", {}).get("old", {})
            new_rv = diff.get("review_stats", {}).get("new", {})
            items_diff = diff.get("items", {})
            click.echo("")
            click.echo("    覆盖差异:")
            click.echo(
                f"      复核统计: 已签收 {old_rv.get('signed', 0)} → {new_rv.get('signed', 0)}  "
                f"待补件 {old_rv.get('supplement', 0)} → {new_rv.get('supplement', 0)}  "
                f"待处理 {old_rv.get('pending', 0)} → {new_rv.get('pending', 0)}"
            )
            only_old = items_diff.get("only_in_old", [])
            only_new = items_diff.get("only_in_new", [])
            if only_old:
                click.echo(f"      仅在旧批次: {len(only_old)} 项")
                for p in only_old[:3]:
                    click.echo(f"        - {p}")
                if len(only_old) > 3:
                    click.echo(f"        ... 还有 {len(only_old) - 3} 项")
            if only_new:
                click.echo(f"      仅在新批次: {len(only_new)} 项")
                for p in only_new[:3]:
                    click.echo(f"        - {p}")
                if len(only_new) > 3:
                    click.echo(f"        ... 还有 {len(only_new) - 3} 项")

        if ev["warnings"]:
            click.echo("")
            for w in ev["warnings"]:
                click.echo(f"    [!] {w}")

    click.echo("-" * 60)
    click.echo("")

    post = trace_data.get("post_restore_activity", [])
    if post:
        click.echo(f"恢复后追加操作（共 {len(post)} 条）:")
        for log in post:
            action_label = "撤销" if log["action"] == "undo" else "复核"
            from_label = STATUS_LABELS.get(log["prev_status"], log["prev_status"])
            to_label = STATUS_LABELS.get(log["new_status"], log["new_status"])
            time_str = format_time(log["created_at"])
            click.echo(f"  [{time_str}] {action_label} #{log['item_id']} {log.get('file_path', '')}")
            click.echo(f"      {from_label} → {to_label}")
            if log.get("new_remark"):
                click.echo(f"      备注: {log['new_remark']}")
            if log.get("operator"):
                click.echo(f"      操作人: {log['operator']}")
    else:
        click.echo("恢复后未追加任何复核或撤销操作。")

    click.echo("")
    click.echo("=" * 60)


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


def _format_unified_recovery_summary(
    recovery_summary: Dict,
    title: str = "恢复摘要",
    include_post_ops: bool = True,
    include_warnings: bool = True,
) -> None:
    """
    统一格式化输出恢复摘要。

    这是 trace / list / resume / preview / 恢复完成 等所有视图共用的格式化函数，
    保证各命令输出的摘要信息完全一致。

    参数:
        recovery_summary: build_recovery_summary 或 build_recovery_summary_from_preview 返回的摘要
        title: 输出标题
        include_post_ops: 是否显示恢复后新增操作数
        include_warnings: 是否显示告警
    """
    click.echo("=" * 60)
    click.echo(title)
    click.echo("=" * 60)
    click.echo(f"批次号: {recovery_summary['batch_no']}")

    if not recovery_summary.get("has_restore"):
        click.echo("状态: 原始导入批次，从未从快照恢复")
        click.echo("")
    else:
        src = recovery_summary.get("source_snapshot")
        if src:
            snap_marker = "[OK]" if src.get("exists") else "[MISSING](已丢失)"
            click.echo(f"来源快照: {snap_marker} {src.get('path', '')}")
            if src.get("created_at"):
                click.echo(f"快照创建: {format_time(src['created_at'])}")

        evt = recovery_summary.get("restore_event")
        if evt and evt.get("restored_at"):
            click.echo(f"恢复时间: {format_time(evt['restored_at'])}")
            if evt.get("operator"):
                click.echo(f"操作人: {evt['operator']}")
            tag_parts = []
            if evt.get("was_force"):
                tag_parts.append("强制覆盖")
            if evt.get("was_remapped"):
                tag_parts.append("目录重映射")
            if tag_parts:
                click.echo(f"恢复方式: {'、'.join(tag_parts)}")
        elif evt:
            tag_parts = []
            if evt.get("was_force"):
                tag_parts.append("强制覆盖")
            if evt.get("was_remapped"):
                tag_parts.append("目录重映射")
            if tag_parts:
                click.echo(f"预计恢复方式: {'、'.join(tag_parts)}")

        click.echo("")

        rv = recovery_summary.get("review_stats", {})
        click.echo("复核统计:")
        click.echo(
            f"  总计: {rv.get('total', 0)}  已签收: {rv.get('signed', 0)}  "
            f"待补件: {rv.get('supplement', 0)}  待处理: {rv.get('pending', 0)}"
        )
        click.echo("")

        pc = recovery_summary.get("precheck_stats", {})
        click.echo("预检统计:")
        click.echo(
            f"  总计: {pc.get('total', 0)}  通过: {pc.get('passed', 0)}  "
            f"失败: {pc.get('failed', 0)}  未检查: {pc.get('unchecked', 0)}"
        )
        click.echo(f"证据项数量: {recovery_summary.get('item_count', 0)}")
        click.echo("")

        last_log = recovery_summary.get("last_review_log")
        if last_log:
            action_label = "撤销" if last_log.get("action") == "undo" else "复核"
            click.echo("最后一条复核记录:")
            click.echo(f"  [{format_time(last_log.get('created_at', 0))}] {action_label}")
            if last_log.get("file_path"):
                fp = last_log["file_path"]
                if last_log.get("manifest_line_no"):
                    click.echo(f"  文件(清单第{last_log['manifest_line_no']}行): {fp}")
                else:
                    click.echo(f"  文件: {fp}")
            from_label = STATUS_LABELS.get(last_log.get("prev_status"), last_log.get("prev_status"))
            to_label = STATUS_LABELS.get(last_log.get("new_status"), last_log.get("new_status"))
            click.echo(f"  状态: {from_label} → {to_label}")
            if last_log.get("new_remark"):
                click.echo(f"  备注: {last_log['new_remark']}")
            if last_log.get("operator"):
                click.echo(f"  操作人: {last_log['operator']}")
            click.echo("")

        diff = recovery_summary.get("overwrite_diff")
        if diff:
            click.echo("-" * 60)
            click.echo("覆盖差异 (旧 → 新)")
            click.echo("-" * 60)
            old_batch = diff.get("old_batch", {})
            new_batch = diff.get("new_batch", {})
            old_desc = old_batch.get("description", "(无描述)")
            new_desc = new_batch.get("description", "(无描述)")
            if old_desc or new_desc:
                click.echo(f"批次描述: 「{old_desc}」→「{new_desc}」")

            old_rv = diff.get("review_stats", {}).get("old", {})
            new_rv = diff.get("review_stats", {}).get("new", {})
            click.echo(
                f"复核统计: 已签收 {old_rv.get('signed', 0)} → {new_rv.get('signed', 0)}  "
                f"待补件 {old_rv.get('supplement', 0)} → {new_rv.get('supplement', 0)}  "
                f"待处理 {old_rv.get('pending', 0)} → {new_rv.get('pending', 0)}"
            )

            items_diff = diff.get("items", {})
            only_old = items_diff.get("only_in_old", [])
            only_new = items_diff.get("only_in_new", [])
            in_both = items_diff.get("in_both", [])
            if only_old:
                click.echo(f"仅在旧批次: {len(only_old)} 项")
                for p in only_old[:3]:
                    click.echo(f"  - {p}")
                if len(only_old) > 3:
                    click.echo(f"  ... 还有 {len(only_old) - 3} 项")
            if only_new:
                click.echo(f"仅在新批次: {len(only_new)} 项")
                for p in only_new[:3]:
                    click.echo(f"  - {p}")
                if len(only_new) > 3:
                    click.echo(f"  ... 还有 {len(only_new) - 3} 项")
            if in_both:
                click.echo(f"双方共有: {len(in_both)} 项")
            click.echo("")

        if include_post_ops:
            ops = recovery_summary.get("post_restore_ops", {})
            op_count = ops.get("count", 0)
            if op_count > 0:
                click.echo(
                    f"恢复后新增操作: {op_count} 条"
                    f"（复核 {ops.get('review_count', 0)} 条，撤销 {ops.get('undo_count', 0)} 条）"
                )
            else:
                click.echo("恢复后新增操作: 0 条（未修改）")
            click.echo("")

        recon = recovery_summary.get("reconciled", False)
        recon_details = recovery_summary.get("reconciliation_details", {})
        recon_tag = "[OK] 对账通过" if recon else "[!] 对账告警"
        recon_parts = []
        if recon_details.get("item_count_consistent"):
            recon_parts.append("item数一致")
        else:
            recon_parts.append("item数不一致")
        if recon_details.get("review_stats_consistent"):
            recon_parts.append("复核统计一致")
        else:
            recon_parts.append("复核统计不一致")
        if recon_details.get("post_restore_count_consistent"):
            recon_parts.append("操作计数一致")
        else:
            recon_parts.append("操作计数不一致")
        click.echo(f"{recon_tag}: {'、'.join(recon_parts)}")
        click.echo("")

        if include_warnings:
            for w in recovery_summary.get("warnings", []):
                click.echo(f"[!] {w}")

    click.echo("=" * 60)


def _format_preview(preview: Dict) -> None:
    """格式化预演信息输出，使用统一恢复摘要结构"""
    recovery_summary = snapshot_mod.build_recovery_summary_from_preview(preview)
    _format_unified_recovery_summary(recovery_summary, title="恢复预演")

    if preview.get("can_restore"):
        click.echo("[OK] 可以恢复（以上为落库后的预期摘要）")
    else:
        click.echo("[X] 无法恢复，请修正上述问题后重试")
    click.echo("=" * 60)


def _format_restore_summary(summary: Dict, db_path: str, batch_no: str) -> None:
    """格式化恢复完成摘要，从数据库读取统一恢复摘要输出"""
    recovery_summary = snapshot_mod.build_recovery_summary(db_path, batch_no)
    if recovery_summary:
        _format_unified_recovery_summary(recovery_summary, title="恢复完成")
    else:
        click.echo("=" * 60)
        click.echo("恢复完成")
        click.echo("=" * 60)
        click.echo(f"批次号: {summary['batch_no']}")
        click.echo(f"来源快照: {summary['restored_from']}")
        click.echo(f"证据项数量: {summary['item_count']}")
        click.echo("=" * 60)


@snapshot.command("restore")
@click.option("--snapshot", "-s", "snapshot_path", required=True,
              type=click.Path(dir_okay=False), help="快照文件路径或名称")
@click.option("--force", "-f", is_flag=True, help="强制覆盖已存在的同名批次")
@click.option("--evidence-dir", "-e", "evidence_dir", default=None,
              type=click.Path(file_okay=False), help="重映射证据目录路径")
@click.option("--work-dir", "-w", "target_work_dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True),
              help="目标工作目录（默认使用当前工作目录）")
@click.option("--dry-run", is_flag=True,
              help="预演恢复，不修改数据库，仅显示恢复信息")
@click.option("--operator", "-o", default=None, help="操作人（写入恢复事件）")
@click.pass_context
def snapshot_restore(ctx, snapshot_path, force, evidence_dir, target_work_dir, dry_run, operator):
    """从快照恢复批次到数据库（支持预演）"""
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
        preview = snapshot_mod.preview_restore(
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

    _format_preview(preview)

    if dry_run:
        if not preview["can_restore"]:
            sys.exit(1)
        return

    if not preview["can_restore"]:
        sys.exit(1)

    try:
        batch_no, item_count, summary = snapshot_mod.restore_snapshot(
            db_path=target_db_path,
            snapshot_path=snapshot_path,
            force=force,
            evidence_dir=evidence_dir,
            operator=operator,
        )
    except snapshot_mod.SnapshotConflictError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except snapshot_mod.SnapshotMissingFilesError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    _format_restore_summary(summary, target_db_path, batch_no)


def _format_command_chain(chain_data: Dict) -> None:
    """格式化输出恢复核对命令链"""
    recovery_summary = chain_data.get("recovery_summary")
    if recovery_summary:
        _format_unified_recovery_summary(
            recovery_summary,
            title=f"恢复核对命令链: {chain_data['batch_no']}",
        )
    else:
        click.echo("=" * 60)
        click.echo(f"恢复核对命令链: {chain_data['batch_no']}")
        click.echo("=" * 60)

    click.echo(f"当前场景: {chain_data['scenario']}")
    click.echo("")

    warnings = chain_data.get("warnings", [])
    if warnings:
        click.echo("[!] 告警:")
        for w in warnings:
            click.echo(f"    {w}")
        click.echo("")

    steps = chain_data.get("steps", [])
    applicable_steps = [s for s in steps if s["applicable"]]
    inapplicable_steps = [s for s in steps if not s["applicable"]]

    click.echo(f"可执行步骤（共 {len(applicable_steps)} 步）:")
    click.echo("")

    for s in applicable_steps:
        req_tag = "[必填]" if s["required"] else "[可选]"
        click.echo(f"--- 步骤 {s['order']} {req_tag} {s['name']} ---")
        click.echo(f"说明: {s['description']}")
        click.echo("")
        click.echo(f"命令: {s['command']}")
        if s["required_options"]:
            click.echo("必填选项:")
            for opt in s["required_options"]:
                val_str = f"={opt['value']}" if opt["value"] else ""
                click.echo(f"  {opt['option']}{val_str}  —  {opt['reason']}")
        if s["optional_options"]:
            click.echo("可选选项:")
            for opt in s["optional_options"]:
                val_str = f"={opt['value']}" if opt["value"] else ""
                click.echo(f"  {opt['option']}{val_str}  —  {opt['reason']}")
        click.echo("")

    if inapplicable_steps:
        click.echo(f"不适用步骤（共 {len(inapplicable_steps)} 步）:")
        click.echo("")
        for s in inapplicable_steps:
            click.echo(f"  步骤 {s['order']} {s['name']}  —  {s['applicable_reason']}")
        click.echo("")

    click.echo("=" * 60)
    click.echo("数据来源说明:")
    click.echo("  以上所有命令和摘要均从 SQLite 持久化数据（batches、restore_events、")
    click.echo("  evidence_items、review_logs）动态聚合生成，与 list / resume / trace /")
    click.echo("  export 命令共用同一份数据源，重开 CLI 查询结果完全一致。")
    click.echo("=" * 60)


@snapshot.command("check")
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.pass_context
def snapshot_check(ctx, batch_no):
    """恢复核对入口：列出恢复后所有可执行命令链、摘要、必填选项"""
    db_path = ensure_db(ctx)

    chain_data = snapshot_mod.build_command_chain(db_path, batch_no)
    if chain_data is None:
        click.echo(f"错误: 批次 '{batch_no}' 不存在", err=True)
        sys.exit(1)

    _format_command_chain(chain_data)


@main.group()
@click.pass_context
def playbook(ctx):
    """批次操作剧本：生成、导入、检查、预演、执行、回看历史"""
    pass


@playbook.command("import")
@click.option("--playbook-file", "-p", "playbook_path", required=True,
              type=click.Path(exists=True, dir_okay=False), help="剧本 JSON 文件路径")
@click.pass_context
def playbook_import(ctx, playbook_path):
    """导入剧本文件，验证格式和版本"""
    db_path = ensure_db(ctx)
    playbook_path = os.path.abspath(playbook_path)

    try:
        pb = playbook_mod.load_playbook(playbook_path)
    except playbook_mod.PlaybookVersionError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except playbook_mod.PlaybookFormatError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except playbook_mod.PlaybookError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    batch_no = pb["batch_no"]
    click.echo(f"剧本导入成功: {playbook_path}")
    click.echo(f"  版本: {pb['version']}")
    click.echo(f"  批次: {batch_no}")
    if pb.get("description"):
        click.echo(f"  描述: {pb['description']}")
    if pb.get("operator"):
        click.echo(f"  操作人: {pb['operator']}")
    if pb.get("output_file"):
        click.echo(f"  输出文件: {pb['output_file']}")
    click.echo(f"  步骤数: {len(pb['steps'])}")
    click.echo("")

    batch = db.get_batch_by_no(db_path, batch_no)
    if not batch:
        click.echo(f"[!] 警告: 批次 '{batch_no}' 不存在，预演和执行将失败")
    else:
        click.echo(f"  批次状态: 已签收/待补件/待处理")
        if "batch_updated_at" not in pb:
            pb["batch_updated_at"] = batch["updated_at"]
            with open(playbook_path, "w", encoding="utf-8") as f:
                import json as _json
                _json.dump(pb, f, ensure_ascii=False, indent=2)
            click.echo(f"  已记录批次当前 updated_at 到剧本（用于变更检测）")

    click.echo("")
    click.echo("步骤列表:")
    for step in pb["steps"]:
        desc = _describe_playbook_step(step)
        click.echo(f"  {step['order']}. {desc}")

    click.echo("")
    click.echo("提示: 使用 'evi playbook preview -p <剧本文件>' 预演")
    click.echo("      使用 'evi playbook execute -p <剧本文件>' 正式执行")


def _describe_playbook_step(step: Dict) -> str:
    parts = [f"[{step['type']}]"]
    if step["type"] == "review":
        parts.append(f"目标状态: {step.get('target_status', '?')}")
    if step.get("filter_status"):
        parts.append(f"筛选: {step['filter_status']}")
    if step.get("line_range"):
        parts.append(f"行号: {step['line_range'][0]}-{step['line_range'][1]}")
    if step.get("remark_template"):
        parts.append(f"备注模板: {step['remark_template']}")
    if step.get("operator"):
        parts.append(f"操作人: {step['operator']}")
    if step["type"] == "export":
        parts.append(f"输出: {step.get('output_path', '?')}")
    return " ".join(parts)


@playbook.command("preview")
@click.option("--playbook-file", "-p", "playbook_path", required=True,
              type=click.Path(exists=True, dir_okay=False), help="剧本 JSON 文件路径")
@click.pass_context
def playbook_preview(ctx, playbook_path):
    """预演剧本，显示命中的证据项、跳过步骤、覆盖和冲突"""
    db_path = ensure_db(ctx)
    playbook_path = os.path.abspath(playbook_path)

    try:
        pb = playbook_mod.load_playbook(playbook_path)
    except playbook_mod.PlaybookError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    preview = playbook_mod.preview_playbook(db_path, pb)

    click.echo("=" * 60)
    click.echo(f"剧本预演: 批次 '{preview['batch_no']}'")
    click.echo("=" * 60)

    if not preview["can_execute"]:
        click.echo("[X] 无法执行")
    else:
        click.echo("[OK] 可以执行")

    if preview["global_conflicts"]:
        click.echo("")
        click.echo("[!] 全局冲突:")
        for c in preview["global_conflicts"]:
            click.echo(f"    {c}")

    click.echo("")
    total_hit = 0
    total_skip = 0
    total_overwrite = 0
    total_conflict = 0

    for sp in preview["steps"]:
        click.echo(f"--- 步骤 {sp['order']}: {sp['type']} ---")

        matched = sp["matched_items"]
        skipped = sp["skipped_reasons"]
        overwrites = sp["will_overwrite"]
        conflicts = sp["conflicts"]

        total_hit += len(matched)
        total_skip += len(skipped)
        total_overwrite += len(overwrites)
        total_conflict += len(conflicts)

        if matched:
            click.echo(f"  命中: {len(matched)} 项")
            for m in matched[:10]:
                fp = m.get("file_path", "")
                iid = m.get("id", "?")
                click.echo(f"    #{iid} {fp}")
                if m.get("current_status") and m.get("target_status"):
                    click.echo(f"      {m['current_status']} → {m['target_status']}")
                if m.get("current_precheck_status"):
                    click.echo(f"      预检: {m['current_precheck_status']}")
                if m.get("will_revert_to"):
                    click.echo(f"      {m.get('current_status', '?')} → {m['will_revert_to']}")
            if len(matched) > 10:
                click.echo(f"    ... 还有 {len(matched) - 10} 项")

        if skipped:
            click.echo(f"  跳过: {len(skipped)} 条")
            for s in skipped:
                click.echo(f"    - {s}")

        if overwrites:
            click.echo(f"  覆盖: {len(overwrites)} 条")
            for o in overwrites:
                click.echo(f"    - {o}")

        if conflicts:
            click.echo(f"  冲突: {len(conflicts)} 条")
            for c in conflicts:
                click.echo(f"    [!] {c}")

        click.echo("")

    click.echo("-" * 60)
    click.echo(f"汇总: 命中 {total_hit} 项, 跳过 {total_skip} 步, "
               f"覆盖 {total_overwrite} 条, 冲突 {total_conflict} 条")

    if preview["can_execute"]:
        click.echo("")
        click.echo("提示: 使用 'evi playbook execute -p <剧本文件>' 正式执行")
    else:
        click.echo("")
        click.echo("请修正上述问题后重试")

    click.echo("=" * 60)

    if not preview["can_execute"]:
        sys.exit(1)


@playbook.command("execute")
@click.option("--playbook-file", "-p", "playbook_path", required=True,
              type=click.Path(exists=True, dir_okay=False), help="剧本 JSON 文件路径")
@click.option("--operator", "-o", default=None, help="覆盖剧本中的操作人")
@click.option("--force", "-f", is_flag=True,
              help="强制执行（跳过批次修改检测和输出文件冲突）")
@click.pass_context
def playbook_execute(ctx, playbook_path, operator, force):
    """正式执行剧本，每步结果落 SQLite 日志"""
    db_path = ensure_db(ctx)
    playbook_path = os.path.abspath(playbook_path)

    try:
        pb = playbook_mod.load_playbook(playbook_path)
    except playbook_mod.PlaybookError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    try:
        result = playbook_mod.execute_playbook(
            db_path, pb, operator=operator, force=force,
        )
    except playbook_mod.PlaybookConflictError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except playbook_mod.PlaybookStepError as e:
        click.echo(f"执行失败: {e}", err=True)
        sys.exit(1)

    click.echo("=" * 60)
    click.echo(f"剧本执行: 批次 '{result['batch_no']}'")
    click.echo(f"运行 ID: #{result['run_id']}")
    click.echo(f"状态: {result['status']}")
    click.echo("=" * 60)

    for sr in result["steps"]:
        status_tag = "[OK]" if sr["status"] == "success" else (
            "[SKIP]" if sr["status"] == "skipped" else "[FAIL]"
        )
        click.echo(f"  步骤 {sr['order']}: {sr['type']} {status_tag}")

        if sr["status"] == "success" and sr.get("affected_items"):
            for ai in sr["affected_items"]:
                if sr["type"] == "review":
                    click.echo(f"    #{ai.get('id', '?')} {ai.get('file_path', '')} "
                               f"{ai.get('old_status', '')} → {ai.get('new_status', '')}")
                elif sr["type"] == "precheck":
                    click.echo(f"    #{ai.get('id', '?')} {ai.get('file_path', '')} "
                               f"预检: {ai.get('precheck_status', '')}")
                elif sr["type"] == "undo":
                    click.echo(f"    #{ai.get('item_id', '?')} {ai.get('file_path', '')} "
                               f"{ai.get('reverted_from', '')} → {ai.get('reverted_to', '')}")
                elif sr["type"] == "export":
                    click.echo(f"    输出: {ai.get('output_path', '')} "
                               f"({ai.get('format', '')}, {ai.get('count', 0)} 条)")

        if sr["status"] == "failed" and sr.get("error"):
            click.echo(f"    错误: {sr['error']}")

        if sr["status"] == "skipped":
            click.echo("    (无匹配项，已跳过)")

    if result["status"] == "rolled_back":
        click.echo("")
        click.echo("[!] 剧本执行失败，已回滚前面成功的步骤")
        if result.get("error"):
            click.echo(f"    失败原因: {result['error']}")

    click.echo("=" * 60)

    if result["status"] != "completed":
        sys.exit(1)


@playbook.command("history")
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--limit", "-n", type=int, default=10, help="最多显示数量")
@click.pass_context
def playbook_history(ctx, batch_no, limit):
    """查看批次剧本执行历史，跨进程持久化"""
    db_path = ensure_db(ctx)

    runs = playbook_mod.get_playbook_history(db_path, batch_no, limit=limit)

    if not runs:
        click.echo(f"批次 '{batch_no}' 暂无剧本执行记录")
        return

    click.echo(f"批次 '{batch_no}' 剧本执行历史（共 {len(runs)} 条）:")
    click.echo("")

    for run in runs:
        status_tag = {
            "completed": "[OK]",
            "failed": "[FAIL]",
            "executing": "[...]",
            "rolled_back": "[ROLLBACK]",
        }.get(run["status"], "[?]")
        click.echo(f"  运行 #{run['id']} {status_tag} {run['status']}")
        click.echo(f"    操作人: {run.get('operator') or '(无)'}")
        click.echo(f"    开始: {format_time(run['started_at'])}")
        if run.get("finished_at"):
            click.echo(f"    结束: {format_time(run['finished_at'])}")
        if run.get("error_message"):
            click.echo(f"    错误: {run['error_message']}")

        detail = db.get_playbook_run_with_steps(db_path, run["id"])
        if detail and detail.get("steps"):
            click.echo(f"    步骤:")
            for s in detail["steps"]:
                s_tag = "[OK]" if s["status"] == "success" else (
                    "[SKIP]" if s["status"] == "skipped" else "[FAIL]"
                )
                click.echo(f"      {s['step_order']}. {s['step_type']} {s_tag}")


@playbook.command("generate")
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--step", "-s", "steps", multiple=True,
              help="步骤描述（可多次指定），格式: type[:detail]")
@click.option("--from-csv", "csv_path", default=None,
              type=click.Path(exists=True, dir_okay=False), help="从 CSV 模板生成")
@click.option("--from-last-run", is_flag=True, help="从最近一次剧本运行记录生成（无剧本记录时自动回退到最近操作）")
@click.option("--from-recent-ops", "from_recent_ops", is_flag=True,
              help="从最近真实操作记录生成（review/undo，即使没跑过剧本也能用）")
@click.option("--name", "-n", "playbook_name", default=None, help="剧本名称（保存到库时使用）")
@click.option("--operator", default="", help="操作人")
@click.option("--description", "-d", default="", help="剧本描述")
@click.option("--output-file", default="", help="剧本级输出文件路径（会自动追加 export 步骤）")
@click.option("--filter-status", "-f", default="",
              type=click.Choice(["", "all", "pending", "signed", "supplement", "failed_precheck"]),
              help="全局筛选状态（步骤级未指定时生效）")
@click.option("--line-range", default=None, help="全局行号范围 start-end（步骤级未指定时生效）")
@click.option("--remark-template", default="", help="全局备注模板（review 步骤级未指定时生效）")
@click.option("--save", "save_to_lib", is_flag=True, help="保存到剧本库")
@click.option("--overwrite", is_flag=True, help="覆盖同名剧本（配合 --save）")
@click.option("--output", "output_path", default=None, help="剧本 JSON 输出文件路径")
@click.pass_context
def playbook_generate(ctx, batch_no, steps, csv_path, from_last_run, from_recent_ops,
                      playbook_name, operator, description, output_file, filter_status,
                      line_range, remark_template, save_to_lib, overwrite, output_path):
    """生成剧本：从命令参数、CSV 模板、最近操作记录或最近剧本运行记录"""
    db_path = ensure_db(ctx)

    source_count = sum(1 for s in [steps, csv_path, from_last_run, from_recent_ops] if s)
    if source_count == 0:
        click.echo("错误: 必须指定至少一种生成来源 (--step / --from-csv / --from-last-run / --from-recent-ops)", err=True)
        sys.exit(1)
    if source_count > 1:
        click.echo("错误: 只能指定一种生成来源", err=True)
        sys.exit(1)

    try:
        if steps:
            pb = playbook_mod.generate_from_args(
                batch_no=batch_no,
                step_specs=list(steps),
                operator=operator,
                description=description,
                output_file=output_file,
                filter_status=filter_status,
                line_range=line_range,
                remark_template=remark_template,
                db_path=db_path,
            )
        elif csv_path:
            pb = playbook_mod.generate_from_csv(
                batch_no=batch_no,
                csv_path=os.path.abspath(csv_path),
                operator=operator,
                description=description,
                output_file=output_file,
                db_path=db_path,
            )
        elif from_recent_ops:
            pb = playbook_mod.generate_from_recent_ops(
                db_path=db_path,
                batch_no=batch_no,
                operator=operator,
                description=description,
                output_file=output_file,
            )
        elif from_last_run:
            pb = playbook_mod.generate_from_last_run(
                db_path=db_path,
                batch_no=batch_no,
                operator=operator,
                description=description,
                output_file=output_file,
                fallback_to_recent_ops=True,
            )
        else:
            click.echo("错误: 未指定生成来源", err=True)
            sys.exit(1)
    except playbook_mod.PlaybookError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except playbook_mod.PlaybookFormatError as e:
        click.echo(f"格式错误: {e}", err=True)
        sys.exit(1)

    batch = db.get_batch_by_no(db_path, batch_no)
    if batch:
        pb["batch_updated_at"] = batch["updated_at"]

    click.echo("=" * 60)
    click.echo(f"剧本已生成: 批次 '{batch_no}'")
    click.echo(f"  版本: {pb['version']}")

    src = pb.get("source", {})
    src_type = src.get("type", "")
    src_type_cn = {
        "command_args": "命令参数",
        "csv_template": "CSV 模板",
        "recent_operations": "最近真实操作",
        "last_playbook_run": "最近剧本运行",
    }.get(src_type, src_type or "未知")
    click.echo(f"  来源: {src_type_cn}")
    if src.get("timestamp"):
        click.echo(f"  来源时间: {format_time(src['timestamp'])}")

    if pb.get("description"):
        click.echo(f"  描述: {pb['description']}")
    if pb.get("operator"):
        click.echo(f"  操作人: {pb['operator']}")
    if pb.get("output_file"):
        click.echo(f"  导出文件名: {pb['output_file']}")

    gc = pb.get("global_context", {})
    if gc.get("filter_status"):
        click.echo(f"  固化筛选: {gc['filter_status']}")
    if gc.get("line_range"):
        click.echo(f"  固化行号范围: {gc['line_range'][0]}-{gc['line_range'][1]}")
    if gc.get("target_status"):
        click.echo(f"  固化目标状态: {gc['target_status']}")
    if gc.get("remark_template"):
        click.echo(f"  固化备注模板: {gc['remark_template']}")

    vs = pb.get("version_snapshot", {})
    if vs:
        rs = vs.get("review_stats", {})
        click.echo(f"  版本快照: {rs.get('signed', 0)}已签收/{rs.get('supplement', 0)}待补件/{rs.get('pending', 0)}待处理")
        if vs.get("manifest_path"):
            click.echo(f"  固化 manifest: {vs['manifest_path']}")

    if pb.get("batch_updated_at"):
        click.echo(f"  批次快照时间: {format_time(pb['batch_updated_at'])}")
    if pb.get("replayed_from_run_id"):
        click.echo(f"  重放来源: 运行 #{pb['replayed_from_run_id']} ({pb.get('replayed_from_status', '')})")
    click.echo(f"  步骤数: {len(pb['steps'])}")
    click.echo("")

    click.echo("步骤列表:")
    for step in pb["steps"]:
        desc = _describe_playbook_step(step)
        click.echo(f"  {step['order']}. {desc}")

    if save_to_lib and playbook_name:
        try:
            rid = playbook_mod.save_to_library(db_path, playbook_name, pb, overwrite=overwrite)
            click.echo(f"\n已保存到剧本库: {playbook_name} (ID: {rid})")
        except ValueError as e:
            click.echo(f"\n错误: {e}", err=True)
            sys.exit(1)

    if output_path:
        saved = playbook_mod.save_playbook(pb, output_path)
        click.echo(f"\n剧本文件已保存: {saved}")

    click.echo("=" * 60)
    click.echo("")
    click.echo("提示: 使用 'evi playbook check --name <名称>' 或 'evi playbook check -p <文件>' 检查冲突")
    click.echo("      使用 'evi playbook preview -p <剧本文件>' 预演命中条目")
    click.echo("      使用 'evi playbook execute -p <剧本文件>' 或 'evi playbook run --name <名称>' 执行")


@playbook.command("check")
@click.option("--name", "-n", "playbook_name", default=None, help="剧本库名称")
@click.option("--playbook-file", "-p", "playbook_path", default=None,
              type=click.Path(exists=True, dir_okay=False), help="剧本 JSON 文件路径")
@click.pass_context
def playbook_check(ctx, playbook_name, playbook_path):
    """检查剧本冲突：同名、批次修改、导出只读、版本、文件冲突"""
    db_path = ensure_db(ctx)

    if not playbook_name and not playbook_path:
        click.echo("错误: 必须指定 --name 或 --playbook-file", err=True)
        sys.exit(1)

    pb = None
    check_name = playbook_name

    if playbook_name:
        record = playbook_mod.load_from_library(db_path, playbook_name)
        if not record:
            click.echo(f"错误: 剧本库中不存在 '{playbook_name}'", err=True)
            sys.exit(1)
        pb = record["playbook_data"]
        if isinstance(pb, str):
            import json as _json
            try:
                pb = _json.loads(pb)
            except (_json.JSONDecodeError, TypeError):
                click.echo(f"错误: 剧本数据无法解析", err=True)
                sys.exit(1)
    elif playbook_path:
        try:
            pb = playbook_mod.load_playbook(os.path.abspath(playbook_path))
        except playbook_mod.PlaybookError as e:
            click.echo(f"错误: {e}", err=True)
            sys.exit(1)

    check_result = playbook_mod.check_playbook(db_path, pb, name=check_name)

    click.echo("=" * 60)
    click.echo(f"剧本检查: 批次 '{check_result['batch_no']}'")
    click.echo("=" * 60)

    if check_result["can_execute"]:
        click.echo("[OK] 可以执行")
    else:
        click.echo("[X] 无法执行")

    if not check_result.get("version_ok", True):
        click.echo(f"  [!] 版本冲突")

    if check_result.get("same_name_conflict"):
        click.echo(f"  [!] 同名剧本冲突")

    if check_result.get("batch_modified"):
        click.echo(f"  [!] 批次已被修改")
        if check_result.get("batch_change_details"):
            for cd in check_result["batch_change_details"]:
                click.echo(f"      - {cd}")

    if check_result.get("readonly_export_dir"):
        click.echo(f"  [!] 导出目录只读")

    if check_result.get("export_path_conflicts"):
        click.echo(f"  [!] 导出路径冲突")
        for epc in check_result["export_path_conflicts"]:
            click.echo(f"      - {epc}")

    if check_result.get("check_errors"):
        click.echo("")
        click.echo("阻断性错误:")
        for e in check_result["check_errors"]:
            click.echo(f"  [X] {e}")

    if check_result.get("check_warnings"):
        click.echo("")
        click.echo("告警:")
        for w in check_result["check_warnings"]:
            click.echo(f"  [!] {w}")

    if check_result["global_conflicts"]:
        click.echo("")
        click.echo("全局冲突:")
        for c in check_result["global_conflicts"]:
            click.echo(f"  {c}")

    click.echo("")
    for sp in check_result["steps"]:
        click.echo(f"--- 步骤 {sp['order']}: {sp['type']} ---")

        matched = sp["matched_items"]
        skipped = sp["skipped_reasons"]
        overwrites = sp["will_overwrite"]
        conflicts = sp["conflicts"]

        if matched:
            click.echo(f"  命中: {len(matched)} 项")
            for m in matched[:10]:
                fp = m.get("file_path", "")
                iid = m.get("id", "?")
                click.echo(f"    #{iid} {fp}")
                if m.get("current_status") and m.get("target_status"):
                    click.echo(f"      {m['current_status']} → {m['target_status']}")
                if m.get("current_precheck_status"):
                    click.echo(f"      预检: {m['current_precheck_status']}")
                if m.get("will_revert_to"):
                    click.echo(f"      {m.get('current_status', '?')} → {m['will_revert_to']}")
            if len(matched) > 10:
                click.echo(f"    ... 还有 {len(matched) - 10} 项")

        if skipped:
            click.echo(f"  跳过: {len(skipped)} 条")
            for s in skipped:
                click.echo(f"    - {s}")

        if overwrites:
            click.echo(f"  覆盖: {len(overwrites)} 条")
            for o in overwrites:
                click.echo(f"    - {o}")

        if conflicts:
            click.echo(f"  冲突: {len(conflicts)} 条")
            for c in conflicts:
                click.echo(f"    [!] {c}")

        click.echo("")

    click.echo("=" * 60)

    if not check_result["can_execute"]:
        sys.exit(1)


@playbook.command("list")
@click.option("--batch", "-b", "batch_no", default=None, help="按批次筛选")
@click.pass_context
def playbook_list(ctx, batch_no):
    """列出剧本库中的剧本"""
    db_path = ensure_db(ctx)

    items = playbook_mod.list_library(db_path, batch_no=batch_no)

    if not items:
        click.echo("剧本库为空")
        return

    click.echo(f"剧本库（共 {len(items)} 个）:")
    click.echo("")
    for item in items:
        click.echo(f"  {item['name']}")
        click.echo(f"    批次: {item['batch_no']}  版本: {item.get('version', '?')}")
        if item.get("description"):
            click.echo(f"    描述: {item['description']}")
        if item.get("operator"):
            click.echo(f"    操作人: {item['operator']}")
        click.echo(f"    创建: {format_time(item['created_at'])}  修改: {format_time(item['modified_at'])}")
        if item.get("last_run_status"):
            run_tag = item["last_run_status"]
            click.echo(f"    上次运行: {run_tag} (运行 #{item.get('last_run_id', '?')})")
        else:
            click.echo(f"    上次运行: (未运行)")


@playbook.command("show")
@click.option("--name", "-n", "playbook_name", required=True, help="剧本名称")
@click.pass_context
def playbook_show(ctx, playbook_name):
    """查看剧本库中剧本详情"""
    db_path = ensure_db(ctx)

    record = playbook_mod.load_from_library(db_path, playbook_name)
    if not record:
        click.echo(f"错误: 剧本 '{playbook_name}' 不存在", err=True)
        sys.exit(1)

    pb = record.get("playbook_data")
    if isinstance(pb, str):
        import json as _json
        try:
            pb = _json.loads(pb)
        except (_json.JSONDecodeError, TypeError):
            click.echo(f"错误: 剧本数据无法解析", err=True)
            sys.exit(1)

    click.echo("=" * 60)
    click.echo(f"剧本: {playbook_name}")
    click.echo("=" * 60)
    click.echo(f"  批次: {record['batch_no']}")
    click.echo(f"  版本: {record.get('version', '?')}")
    if record.get("description"):
        click.echo(f"  描述: {record['description']}")
    if record.get("operator"):
        click.echo(f"  操作人: {record['operator']}")
    if record.get("output_file"):
        click.echo(f"  输出文件: {record['output_file']}")
    click.echo(f"  创建: {format_time(record['created_at'])}")
    click.echo(f"  修改: {format_time(record['modified_at'])}")
    if record.get("last_run_status"):
        click.echo(f"  上次运行: {record['last_run_status']} (运行 #{record.get('last_run_id', '?')})")
    click.echo("")

    if pb:
        click.echo("步骤列表:")
        for step in pb.get("steps", []):
            desc = _describe_playbook_step(step)
            click.echo(f"  {step.get('order', '?')}. {desc}")

    click.echo("=" * 60)


@playbook.command("run")
@click.option("--name", "-n", "playbook_name", required=True, help="剧本库名称")
@click.option("--operator", "-o", default=None, help="覆盖剧本中的操作人")
@click.option("--force", "-f", is_flag=True,
              help="强制执行（跳过批次修改检测和输出文件冲突）")
@click.pass_context
def playbook_run(ctx, playbook_name, operator, force):
    """执行剧本库中的剧本"""
    db_path = ensure_db(ctx)

    record = playbook_mod.load_from_library(db_path, playbook_name)
    if not record:
        click.echo(f"错误: 剧本 '{playbook_name}' 不存在", err=True)
        sys.exit(1)

    pb = record.get("playbook_data")
    if isinstance(pb, str):
        import json as _json
        try:
            pb = _json.loads(pb)
        except (_json.JSONDecodeError, TypeError):
            click.echo(f"错误: 剧本数据无法解析", err=True)
            sys.exit(1)

    if not force:
        check_result = playbook_mod.check_playbook(db_path, pb)
        if not check_result["can_execute"]:
            if check_result.get("check_errors"):
                for e in check_result["check_errors"]:
                    click.echo(f"错误: {e}", err=True)
            if check_result.get("check_warnings"):
                for w in check_result["check_warnings"]:
                    click.echo(f"告警: {w}", err=True)
            click.echo("使用 --force 强制执行，或修正上述问题后重试", err=True)
            sys.exit(1)
        if check_result.get("check_warnings"):
            for w in check_result["check_warnings"]:
                click.echo(f"告警: {w}")

    try:
        result = playbook_mod.execute_playbook(
            db_path, pb, operator=operator, force=force,
            library_name=playbook_name,
        )
    except playbook_mod.PlaybookConflictError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except playbook_mod.PlaybookStepError as e:
        click.echo(f"执行失败: {e}", err=True)
        sys.exit(1)

    click.echo("=" * 60)
    click.echo(f"剧本执行: {playbook_name}  批次 '{result['batch_no']}'")
    click.echo(f"运行 ID: #{result['run_id']}")
    click.echo(f"状态: {result['status']}")
    click.echo("=" * 60)

    for sr in result["steps"]:
        status_tag = "[OK]" if sr["status"] == "success" else (
            "[SKIP]" if sr["status"] == "skipped" else "[FAIL]"
        )
        click.echo(f"  步骤 {sr['order']}: {sr['type']} {status_tag}")

        if sr["status"] == "success" and sr.get("affected_items"):
            for ai in sr["affected_items"]:
                if sr["type"] == "review":
                    click.echo(f"    #{ai.get('id', '?')} {ai.get('file_path', '')} "
                               f"{ai.get('old_status', '')} → {ai.get('new_status', '')}")
                elif sr["type"] == "precheck":
                    click.echo(f"    #{ai.get('id', '?')} {ai.get('file_path', '')} "
                               f"预检: {ai.get('precheck_status', '')}")
                elif sr["type"] == "undo":
                    click.echo(f"    #{ai.get('item_id', '?')} {ai.get('file_path', '')} "
                               f"{ai.get('reverted_from', '')} → {ai.get('reverted_to', '')}")
                elif sr["type"] == "export":
                    click.echo(f"    输出: {ai.get('output_path', '')} "
                               f"({ai.get('format', '')}, {ai.get('count', 0)} 条)")

        if sr["status"] == "failed" and sr.get("error"):
            click.echo(f"    错误: {sr['error']}")

        if sr["status"] == "skipped":
            click.echo("    (无匹配项，已跳过)")

    if result["status"] == "rolled_back":
        click.echo("")
        click.echo("[!] 剧本执行失败，已回滚前面成功的步骤")
        if result.get("error"):
            click.echo(f"    失败原因: {result['error']}")

    click.echo("=" * 60)

    if result["status"] != "completed":
        sys.exit(1)


@playbook.command("delete")
@click.option("--name", "-n", "playbook_name", required=True, help="剧本名称")
@click.pass_context
def playbook_delete(ctx, playbook_name):
    """删除剧本库中的剧本"""
    db_path = ensure_db(ctx)

    deleted = playbook_mod.delete_from_library(db_path, playbook_name)
    if deleted:
        click.echo(f"已删除剧本: {playbook_name}")
    else:
        click.echo(f"错误: 剧本 '{playbook_name}' 不存在", err=True)
        sys.exit(1)


@main.group()
@click.pass_context
def handoff(ctx):
    """批次交接包：打包导出、查看内容、导入恢复"""
    pass


@handoff.command("create")
@click.option("--batch", "-b", "batch_no", required=True, help="批次编号")
@click.option("--output", "-o", "output_path", default=None,
              help="输出包路径（默认保存到 .handoffs 目录）")
@click.option("--name", "-n", "handoff_name", default=None,
              help="包名称（保存到 .handoffs 目录时使用）")
@click.option("--operator", "-u", "op", default=None, help="操作人")
@click.pass_context
def handoff_create(ctx, batch_no, output_path, handoff_name, op):
    """创建批次交接包（含 manifest、预检/复核结果、操作日志、剧本记录、导出报告）"""
    db_path = ensure_db(ctx)
    work_dir = ctx.obj["work_dir"]

    if output_path is None:
        import time as _time
        if handoff_name is None:
            timestamp = datetime.datetime.fromtimestamp(_time.time()).strftime("%Y%m%d_%H%M%S")
            handoff_name = f"{batch_no}_{timestamp}"
        output_path = handoff_mod.get_handoff_path(work_dir, handoff_name)

    output_path = os.path.abspath(output_path)

    try:
        result = handoff_mod.create_handoff(
            db_path=db_path,
            work_dir=work_dir,
            batch_no=batch_no,
            output_path=output_path,
            operator=op,
        )
    except handoff_mod.HandoffNotFoundError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffMissingFilesError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffPermissionError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    pm = result["package_manifest"]
    click.echo("=" * 60)
    click.echo("交接包已创建")
    click.echo("=" * 60)
    click.echo(f"  路径: {result['output_path']}")
    click.echo(f"  版本: {pm.get('version', '?')}")
    click.echo(f"  批次: {pm['batch']['batch_no']}")
    if pm["batch"].get("description"):
        click.echo(f"  描述: {pm['batch']['description']}")
    click.echo(f"  证据项: {pm['batch'].get('item_count', '?')} 项")
    click.echo(f"  包含文件: {', '.join(pm.get('files', []))}")
    if pm.get("source", {}).get("operator"):
        click.echo(f"  操作人: {pm['source']['operator']}")
    click.echo(f"  创建时间: {format_time(pm.get('created_at', 0))}")
    click.echo("=" * 60)


@handoff.command("show")
@click.option("--package", "-p", "package_path", required=True,
              type=click.Path(dir_okay=False), help="交接包文件路径或名称")
@click.pass_context
def handoff_show(ctx, package_path):
    """查看交接包内容（只读，不解包到磁盘）"""
    work_dir = ctx.obj["work_dir"]

    if not os.path.isabs(package_path) and not os.path.exists(package_path):
        candidate = handoff_mod.get_handoff_path(work_dir, package_path)
        if os.path.exists(candidate):
            package_path = candidate

    package_path = os.path.abspath(package_path)

    try:
        info = handoff_mod.inspect_handoff(package_path)
    except handoff_mod.HandoffNotFoundError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffFormatError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffVersionError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffChecksumError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    pm = info["package_manifest"]
    snapshot = info["snapshot"]
    items = snapshot.get("items", [])
    logs = snapshot.get("review_logs", [])

    click.echo("=" * 60)
    click.echo(f"交接包内容: {os.path.basename(package_path)}")
    click.echo("=" * 60)
    click.echo(f"  路径: {package_path}")
    click.echo(f"  版本: {pm.get('version', '?')}")
    click.echo(f"  打包时间: {format_time(pm.get('created_at', 0))}")
    src = pm.get("source", {})
    if src.get("work_dir"):
        click.echo(f"  来源 work-dir: {src['work_dir']}")
    if src.get("operator"):
        click.echo(f"  打包人: {src['operator']}")
    click.echo("")

    click.echo(f"批次信息:")
    click.echo(f"  批次号: {pm['batch']['batch_no']}")
    if pm["batch"].get("description"):
        click.echo(f"  描述: {pm['batch']['description']}")
    click.echo(f"  原 manifest: {pm['batch']['manifest_path']}")
    click.echo(f"  原证据目录: {pm['batch']['evidence_dir']}")
    click.echo(f"  证据项数: {len(items)}")
    click.echo(f"  复核日志: {len(logs)} 条")
    click.echo("")

    total = len(items)
    passed = sum(1 for i in items if i.get("precheck_status") == "passed")
    failed = sum(1 for i in items if i.get("precheck_status") == "failed")
    unchecked = total - passed - failed
    signed = sum(1 for i in items if i.get("review_status") == "signed")
    supp = sum(1 for i in items if i.get("review_status") == "supplement")
    pend = total - signed - supp
    click.echo("预检统计:")
    click.echo(f"  总计: {total}  通过: {passed}  失败: {failed}  未检查: {unchecked}")
    click.echo("复核统计:")
    click.echo(f"  总计: {total}  已签收: {signed}  待补件: {supp}  待处理: {pend}")
    click.echo("")

    recent = info.get("recent_ops", [])
    click.echo(f"最近操作日志（包内保存 {len(recent)} 条）:")
    for log in recent[:5]:
        action = "撤销" if log.get("action") == "undo" else "复核"
        click.echo(f"  [{format_time(log.get('created_at', 0))}] {action} "
                   f"#{log.get('item_id', '?')} {log.get('file_path', '')}")
    if len(recent) > 5:
        click.echo(f"  ... 还有 {len(recent) - 5} 条")
    click.echo("")

    lp = info.get("last_playbook")
    if lp:
        lr = lp.get("last_run")
        lib = lp.get("playbook_library", [])
        if lr:
            click.echo(f"最近一次剧本运行: #{lr.get('id', '?')} 状态: {lr.get('status', '?')}")
            click.echo(f"  开始: {format_time(lr.get('started_at', 0))}")
            if lr.get("finished_at"):
                click.echo(f"  结束: {format_time(lr.get('finished_at', 0))}")
            if lr.get("operator"):
                click.echo(f"  操作人: {lr['operator']}")
            if lr.get("steps"):
                click.echo(f"  步骤数: {len(lr['steps'])}")
        if lib:
            click.echo(f"剧本库: {len(lib)} 个")
            for pb in lib[:3]:
                click.echo(f"  - {pb.get('name', '?')}")
            if len(lib) > 3:
                click.echo(f"  ... 还有 {len(lib) - 3} 个")
        click.echo("")

    er = info.get("export_report", {})
    if er:
        click.echo("导出报告:")
        click.echo(f"  生成时间: {format_time(er.get('generated_at', 0))}")
    click.echo("=" * 60)


@handoff.command("import")
@click.option("--package", "-p", "package_path", required=True,
              type=click.Path(dir_okay=False), help="交接包文件路径或名称")
@click.option("--force", "-f", is_flag=True, help="强制覆盖已存在的同名批次")
@click.option("--evidence-dir", "-e", "evidence_dir", default=None,
              type=click.Path(file_okay=False), help="重映射证据目录路径")
@click.option("--work-dir", "-w", "target_work_dir", default=None,
              type=click.Path(file_okay=False, dir_okay=True),
              help="目标工作目录（默认使用当前工作目录）")
@click.option("--dry-run", is_flag=True,
              help="只读核查（预演），不修改数据库和文件系统")
@click.option("--operator", "-u", "op", default=None, help="操作人")
@click.pass_context
def handoff_import(ctx, package_path, force, evidence_dir, target_work_dir, dry_run, op):
    """导入交接包：先只读核查，确认无误再正式恢复"""
    work_dir = ctx.obj["work_dir"]

    if not os.path.isabs(package_path) and not os.path.exists(package_path):
        candidate = handoff_mod.get_handoff_path(work_dir, package_path)
        if os.path.exists(candidate):
            package_path = candidate

    package_path = os.path.abspath(package_path)

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
        preview = handoff_mod.preview_import_handoff(
            db_path=target_db_path,
            work_dir=target_work_dir,
            package_path=package_path,
            force=force,
            evidence_dir=evidence_dir,
        )
    except handoff_mod.HandoffNotFoundError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffFormatError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffVersionError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffChecksumError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    click.echo("=" * 60)
    if dry_run:
        click.echo(f"交接包导入核查（只读预演）: {os.path.basename(package_path)}")
    else:
        click.echo(f"交接包导入核查: {os.path.basename(package_path)}")
    click.echo("=" * 60)

    pm = preview["package_manifest"]
    click.echo(f"包版本: {pm.get('version', '?')}")
    click.echo(f"批次: {preview['package_manifest']['batch']['batch_no']}")
    click.echo(f"目标 work-dir: {target_work_dir}")
    click.echo("")

    if not preview["work_dir_writable"]:
        click.echo(f"[X] 目标目录不可写: {preview.get('work_dir_error', '')}")
    else:
        click.echo("[OK] 目标目录可写")

    if preview["conflicts"]:
        for c in preview["conflicts"]:
            if "已存在，使用 --force" in c:
                click.echo(f"[X] {c}")
            elif "已存在（将被强制覆盖）" in c:
                click.echo(f"[!] {c}")
            else:
                click.echo(f"[!] {c}")
    else:
        click.echo("[OK] 无批次/剧本冲突")

    if preview["evidence_remapped"]:
        click.echo(f"[!] 证据目录将重映射到: {preview['evidence_dir']}")
    click.echo("")

    click.echo("批次统计:")
    click.echo(f"  证据项: {preview['item_count']} 项")
    pc = preview["precheck_stats"]
    rv = preview["review_stats"]
    click.echo(f"  预检: 通过 {pc['passed']} / 失败 {pc['failed']} / 未检查 {pc['unchecked']}")
    click.echo(f"  复核: 已签收 {rv['signed']} / 待补件 {rv['supplement']} / 待处理 {rv['pending']}")
    click.echo("")

    if preview.get("diff"):
        diff = preview["diff"]
        click.echo("-" * 60)
        click.echo("强制覆盖差异:")
        old_rv = diff["review_stats"]["old"]
        new_rv = diff["review_stats"]["new"]
        click.echo(f"  复核: 已签收 {old_rv['signed']}→{new_rv['signed']}  "
                   f"待补件 {old_rv['supplement']}→{new_rv['supplement']}")
        only_old = diff["items"].get("only_in_old", [])
        only_new = diff["items"].get("only_in_new", [])
        if only_old:
            click.echo(f"  仅旧批次: {len(only_old)} 项")
        if only_new:
            click.echo(f"  仅新批次: {len(only_new)} 项")
        click.echo("-" * 60)
        click.echo("")

    if preview["can_import"]:
        click.echo("[OK] 核查通过，可以导入")
    else:
        click.echo("[X] 核查未通过，无法导入")
    click.echo("=" * 60)

    if not preview["can_import"]:
        try:
            db.insert_handoff_import(
                db_path=target_db_path,
                package_path=preview["package_path"],
                package_version=preview["package_manifest"].get("version", handoff_mod.HANDOFF_VERSION),
                batch_no=preview["package_manifest"]["batch"]["batch_no"],
                source_summary=preview["package_manifest"].get("source", {}),
                import_log=preview.get("import_log", []),
                restore_result={"error": "预演检查失败", "conflicts": preview["conflicts"]},
                status="failed",
                operator=op,
                was_force=force,
                evidence_dir_remapped=evidence_dir if preview.get("evidence_remapped") else None,
            )
        except Exception:
            pass
        sys.exit(1)

    if dry_run:
        return

    try:
        result = handoff_mod.import_handoff(
            db_path=target_db_path,
            work_dir=target_work_dir,
            package_path=package_path,
            force=force,
            evidence_dir=evidence_dir,
            operator=op,
        )
    except handoff_mod.HandoffConflictError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)
    except handoff_mod.HandoffError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    click.echo("")
    click.echo("=" * 60)
    click.echo("导入完成")
    click.echo("=" * 60)
    click.echo(f"  批次: {result['batch_no']}")
    click.echo(f"  证据项: {result['item_count']} 项")
    click.echo(f"  manifest: {result['manifest_path']}")
    click.echo(f"  证据目录: {result['evidence_dir']}")
    if result.get("evidence_remapped"):
        click.echo(f"  证据目录已重映射")
    if result.get("imported_playbooks"):
        click.echo(f"  已导入剧本: {', '.join(result['imported_playbooks'])}")
    if result.get("skipped_playbooks"):
        click.echo(f"  跳过剧本: {', '.join(result['skipped_playbooks'])}")
    if op:
        click.echo(f"  操作人: {op}")
    click.echo("")
    click.echo("提示: 使用 'evi resume -b <批次号>' 查看恢复后状态")
    click.echo("      使用 'evi trace -b <批次号>' 查看完整恢复链路")
    click.echo("      使用 'evi list' 查看所有批次")
    click.echo("=" * 60)


@handoff.command("list")
@click.pass_context
def handoff_list(ctx):
    """列出工作目录中的交接包"""
    work_dir = ctx.obj["work_dir"]
    items = handoff_mod.list_handoffs(work_dir)
    if not items:
        click.echo("暂无交接包")
        return
    click.echo(f"共 {len(items)} 个交接包:")
    click.echo("")
    for it in items:
        size_str = _format_size(it.get("size", 0))
        click.echo(f"  {it['name']}")
        click.echo(f"    批次: {it.get('batch_no', '?')}  版本: {it.get('version', '?')}  "
                   f"大小: {size_str}  创建: {format_time(it.get('created_at', 0))}")


@handoff.command("log")
@click.option("--batch", "-b", "batch_no", default=None, help="按批次筛选")
@click.option("--limit", "-n", type=int, default=20, help="最多显示数量")
@click.pass_context
def handoff_log(ctx, batch_no, limit):
    """查看交接包导入记录（SQLite 持久化，跨进程可查）"""
    db_path = ensure_db(ctx)
    records = db.get_handoff_imports(db_path, batch_no=batch_no, limit=limit)
    if not records:
        click.echo("暂无导入记录")
        return
    click.echo(f"导入记录（共 {len(records)} 条）:")
    click.echo("")
    for r in records:
        status_tag = {
            "imported": "[OK]",
            "failed": "[FAIL]",
            "previewed": "[PREVIEW]",
        }.get(r["status"], "[?]")
        click.echo(f"  #{r['id']} {status_tag} {r['status']}  批次: {r['batch_no']}")
        click.echo(f"    包版本: {r['package_version']}  包: {os.path.basename(r['package_path'])}")
        click.echo(f"    时间: {format_time(r['imported_at'])}")
        if r.get("operator"):
            click.echo(f"    操作人: {r['operator']}")
        if r.get("was_force"):
            click.echo(f"    方式: 强制覆盖")
        if r.get("evidence_dir_remapped"):
            click.echo(f"    证据目录重映射: {r['evidence_dir_remapped']}")
        src = r.get("source_summary", {})
        if isinstance(src, dict) and src.get("work_dir"):
            click.echo(f"    来源 work-dir: {src['work_dir']}")
        rr = r.get("restore_result", {})
        if isinstance(rr, dict):
            if rr.get("error"):
                click.echo(f"    错误: {rr['error']}")
            if rr.get("imported_playbooks"):
                click.echo(f"    导入剧本: {', '.join(rr['imported_playbooks'])}")
            if rr.get("skipped_playbooks"):
                click.echo(f"    跳过剧本: {', '.join(rr['skipped_playbooks'])}")
        il = r.get("import_log", [])
        if isinstance(il, list) and il:
            click.echo(f"    导入步骤: {len(il)} 步")
            for step in il:
                if step.get("ok"):
                    tag = "[OK]"
                else:
                    tag = "[X]"
                click.echo(f"      {tag} {step.get('step', '')}")
        click.echo("")


if __name__ == "__main__":
    main()
