# 从摆盘原型迁移

旧入口 `vela-agent`、旧计划格式 `vela.context-plan/v1`、`wam_*` MCP 工具继续可用；适配层将旧计划转为通用 Plan，再执行同一个 Runner。

新入口 `vela-agent-kit` 使用 `vela.agent.plan/v1`，显式记录原始 mission、skill、前置条件和约束。旧计划不会自动成为新格式文件，可用旧入口执行或重新规划。新入口支持按配置选择后端插件。

原来的 CodexJson 调用、视觉判定、HTTP 后端与任务管理已抽到本包，Vela 原模块保留兼容导入。控制线程、动作预算、旧会话隔离、停止与上下文回执仍在原部署链路执行。

摆盘专用语义移入 profile.verification：盘子可见、盘架有空位、已经放置的盘子不变、释放后稳定、机械臂可以交接。其他任务配置自己的条件。

独立包安装不需要 Vela 的 numpy、相机驱动和控制依赖；真实执行时仍需一个实现协议的 Vela/OpenWAM 服务。服务端无需为这次核心抽取重新训练模型。

事件格式由旧的 `vela.context-event/v1` 变为 `vela.agent.event/v1`，事件名称与游标行为保持兼容。新的 plan.json 是通用格式；若外部程序严格检查旧 schema，需要同步更新读取器。
