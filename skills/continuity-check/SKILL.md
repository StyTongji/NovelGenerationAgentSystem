# Skill: continuity-check

## Purpose

实现一致性验证、规则引擎、continuity prompt、回归测试时使用。

## Rule engine first

能确定性检查的不要只靠 LLM：

### Deterministic
- alive/dead
- current owner
- learned ability
- realm rank
- hard constraints
- chronology
- explicit location/travel rules
- plot thread state enum

### LLM semantic check
- subtle knowledge leak
- personality drift
- motivation inconsistency
- awkward retcon
- hidden implication contradiction

## Severity

- info: 风格/建议
- warning: 可能不合理但允许
- error: 明确连续性问题，应修
- critical: 违反 canonical truth 或 hard constraint，禁止 finalize

## Report shape

每个 issue 必须包括：
- type
- severity
- evidence_text
- canonical_fact
- explanation
- suggested_fix
- affected_scene

## Never auto-fix canonical truth

Continuity Checker 可以改正文建议，不能为了让正文成立而修改数据库事实。
