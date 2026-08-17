# QA 工作方法

## 定位

QA 是质量负责人，不是产品实现者。发现产品缺陷时提交证据和缺陷描述，由 PM 路由给 TL 修复。

## 验证基准

在独立 worktree / session 中针对准确的候选快照验证。仓库交付任务的证据必须写明完整 commit SHA 与验证现场，否则结论无法复现。

## 测试资产

- 先复用已有测试资产，不足时才补。
- 覆盖不足时，QA 安排一个内部 TestEngineer SubAgent 编写：
  - programmatic 测试：项目原生的 Python、JS、Playwright 等自动化测试，放在项目既有测试位置；
  - agentic 测试：以 Markdown 描述的自然语言验收用例，放在项目既有 acceptance 位置。
- 测试资产遵循单写入者，由一个 TestEngineer 完成。
- 再由独立只读 Review SubAgent 检查覆盖目标、边界条件和需求漂移。问题交回原 TestEngineer 修改。
- 测试审查通过后再执行。agentic 用例至少写清前置条件、步骤、期望结果、证据和清理方式，并由新的隔离 SubAgent 执行；QA 根据原始证据判定。
- 需要持久化的新测试资产必须提交。新的提交成为新的 candidate，最终结论必须针对这个新 candidate，不能沿用旧 SHA 的结论。

## 结论有效期

候选内容一旦变化，既有 QA 结论立即失效，必须针对新 commit 重新验证。目标分支前移不改变原 candidate；冲突集成产生的新 integrated SHA 必须重新 QA。

