# 升级到 AgenticWAM 0.3

本项目统一使用 AgenticWAM 品牌，安装包、命令、Python 导入名与插件分组均为 `agenticwam`。

## 从 0.2 升级

| 入口 | 旧名称（仅用于识别旧安装） | 当前名称 |
| --- | --- | --- |
| 仓库 | `Vela-Agent` | `AgenticWAM` |
| Python 安装包 | `velabot-agent` | `agenticwam` |
| 命令 | `vela-agent-kit` | `agenticwam` |
| Python 导入 | `vela_agent` | `agenticwam` |
| 插件分组 | `vela_agent.backends` 等 | `agenticwam.backends` 等 |
| 示例目录 | `src/vela_agent/examples` | `src/agenticwam/examples` |
| 文档中的控制台地址变量 | `VELABOT_CONSOLE_URL` | `AGENTICWAM_CONSOLE_URL` |

在停止旧 Agent 进程后，从本仓库重新安装：

```bash
git remote set-url origin https://github.com/ZzzzzzHhhhhhh/AgenticWAM.git
git pull --ff-only
python -m pip install .
agenticwam --help
```

这会安装新的分发包，不会自动删除旧包。确认没有其他项目依赖旧包后，可在对应虚拟环境中运行
`python -m pip uninstall velabot-agent`。新包不提供旧命令或旧 Python 导入的别名；自定义代码、启动脚本及外部插件需要修改导入与入口，并重新安装。

如果通过 MCP 连接 Codex，把 `command` 改为新环境中的 `agenticwam` 可执行文件，同时更新 `--profile` 指向的目录。MCP 工具继续使用 `agent_capabilities`、`agent_plan`、`agent_execute`、`agent_status`、`agent_events`、`agent_cancel`，无需更改调用名称。

## 计划和运行记录

新输出使用以下 schema：

- 计划：`agenticwam.plan/v1`
- 通用上下文：`agenticwam.context/v1`
- 运行事件：`agenticwam.event/v1`
- 指标：`agenticwam.metrics/v1`
- MCP 能力：`agenticwam.capabilities/v1`

`Plan.from_dict` 和 CLI 的 `--plan` 继续读取 0.2.0 的 `vela.agent.plan/v1` 文件，保留原始目标、步骤标识和约束；重新保存时统一输出新 schema。字段校验仍然执行，其他未知 schema 不会被接受。

`agenticwam inspect` 仍可汇总已有运行目录。外部事件消费者如严格检查 schema，需要更新允许的名称；旧日志不会被批量改写。进程重启不会恢复物理执行，应先核对现场和运行结果。

## Vela-Franka/OpenWAM 后端

Vela-Franka 是所适配的机器人执行系统名称，不是本项目的品牌。配置中的 `backend: vela-openwam`、适配类 `VelaContextBackend`、服务端的 `vela.agent.execution/v1` 和 `wam.context/v1` 线协议保持原样，避免改名破坏现有部署。机器人服务端无需因这次命名调整重新训练模型或变更接口。

早期原型的 `vela-agent`、`wam_*` 入口属于配套 Vela-Franka 工程，不由本仓库安装。原型的 `vela.context-plan/v1` 与本包计划结构不同，需在新入口重新生成计划；不能只替换 schema 名称。

独立包不依赖机器人驱动；真实执行仍需配套修改后的 Vela/OpenWAM 服务。单盘摆放、夹爪释放与视觉交接要求继续由任务配置和适配层实现。
