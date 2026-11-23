r"""命令行入口。

用法示例::

    python3 -m careflow assess fixtures --as-of 2026-05-08
    python3 -m careflow assess fixtures --as-of 2026-05-08 --format text
    python3 -m careflow plan fixtures --as-of 2026-05-08 --format text
    python3 -m careflow snapshot fixtures --as-of 2026-05-08 --scope A --out snap.json
    python3 -m careflow verify fixtures --snapshot snap.json --as-of 2026-05-08
    python3 -m careflow handover fixtures --as-of 2026-05-08 --out-dir out
    python3 -m careflow diff old.json new.json --plan plan.json --format text
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .engine import assess
from .handover import (
    build_snapshot,
    diff_snapshots,
    load_snapshot,
    save_snapshot,
    verify_snapshot,
)
from .io import load_bundle
from .planning import baseline_plan, compare_plans
from .report import (
    format_aggregates,
    format_assessment,
    format_diff,
    format_handover,
    format_plan,
)


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期需为 YYYY-MM-DD: {text}") from exc


def _dump(data, path: str | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _assessment(args):
    bundle = load_bundle(args.root, teams=args.teams or None)
    return bundle, assess(bundle.progress, bundle.findings, bundle.protocol, args.as_of, bundle.transfers)


def cmd_assess(args) -> int:
    bundle, assessment = _assessment(args)
    if args.dimension == "all":
        if args.format == "text":
            labels = {"site": "按馆", "material": "按材料", "treatment": "按处理类型", "batch": "按批次"}
            print("".join(f"{labels[dim]}\n" + format_aggregates(assessment, dim) + "\n"
                          for dim in ("site", "material", "treatment", "batch")))
        else:
            _dump(assessment.aggregates_dict(), args.out)
        return 0
    if args.dimension:
        if args.format == "text":
            print(format_aggregates(assessment, args.dimension))
        else:
            _dump(
                {
                    "as_of": assessment.as_of.isoformat(),
                    "dimension": args.dimension,
                    "groups": {k: v.to_dict() for k, v in assessment.aggregate(args.dimension).items()},
                },
                args.out,
            )
        return 0
    if args.format == "text":
        text = format_assessment(assessment, limit=args.limit)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        _dump(assessment.to_dict(), args.out)
    return 0


def cmd_plan(args) -> int:
    bundle, assessment = _assessment(args)
    data = compare_plans(assessment, bundle, start=args.as_of)
    if args.baseline:
        data["baseline"] = baseline_plan(assessment, bundle, start=args.as_of).to_dict()
    if args.format == "text":
        text = format_plan(data, show_assignments=args.assignments)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        _dump(data, args.out)
    return 0


def cmd_snapshot(args) -> int:
    _, assessment = _assessment(args)
    record = build_snapshot(assessment, scope=args.scope, generated_at=args.as_of)
    if args.format == "text":
        text = format_handover(record)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0
    if args.out:
        path = save_snapshot(record, args.out)
        print(f"已写入 {path}")
    else:
        _dump(record.to_dict(), None)
    return 0


def cmd_verify(args) -> int:
    _, assessment = _assessment(args)
    problems = verify_snapshot(assessment, load_snapshot(args.snapshot))
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print("快照与现场重算一致")
    return 0


def cmd_diff(args) -> int:
    before = load_snapshot(args.before)
    after = load_snapshot(args.after)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8")) if args.plan else None
    data = diff_snapshots(before, after, plan=plan)
    if args.format == "text":
        text = format_diff(data)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        _dump(data, args.out)
    return 0


def cmd_handover(args) -> int:
    """把每个馆各自看到的交付缺口导出成独立快照，便于交接班核对。"""

    _, assessment = _assessment(args)
    scopes: list[str | None]
    if args.scope:
        scopes = list(args.scope)
    else:
        sites = sorted({site for obj in assessment.objects for site in obj.sites if site})
        scopes = [None, *sites]
    out_dir = Path(args.out_dir)
    written: list[str] = []
    for scope in scopes:
        record = build_snapshot(assessment, scope=scope or "all", generated_at=args.as_of)
        name = f"handover-{scope or 'all'}.json"
        save_snapshot(record, out_dir / name)
        written.append(str(out_dir / name))
        if args.format == "text":
            print(format_handover(record))
    for path in written:
        print(path, file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="careflow",
        description="馆际轮护的高风险工序缺口与交接分析",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("root", nargs="?", default="fixtures", help="输入目录（默认 fixtures）")
    common.add_argument("--as-of", type=_date, default=date.today(), help="分析基准日 YYYY-MM-DD")
    common.add_argument("--teams", action="append", default=None, help="班组能力表，可重复")
    common.add_argument("--out", default=None, help="写入文件而不是打印")
    common.add_argument("--format", choices=["json", "text"], default="json", help="输出格式（默认 json）")

    p = sub.add_parser("assess", parents=[common], help="输出逐件评估与按维度聚合")
    p.add_argument(
        "--dimension",
        choices=["site", "material", "treatment", "batch", "all"],
        default=None,
        help="只输出某个维度的聚合",
    )
    p.add_argument("--limit", type=int, default=None, help="文字模式下只列前 N 件")
    p.set_defaults(func=cmd_assess)

    p = sub.add_parser("plan", parents=[common], help="比较未来两周的支援方案")
    p.add_argument("--baseline", action="store_true", help="附带不投入支援的基线")
    p.add_argument("--assignments", action="store_true", help="文字模式下展开推荐方案排程")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("snapshot", parents=[common], help="生成交接快照")
    p.add_argument("--scope", default="all", help="只保留某馆（默认全部）")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("verify", parents=[common], help="校验快照能否复原")
    p.add_argument("--snapshot", required=True, help="快照 JSON")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("diff", help="对比两个交接快照的逐件结果与截止原因")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--plan", default=None, help="支援方案 JSON，用于标注处置类型")
    p.add_argument("--out", default=None, help="写入文件而不是打印")
    p.add_argument("--format", choices=["json", "text"], default="json", help="输出格式（默认 json）")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("handover", parents=[common], help="按馆导出各自看到的交付缺口快照")
    p.add_argument(
        "--scope", action="append", default=None,
        help="只导出指定馆，可重复；不给则导出全部与每个馆",
    )
    p.add_argument("--out-dir", default="handover", help="输出目录（默认 handover）")
    p.set_defaults(func=cmd_handover)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
