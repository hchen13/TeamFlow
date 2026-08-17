# 软件开发协作（software-development）

协调负责人是 `pm`。项目决策人（stakeholder）在职责之外，不注册 Agent，只通过 PM 参与。

| 稳定键 | 职责 | 数量 | 主要责任 |
| --- | --- | --- | --- |
| `pm` | PM | 单个 | 目标、范围、优先级、任务路由、最终验收 |
| `tl` | 技术负责人 | 可多个 | 技术方案、实现、代码质量、技术集成 |
| `qa` | QA | 可多个 | 独立验证、缺陷证据、测试资产 |
| `design` | 设计 | 可多个 | 交互、视觉、用户体验、用户文案 |

## 读取顺序

1. 所有职责先读[协作与交接协议](collaboration.md)，了解其他职责会交付什么、下一步由谁负责。
2. 只读当前职责的工作方法：[PM](pm.md)、[TL](tl.md)、[QA](qa.md) 或 [Design](design.md)。不要把其他职责的详细工作方法加载进当前上下文。
3. 仅当卡片的 `delivery_mode` 是 `repository` 时，再读[仓库交付协议](repository-delivery.md)。

合法动作、参数、字段和值始终以 `get_assignment` 返回的 workflow contract 和 `get_task.available_actions` 为准。本目录说明协作方法，不能改变机器权限或状态机。

