# Agent Hub：开发入口

开始修改前先看 [开发导航](docs/developer-guide.md)：它按功能列出业务实现、API、界面和测试，并说明桌面版与浏览器版的共同边界。

- 根目录保留的旧模块名有些是兼容壳；修改这些功能时以 `agent_hub/` 下的实现为准。`hub.py`、`server.py`、`gateway_runtime.py` 等仍是实际实现，不是兼容壳。
- 保留工作区已有改动。配置、客户端文件和运行数据可能包含个人信息或密钥；开发和验证优先使用临时配置、临时目录与合成记录。
- 按改动范围运行相关测试；跨功能调整可运行下面的基础回归，浏览器和打包验证见开发导航。

```powershell
python -m unittest discover -s tests
node tests/ui_contract.cjs
node tests/ui_async_contract.cjs
node tests/workbench_contract.cjs
node tests/bridge_contract.cjs
node tests/ui_assets_contract.cjs
```

- 网关、客户端配置写入和桌面启动都与本机状态有关。`scripts/verify-*-live*.py` 等真实验证脚本可能使用账号、启动客户端或消耗额度；运行前核对脚本和本次任务的授权范围。
