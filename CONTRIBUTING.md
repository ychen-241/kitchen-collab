# 贡献指南

感谢关注 kitchen-collab！这是一个演示「AI 辅助后厨调度价值边界」的原型项目，
欢迎 Issue 与 Pull Request。

## 开发环境

```bash
git clone <repo-url> && cd kitchen-collab
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 本地验证（提交前必须全绿）

```bash
export PYTHONPATH=src        # Windows: set PYTHONPATH=src
python -m unittest discover -s tests -v
python examples/demo.py
```

## 提交规范

- 一个 PR 只做一件事；提交信息用祈使句，如 `fix: 修复催菜重排的重复状态迁移`
- 新功能必须带测试；改核心逻辑必须同步更新 `docs/design.md`
- 变更影响用户可见行为时，在 `CHANGELOG.md` 的 `[Unreleased]` 下补一行

## 领域红线（请务必遵守）

1. **过敏/食安相关逻辑必须走确定性校验（BLOCK），不允许降级为「提示」**
2. **所有写操作（改顺序/改派/退菜/替代食材）必须走「建议 → 人工确认 → 留痕」，禁止静默执行**
3. **不引入「静默合并过敏信息」的逻辑；备注随套餐拆单逐行传递是不可妥协的验收项**
4. 指标口径改动需在 `docs/design.md` 的「口径定义」一节说明分子/分母/排除规则/统计周期

## 报告缺陷

优先附上：最小复现订单 JSON、期望行为、实际行为、`data/audit_log.jsonl` 相关片段（脱敏后）。
