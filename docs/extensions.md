# 接口与插件开发

所有扩展协议在 `agenticwam.core.interfaces`，契约在 `agenticwam.core.types`。core 不导入具体实现。应用在 `config.assemble` 或自己的组合入口中注入实现。

## 任务与上下文

`Request` 保存原始目标和可选媒体引用；`Plan` 保存原始 mission、请求标识和顺序步骤。
`Step` 包含 instruction、success_criteria、preconditions、constraints、skill、时限。当前只执行顺序计划，不隐式调度并行依赖图。
`ContextCompiler.compile(step, context_id, revision)` 返回动作模型的输入。默认文本编译器只接受声明的 skill；OpenWAM 线协议由后端转换，不能让 core 使用 `wam.context/v1`。

`Plan.to_dict/from_dict` 使用 `agenticwam.plan/v1`。步骤标识校验防止逃逸日志目录；计划最多 64 步，单步最多 600 秒。
语义正确性仍需模型能力验证，类型校验不证明语言目标可执行。

## Backend

实现 `check_capabilities`、`observe`、`execute`、`cancel`：

- `execute(operation_id, context, units, timeout_sec, cancel=...)` 只执行有限批次。units 在 OpenWAM 中对应 action chunk，其含义由后端说明。
- 只有动作队列已处理完且服务处于规定交接状态时才返回 `state=completed`。失败抛异常或返回失败状态；不得把请求 ACK 当成完成。
- 成功结果包含 `finished_monotonic_ns`、`handoff_ready`、`receipt`；可提供 `completion_signal={type: ..., candidate: bool}`。
- 后端验证 context 的准确回执，并拒绝旧会话。Vela/OpenWAM 适配器校验完整文本、标识、版本及摘要。
- `observe(directory, after_ns=..., cancel=...)` 返回 `images` 文件引用和 `metadata`。metadata 的 `server_monotonic_ns` 必须严格大于 after_ns；图像时间也必须满足后端新鲜度要求。
- 一个后端的动作与观测时间必须属于同一执行主机的单调时钟域。不能把 Agent 机器的时间与机器人机器直接相减。
- 相机多视角的原子同步由后端负责；当前 Vela 使用快照加预览读取，不能宣称多视角严格同步。
- 所有阻塞方法应有截止时间并响应 cancel。错误返回后 core 会尝试取消当前 operation_id；取消不得让迟到的执行请求重新启动。

## Monitor

`begin` 检查前置条件，`should_check` 决定何时查看证据，`evaluate` 返回 `continue/succeeded/blocked/unknown`，并提供 reason 和 evidence。

`RequiredSignal` 可以要求任意适配器事件，不限于夹爪。事件只调度或门控检查，不能单独宣布成功。视觉规则来自任务配置，通用检查器不包含具体物体名称。

默认两次不同时间的证据确认后切换。这是工程稳定性策略，不是统计独立验证或已校准的置信度。证据不确定时停止；明确受阻时才允许调用可选重规划器。

## 有限重规划

`Replanner.revise(plan, completed, failed, evidence, cancel=...)` 返回剩余计划。
必须保留 request_id 和原始 mission，不得重新加入已经完成的步骤标识。Runner 保留完成历史，提升 context revision，并记录 plan_revised。
`max_replans` 默认为 0；显式开启后仍受重规划次数及总执行步数上限约束。当前语义约束保持主要依赖规划器，尚无形式化计划正确性证明。

## 安装式插件

外部包通过 Python entry point 注册，名字由可信配置选择，不允许语言模型输出模块路径并执行。

```toml
[project.entry-points."agenticwam.backends"]
my_policy = "my_adapter:create_backend"
```

```python
def create_backend(*, config):
    return MyBackend(config)
```

配置 `backend: my_policy` 和 `backend_config` 后，CLI 与 MCP 均使用该实现。另有 `agenticwam.planners`、`agenticwam.compilers`、`agenticwam.monitors` 分组。
工厂参数分别为 planner/monitor 的 `model, profile`，compiler 的 `config`。构造函数应延迟连接硬件至实际执行；列出插件不导入其代码。

新增模型供应商也可直接实现 StructuredModel.ask，注入 LanguagePlanner 与 VisionVerifier，无需修改 Codex 适配器或 Runner。插件代码拥有普通 Python 代码权限，应由部署者安装。
