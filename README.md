# 馆际轮护的高风险工序缺口与交接

本地数据与术语材料供业务人员整理分析资料使用，所有记录均为合成样例。

这套工具把“各馆只报自己那一步是否准时”的口径，改成按**藏品的实际护理链**和
**截止/静置条件**计算，并回答四个问题：

1. 每件藏品现在卡在哪：处置完成了吗？完整交付了吗？缺的是证据还是时间？
2. 接下来两周哪些积压能靠兼容班组消掉，哪些要馆方先确认，哪些增派班组也来不及？
3. 改派是否守住了批次、在途护理链和支援后的复检窗口？
4. 交接班时能否复原各馆原来看到的交付缺口，并看清专项支援改变了哪些逐件结果？

## 目录

```
careflow/            分析包（Python 3.11 标准库）
  model.py           领域模型、证据状态、规程参数
  io.py              CSV 读取与结构校验
  engine.py          逐件评估：护理链、截止、静置窗口、风险
  planning.py        两周支援方案比较
  handover.py        交接快照、回放校验、前后差异
  report.py          值班员可读的文字视图
examples/weekly/     更完整的合成样例（含检查发现、场地、交接）
fixtures/            最小样例
docs/                字段与业务规程
tests/               结构与业务规则测试
```

## 环境与测试

Python 3.11 标准库，无需第三方依赖。

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q .
```

## 使用

```bash
# 逐件评估 + 按馆/材料/处理类型/批次聚合
python3 -m careflow assess examples/weekly --as-of 2026-05-08
python3 -m careflow assess examples/weekly --as-of 2026-05-08 --format text
python3 -m careflow assess examples/weekly --as-of 2026-05-08 --dimension site

# 比较未来两周的支援方案
python3 -m careflow plan examples/weekly --as-of 2026-05-08 --format text --assignments

# 交接：按馆导出快照、回放校验、对比前后逐件结果
python3 -m careflow handover examples/weekly --as-of 2026-05-08 --out-dir out
python3 -m careflow snapshot examples/weekly --as-of 2026-05-08 --scope A --out out/A.json
python3 -m careflow verify examples/weekly --as-of 2026-05-08 --snapshot out/A.json
python3 -m careflow diff out/before.json out/after.json --plan plan.json --format text
```

默认输出 JSON，`--format text` 输出可直接贴进值班记录的文字版。

## 计算口径

| 业务要求 | 实现 |
|---|---|
| 按藏品实际护理链计算 | 以 `object` 合并所有工序；同一藏品换馆交接不会变成两件待交付对象（重复工序合并并给 `merged_handover` 提示） |
| 同一检查可缩短时限，也可只要求附加复检 | `findings.csv` 的 `effect`：`shorten` 收紧截止，`add_recheck` 在没有复检工序时补一个待完成复检，`both` 两者都做 |
| 处置实际完成才重置阶段时限 | `effect=reset` 仅在处置状态为 `done` 时把截止重置为完成日 + `days` |
| 静置下限与复检上限 | 从处置完成日起算：`quiet_min_days` ≤ 复检日期 ≤ `recheck_max_days`；窗口不存在或已过即“已确认错过” |
| 处置完成与完整交付分开 | `treatment_complete` 只看处置；`full_delivery` 要求护理链全部完成、无证据缺口、无已确认错过 |
| 按馆、材料、处理类型汇总 | `assess --dimension`；每个分组都有处置完成、完整交付、准时交付、逾期完成与各风险档计数 |
| 证据缺失 vs 已确定错过 | 缺报/未知/缺回执记为 `evidence_gap`，不判逾期；只有截止或安全窗口确实已过才记为 `confirmed_missed` |
| 两周支援方案比较 | `plan` 比较各班组、全部班组与不投入支援，并跑一个无限产能压力方案作对照 |
| 批次不拆 | 同 `batch` 的藏品作为不可拆单元；任一成员未就绪时整批转“需馆方确认” |
| 在途护理链锁定 | `transfers.csv` 中 `in_transit`/`received` 的藏品不可改派；`returned` 后恢复可排 |
| 已腾出的场地 | `spaces.csv` 的可用窗口与容量参与排程，场地不足会推迟或降低方案 |
| 支援后仍有复检窗口 | 排程先定处置日，再在静置窗口内找复检班组；找不到就不算“可支援” |
| 已确认错过优先占用合格班组 | 记为“减损支援”（`mitigation_assigned`），**不计入通过件数** |
| 复原各馆看到的缺口 | `handover`/`snapshot` 保存逐件缺口与分组聚合；`verify` 以零容忍重算比对 |
| 查出支援改变了哪些逐件结果 | `diff` 输出逐件 `risk`、交付缺口、截止原因的新增与消除 |

## 字段

见 `docs/数据说明.md`。必填只有 `primary.csv` 的 `object,site,step,event,deadline,inspection,elapsed,status`；
其余文件与列都是可选的，缺失会在输出 `issues` 里说明，而不是静默当成零。

## 已知边界

- 排程是启发式的（按风险顺序逐单元贪心，同一批次绑定同一班组），不保证全局最优。
- 支援只覆盖处置与复检；回库等馆内工序不排班，但会计入交付缺口。
- 日期按自然日计算，`start`/`end` 均为闭区间。
