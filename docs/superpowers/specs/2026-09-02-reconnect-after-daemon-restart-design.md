# Connector 重启后的 Agent 自动恢复设计

日期：2026-09-02
状态：已实现，待合入

## 目标

当常驻 Connector 因 `restart`、崩溃或人工维护更换本地 process token 后，已经运行的
`asklear-browser-agent` 不要求用户重启 MCP 客户端，也能继续完成下一次浏览器调用。

## 不变边界

- 只处理本地 Agent → Connector 的 process token 重新握手。
- 不改变浏览器六个工具、扩展 WebSocket 协议、Credits 或页面数据边界。
- 不重试已经被 Connector 接受的业务命令；首次响应为 HTTP 401 时才允许恢复。

## 方案

`BrowserAgentAdapter._call()` 发送命令后，如果收到 HTTP 401：

1. 将 `_ensured` 置为 `False`，使下一次握手重新读取私有 token 文件并验证 Connector。
2. 重新执行一次 `_ensure_connector()`。
3. 只用原始请求参数重发一次命令。
4. 第二次仍失败时，按原有错误契约返回，不再重试。

401 在 Connector 鉴权层被拒绝，业务命令尚未执行，因此一次重试不会造成重复浏览器
操作。其他状态码和网络异常不走这条重握手路径。

## 验收

- 首次命令收到 401、重新握手后第二次成功：返回成功结果，命令发送次数为 2。
- 重新握手失败：返回明确的 Connector 错误，命令最多发送 1 次重试。
- 首次响应不是 401：保持原行为，不重复发送。
- 真实流程：启动 Agent → 调用浏览器 → 重启 Connector → 使用同一 Agent 再调用浏览器成功。
