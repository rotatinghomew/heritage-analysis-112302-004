"""CSV 读取与证据解析。

空值表示“来源没有给值”（MISSING），显式 ``unknown``/``未知`` 表示“来源明确
不知道”（UNKNOWN），``0`` 单独记为 ZERO。三者不会混为一谈。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from .model import (
    MISSING,
    UNKNOWN,
    Bundle,
    DataIssue,
    Evidence,
    EvidenceState,
    Finding,
    ProgressRecord,
    Protocol,
    SiteSpace,
    TeamWindow,
    Transfer,
)

PRIMARY_FILE = "primary.csv"
FINDINGS_FILE = "findings.csv"
PROTOCOL_FILE = "protocol.csv"
SPACES_FILE = "spaces.csv"
TRANSFERS_FILE = "transfers.csv"

_UNKNOWN_TOKENS = {"unknown", "未知", "?", "n/a"}
_MISSING_TOKENS = {"missing", "缺报", "none", "null", "-"}

_TRUE = {"1", "true", "yes", "y", "是", "t"}
_FALSE = {"0", "false", "no", "n", "否", "f"}

REQUIRED_PRIMARY = (
    "object",
    "site",
    "step",
    "event",
    "deadline",
    "inspection",
    "elapsed",
    "status",
)


def _clean(row: Mapping[str, str], key: str) -> str:
    return (row.get(key) or "").strip()


def parse_evidence(raw: str | None, *, blank: EvidenceState = EvidenceState.MISSING) -> Evidence:
    if raw is None:
        return Evidence(blank, None)
    text = raw.strip()
    if not text:
        return Evidence(blank, text or None)
    lowered = text.lower()
    if lowered in _MISSING_TOKENS:
        return Evidence(EvidenceState.MISSING, text)
    if lowered in _UNKNOWN_TOKENS:
        return Evidence(EvidenceState.UNKNOWN, text)
    if lowered == "0":
        return Evidence(EvidenceState.ZERO, text, 0)
    return Evidence(EvidenceState.PRESENT, text, text)


def parse_date(raw: str | None) -> Evidence:
    text = (raw or "").strip()
    if not text:
        return MISSING
    lowered = text.lower()
    if lowered in _MISSING_TOKENS:
        return Evidence(EvidenceState.MISSING, text)
    if lowered in _UNKNOWN_TOKENS:
        return Evidence(EvidenceState.UNKNOWN, text)
    try:
        return Evidence(EvidenceState.PRESENT, text, date.fromisoformat(text))
    except ValueError:
        return Evidence(EvidenceState.UNKNOWN, text)


def parse_int(raw: str | None) -> Evidence:
    text = (raw or "").strip()
    if not text:
        return MISSING
    lowered = text.lower()
    if lowered in _MISSING_TOKENS:
        return Evidence(EvidenceState.MISSING, text)
    if lowered in _UNKNOWN_TOKENS:
        return Evidence(EvidenceState.UNKNOWN, text)
    try:
        value = int(text)
    except ValueError:
        return Evidence(EvidenceState.UNKNOWN, text)
    if value == 0:
        return Evidence(EvidenceState.ZERO, text, 0)
    return Evidence(EvidenceState.PRESENT, text, value)


def parse_bool(raw: str | None) -> Evidence:
    text = (raw or "").strip()
    if not text:
        return MISSING
    lowered = text.lower()
    if lowered in _MISSING_TOKENS:
        return Evidence(EvidenceState.MISSING, text)
    if lowered in _UNKNOWN_TOKENS:
        return Evidence(EvidenceState.UNKNOWN, text)
    if lowered in _TRUE:
        return Evidence(EvidenceState.PRESENT, text, True)
    if lowered in _FALSE:
        return Evidence(EvidenceState.PRESENT, text, False)
    return Evidence(EvidenceState.UNKNOWN, text)


def _read_rows(path: Path, issues: list[DataIssue]) -> tuple[list[str], list[dict[str, str]]]:
    import csv

    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            header = [h.strip() for h in (reader.fieldnames or [])]
            rows = []
            for index, row in enumerate(reader, start=2):
                if not any((value or "").strip() for value in row.values() if isinstance(value, str)):
                    continue
                clean = {}
                for key, value in row.items():
                    if key is None:
                        # 多余的列：csv 会把它塞进 None 键的列表里
                        clean["_extra"] = "|".join(value or [])
                        continue
                    clean[key.strip()] = (value or "").strip()
                clean["_row"] = str(index)
                rows.append(clean)
    except FileNotFoundError:
        issues.append(DataIssue("file_missing", f"缺少文件 {path.name}", severity="info"))
        return [], []
    return header, rows


def _row_number(row: Mapping[str, str]) -> int:
    # 第二遍覆盖时保留源文件行号
    try:
        return int(row["_row"])
    except (KeyError, ValueError):
        return 0


def _optional(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _load_primary(path: Path, bundle: Bundle) -> None:
    header, rows = _read_rows(path, bundle.issues)
    if not rows:
        return
    missing = [c for c in REQUIRED_PRIMARY if c not in header]
    if missing:
        bundle.issues.append(
            DataIssue("schema_missing_columns", f"primary 缺少字段: {', '.join(missing)}", severity="block")
        )
    for row in rows:
        row_no = _row_number(row)
        object_id = _clean(row, "object")
        site = _clean(row, "site")
        step = _clean(row, "step")
        if not object_id or not step:
            bundle.issues.append(
                DataIssue("row_identity", "缺少 object 或 step, 该行无法归入护理链", row=row_no)
            )
            continue
        depends_raw = _clean(row, "depends_on") or _clean(row, "depends")
        depends = tuple(part.strip() for part in depends_raw.replace(";", ",").replace("|", ",").split(",") if part.strip())
        bundle.progress.append(
            ProgressRecord(
                object_id=object_id,
                site=site,
                step=step,
                event=parse_date(row.get("event")),
                deadline=parse_date(row.get("deadline")),
                inspection=parse_evidence(row.get("inspection")),
                elapsed=parse_int(row.get("elapsed")),
                status=parse_evidence(row.get("status")),
                receipt=parse_bool(row.get("receipt")) if "receipt" in header else None,
                depends_on=depends,
                material=_optional(row.get("material")),
                treatment_type=_optional(row.get("treatment_type") or row.get("treatment")),
                batch=_optional(row.get("batch")),
                owner=_optional(row.get("owner")),
                source_row=row_no,
                extra={k: v for k, v in row.items() if k not in {*REQUIRED_PRIMARY, "_row"}},
            )
        )


def _load_findings(path: Path, bundle: Bundle) -> None:
    header, rows = _read_rows(path, bundle.issues)
    if not rows:
        return
    for row in rows:
        effect = (_clean(row, "effect") or "none").lower()
        days = parse_int(row.get("days"))
        limit = parse_int(row.get("limit_days"))
        recheck = parse_bool(row.get("recheck")) if "recheck" in header else MISSING
        confirmed = parse_bool(row.get("confirmed")) if "confirmed" in header else None
        bundle.findings.append(
            Finding(
                inspection=_optional(row.get("inspection")),
                object_id=_optional(row.get("object") or row.get("object_id")),
                step=_optional(row.get("step")),
                effect=effect,
                days=int(days.value) if days.known and days.value is not None else None,
                limit_days=int(limit.value) if limit.known and limit.value is not None else None,
                recheck=recheck.known and bool(recheck.value),
                confirmed=confirmed,
                finding_date=parse_date(row.get("finding_date") or row.get("date")),
                note=_optional(row.get("note")),
                source_row=_row_number(row),
            )
        )


def _load_teams(path: Path, bundle: Bundle) -> None:
    header, rows = _read_rows(path, bundle.issues)
    for row in rows:
        start = parse_date(row.get("start"))
        end = parse_date(row.get("end"))
        team = _clean(row, "team")
        if not team or not start.known or not end.known:
            bundle.issues.append(
                DataIssue("team_window_invalid", "班组记录缺 team/start/end", row=_row_number(row))
            )
            continue
        if start.value > end.value:
            bundle.issues.append(
                DataIssue("team_window_invalid", f"班组 {team} 的 start 晚于 end", row=_row_number(row))
            )
            continue
        capacity = parse_int(row.get("capacity"))
        bundle.teams.append(
            TeamWindow(
                team=team,
                site=_clean(row, "site"),
                skill=_clean(row, "skill"),
                start=start.value,
                end=end.value,
                capacity=int(capacity.value) if capacity.known and capacity.value is not None else 1,
                source_row=_row_number(row),
            )
        )


def _load_spaces(path: Path, bundle: Bundle) -> None:
    _header, rows = _read_rows(path, bundle.issues)
    for row in rows:
        start = parse_date(row.get("available_from"))
        end = parse_date(row.get("available_to"))
        site = _clean(row, "site")
        if not site or not start.known or not end.known:
            bundle.issues.append(
                DataIssue("space_window_invalid", "场地记录缺 site/available_from/available_to", row=_row_number(row))
            )
            continue
        capacity = parse_int(row.get("capacity"))
        bundle.spaces.append(
            SiteSpace(
                site=site,
                available_from=start.value,
                available_to=end.value,
                capacity=int(capacity.value) if capacity.known and capacity.value is not None else 1,
                note=_optional(row.get("note")),
                source_row=_row_number(row),
            )
        )


def _load_transfers(path: Path, bundle: Bundle) -> None:
    _header, rows = _read_rows(path, bundle.issues)
    for row in rows:
        object_id = _clean(row, "object") or _clean(row, "object_id")
        state = (_clean(row, "state") or "planned").lower()
        if not object_id:
            bundle.issues.append(DataIssue("transfer_invalid", "交接记录缺 object", row=_row_number(row)))
            continue
        urgent = parse_bool(row.get("urgent"))
        bundle.transfers.append(
            Transfer(
                object_id=object_id,
                from_site=_optional(row.get("from_site")),
                to_site=_optional(row.get("to_site")),
                state=state,
                urgent=urgent.known and bool(urgent.value),
                origin_confirmed=parse_bool(row.get("origin_confirmed")) if "origin_confirmed" in row else None,
                receiving_confirmed=parse_bool(row.get("receiving_confirmed")) if "receiving_confirmed" in row else None,
                returned_on=parse_date(row.get("returned_on")),
                note=_optional(row.get("note")),
                source_row=_row_number(row),
            )
        )


def _load_protocol(path: Path, issues: list[DataIssue]) -> tuple[Protocol, bool]:
    _header, rows = _read_rows(path, issues)
    if not rows:
        return Protocol(), False
    return Protocol.from_rows(rows), True


def load_bundle(
    root: str | Path,
    *,
    primary: str | Path | None = None,
    findings: str | Path | None = None,
    teams: str | Path | Iterable[str | Path] | None = None,
    spaces: str | Path | None = None,
    transfers: str | Path | None = None,
    protocol: str | Path | None = None,
) -> Bundle:
    """从目录或显式文件读取一个分析包。

    目录模式下自动识别 ``primary.csv``、``findings.csv``、``support-*.csv``、
    ``spaces.csv``、``transfers.csv``、``protocol.csv``。``support-*.csv`` 是
    班组能力表的兼容命名（对应仓库中的 ``support-1.csv``）。
    """

    base = Path(root)
    bundle = Bundle()
    if primary is None:
        primary = base / PRIMARY_FILE
    if findings is None:
        findings = base / FINDINGS_FILE
    if spaces is None:
        spaces = base / SPACES_FILE
    if transfers is None:
        transfers = base / TRANSFERS_FILE
    if protocol is None:
        protocol = base / PROTOCOL_FILE
    if teams is None:
        teams = sorted(base.glob("support-*.csv"))

    primary_path = Path(primary)
    _load_primary(primary_path, bundle)
    if primary_path.exists():
        bundle.sources["primary"] = str(primary_path)

    findings_path = Path(findings)
    if findings_path.exists():
        _load_findings(findings_path, bundle)
        bundle.sources["findings"] = str(findings_path)

    team_paths: list[Path]
    if isinstance(teams, (str, Path)):
        team_paths = [Path(teams)]
    else:
        team_paths = [Path(p) for p in teams]
    loaded_teams = False
    for path in team_paths:
        if path.exists():
            _load_teams(path, bundle)
            loaded_teams = True
            bundle.sources[f"teams:{path.name}"] = str(path)
    if not loaded_teams:
        bundle.issues.append(
            DataIssue("teams_missing", "未找到班组能力文件 (support-*.csv 或 teams.csv)", severity="info")
        )

    spaces_path = Path(spaces)
    if spaces_path.exists():
        _load_spaces(spaces_path, bundle)
        bundle.sources["spaces"] = str(spaces_path)
    transfers_path = Path(transfers)
    if transfers_path.exists():
        _load_transfers(transfers_path, bundle)
        bundle.sources["transfers"] = str(transfers_path)

    protocol_path = Path(protocol)
    parsed, loaded_protocol = _load_protocol(protocol_path, bundle.issues)
    bundle.protocol = parsed
    if loaded_protocol:
        bundle.sources["protocol"] = str(protocol_path)

    _validate(bundle)
    return bundle


def _validate(bundle: Bundle) -> None:
    seen: dict[tuple[str, str], int] = {}
    object_sites: dict[str, set[str]] = {}
    chains: dict[str, set[str]] = {}
    for record in bundle.progress:
        key = (record.object_id, record.step)
        if key in seen:
            bundle.issues.append(
                DataIssue(
                    "duplicate_step",
                    f"{record.object_id} 的 {record.step} 出现多次; 按交接合并而不是复制成两件藏品",
                    object_id=record.object_id,
                    site=record.site,
                    step=record.step,
                    row=record.source_row,
                )
            )
        seen[key] = record.source_row
        object_sites.setdefault(record.object_id, set()).add(record.site)
        chains.setdefault(record.object_id, set()).add(record.step)
    for object_id, sites in object_sites.items():
        if len(sites) > 1:
            bundle.issues.append(
                DataIssue(
                    "multi_site_chain",
                    f"{object_id} 的护理链跨 {', '.join(sorted(sites))}; 交接不产生新的待交付对象",
                    object_id=object_id,
                )
            )
    known_inspections = {
        r.inspection.value for r in bundle.progress if r.inspection.known and r.inspection.value
    }
    for finding in bundle.findings:
        if finding.inspection and known_inspections and finding.inspection not in known_inspections:
            bundle.issues.append(
                DataIssue(
                    "orphan_finding",
                    f"检查发现 {finding.inspection} 未关联到任何进度记录",
                    object_id=finding.object_id,
                    step=finding.step,
                    row=finding.source_row,
                )
            )
    for record in bundle.progress:
        for dep in record.depends_on:
            if dep not in chains.get(record.object_id, set()):
                bundle.issues.append(
                    DataIssue(
                        "unknown_dependency",
                        f"{record.object_id} 的 {record.step} 依赖不存在的步骤 {dep}",
                        object_id=record.object_id,
                        step=record.step,
                        row=record.source_row,
                    )
                )
    for transfer in bundle.transfers:
        if transfer.object_id not in object_sites:
            bundle.issues.append(
                DataIssue(
                    "orphan_transfer",
                    f"交接记录指向未出现在 primary 中的藏品 {transfer.object_id}",
                    object_id=transfer.object_id,
                    row=transfer.source_row,
                )
            )
        if transfer.urgent and transfer.state in {"planned", "in_transit"} and not transfer.dual_confirmed:
            bundle.issues.append(
                DataIssue(
                    "urgent_transfer_unconfirmed",
                    f"{transfer.object_id} 为紧急转馆, 需原馆与接收馆共同确认",
                    object_id=transfer.object_id,
                    severity="block",
                )
            )
