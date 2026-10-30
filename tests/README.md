# 测试目录

功能与后端、API、界面的对应关系见[开发导航](../docs/developer-guide.md)。这里保留平铺结构，以兼容 `unittest` 发现、测试间共享导入，以及浏览器夹具使用的相对路径。

| 查找目的 | 入口 |
|---|---|
| 配置、供应商、模型、切换和各客户端 | `test_api*.py`、`test_modelsync.py`、`test_switch_plans.py`、`test_sync_transaction.py`、`test_*_scope.py`、`test_agent_connections.py` |
| Agent 实例 | `test_inventory.py`、`test_daily_scope.py` |
| Skills、继续工作、额度 | `test_capability_library.py`、`test_session_*.py`、`test_gateway_titles.py`、`test_handoff_summary.py`、`test_quota_monitor.py`、`test_workspace_*.py` |
| 网关、订阅和观测 | `test_gateway*.py`、`test_proxy_transport.py`、`test_*subscription*.py`、`test_*observation*.py`、`test_workbuddy_cases.py` |
| 桌面、入口与目录兼容 | `test_desktop_close.py`、`test_launch.py`、`test_backend_package_layout.py` |
| 前端无浏览器契约 | `ui*_contract.cjs`、`workbench_contract.cjs`、`bridge_contract.cjs` |
| 真实页面和交互 | `browser.cjs`、`daily_browser.cjs`、`workspace_browser.cjs`、`skills_bulk_browser.cjs`、`*_subscription_browser.cjs` |
| 共用夹具 | `support.py`、`*_fixture_server.py`、`fixture_server_helpers.py`、`daily_records.py`、`proxy_fixture.py` |

从仓库根目录运行 `python -m unittest discover -s tests`；单项测试和前端命令见[开发导航的验证部分](../docs/developer-guide.md#验证与构建)。浏览器测试会将截图写入 `tests/artifacts/` 的固定名称；需要保留现有截图时，在隔离副本中运行。`live_browser.cjs` 和 `live_capture_server.py` 不属于上述普通夹具回归，运行前先核对其数据来源。
