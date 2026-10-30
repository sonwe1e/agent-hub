# 额度提醒实现与边界

额度监控只查询上游提供的快照，默认启用，每 15 分钟查询一次；有明确比例的窗口剩余不高于 20% 时提醒。查询间隔、剩余比例和开关可调整。它不会更换账号、线路或模型，不进行推理请求，也不修改任何客户端的登录或配置。未知比例保持未知；查询失败时，账号身份确认未变的旧快照仍可查看，但标为过期。

目前适配的快照来自 Hub 已有的 Codex ChatGPT 与 ZCode 额度接口，具体窗口是否可用取决于账户及上游返回。Codex 和 ZCode 个人 Coding Plan 有早前真实接口验证；ZCode 团队套餐与 Start Plan 仅有模拟证据。其他线路暂没有可用的此类查询。原生历史快照和订阅线路快照分别呈现，不能当作实时账单或其他账号的额度。

提醒按当前账号、窗口和级别去重，重置时间改变或低额度恢复后再次跨过阈值可重新提醒。额度快照显示 100% 时，消息表述为“快照显示已用完”，不宣称实际请求已经遭拒。HTTP 429 可能是额度不足，也可能是请求速率过高；只在上游给出明确结构化错误代码时区分，不能根据 429 本身断言额度耗尽。

应用运行期间，可独立每 30 秒检查已索引的订阅共享线路失败事件及 Hub 网关记录的 Codex 原生线路失败，以请求标识去重。索引未记录请求时的账号身份，这些提醒标为账号未知，不归属给当前账号。只提醒本次监控启动之后发生的事件，避免把历史故障当作新问题。监控只覆盖已有观测数据：采集关闭、未经网关的直连没有相应原生日志、数据库暂不可读或上游错误未记录时，不能保证发现；回看窗口为五分钟，超过窗口才乱序到达的事件可能漏提醒。桌面系统禁用通知时事件仍可在 Hub 界面查看，系统通知是否最终展示不由 Hub 保证。

`data/quota-monitor-settings.json` 存设置；`data/quota-monitor-alerts.json` 存去重信息及随机 HMAC 密钥。账号标识只参与内存中的 HMAC，不以原文写入磁盘、公开接口或通知。密钥用于避免持久化可直接识别的账号标识，不是针对本机目录被读取时的绝对隐私保证。发生账号切换时旧快照立即失效；查询期间发生切换会丢弃该次结果。

## 代码接入

`quota_monitor.QuotaMonitor(root, queries, identities, connected=None, notify=None, recent_failures=None)` 接收两组以 `codex-chatgpt` 和 `zcode-subscription` 为键的无参回调：`queries` 返回已有适配器的 `{quota: ...}` 或 quota 对象，`identities` 返回当前账号和套餐的稳定身份对象；监控器马上做 HMAC，绝不输出身份原文。可注入 `connected` 回调判断线路是否接入。

`start()` 启动后台线程，`close(timeout)` 停止并返回是否在限时内退出。`status()` 返回设置、每个来源的快照/错误/过期状态和公开事件；`update_settings()` 更新设置并持久化；`refresh(provider=None)` 同步执行一次手动查询。`notify(event)` 接受固定格式的脱敏事件。

可选的 `recent_failures(since)` 返回包含 `id`、`provider`、事件发生时间 `time`、`httpStatus` 和可选结构化 `code` 的记录列表。事件阅读应使用本机观测索引，禁止返回原始错误正文；仅提供时间字段并不能证明某条历史线路属于当前登录账号。`report_upstream(...)` 也可直接接收新错误事件。`status` 中的 `failureMonitoringAvailable`、`failureReadError` 和 `lastFailureCheckAt` 用于区分未接入、读取失败和正常检查。

测试：`python -m unittest discover -s tests -p test_quota_monitor.py -v`。这些使用临时目录和模拟查询，不调用真实账号；桌面通知、打包生命周期及真实限额事件仍需各自验证。
