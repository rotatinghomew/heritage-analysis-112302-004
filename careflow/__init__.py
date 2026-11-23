"""馆际轮护的高风险工序缺口与交接分析。

本包只使用 Python 标准库。核心概念：

* ``Evidence`` 区分“已知”“显式为零”“缺报”“未知”；
* ``ObjectAssessment`` 以藏品为唯一对象，跨馆交接不会把同一藏品拆成两件；
* ``plan`` 在两周窗口内比较支援方案，并检查批次、在途、静置与交还约束；
* ``handover`` 保存/复原各馆原本看到的交付缺口，并对比专项支援前后的逐件结果。
"""

from .model import (  # noqa: F401
    Aggregate,
    Assessment,
    Assignment,
    Bundle,
    DataIssue,
    Disposition,
    Evidence,
    EvidenceState,
    Finding,
    HandoverRecord,
    ObjectAssessment,
    Outcome,
    Plan,
    ProgressRecord,
    Protocol,
    Reason,
    RecheckWindow,
    Risk,
    SiteSpace,
    StepAssessment,
    StepState,
    TeamWindow,
    Transfer,
)
from .engine import assess  # noqa: F401
from .io import load_bundle  # noqa: F401
from .planning import baseline_plan, compare_plans, plan_support  # noqa: F401
from .handover import (  # noqa: F401
    build_snapshot,
    diff_assessments,
    load_snapshot,
    save_snapshot,
    verify_snapshot,
)

__all__ = [
    "Aggregate",
    "Assessment",
    "Assignment",
    "Bundle",
    "DataIssue",
    "Disposition",
    "Evidence",
    "EvidenceState",
    "Finding",
    "HandoverRecord",
    "ObjectAssessment",
    "Outcome",
    "Plan",
    "ProgressRecord",
    "Protocol",
    "Reason",
    "RecheckWindow",
    "Risk",
    "SiteSpace",
    "StepAssessment",
    "StepState",
    "TeamWindow",
    "Transfer",
    "assess",
    "load_bundle",
    "baseline_plan",
    "plan_support",
    "compare_plans",
    "build_snapshot",
    "verify_snapshot",
    "save_snapshot",
    "load_snapshot",
    "diff_assessments",
]
