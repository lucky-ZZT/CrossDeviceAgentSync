## 主要内容

- 自动识别本机 Codex Provider。
- 选择性切换对话归属，不创建重复会话。
- 可选择创建副本，同时保留来源和目标 Provider 对话。
- 在两台电脑之间导出、导入选中的 Codex 对话。
- 同步自定义文件夹，并保留双方冲突文件。
- 数据修改前支持完整备份、恢复和失败回滚。
- Provider 写入前执行进程、锁、SQLite、路径、权限和磁盘空间预检。
- 保存轮转、脱敏的软件诊断日志。
- “检查更新”只比较本项目最新 GitHub Release，不会自动下载或安装。

## 更新与维护

软件更新按正常 GitHub Release 流程发布。`codex-provider-sync` 和 `codex-rehome` 的变化由 Codex 按维护文档独立审查，判断值得采用后才修改、测试和发布新版本。

## 验证

- 单元与 GUI 回归测试：34 项通过
- EXE `--self-test`：通过
- SHA-256：`861203C0EDD7FE95A0A471676F7711AB95B61B80AC3B5C9C15C30992D280FA00`

## 使用

下载 `CrossDeviceAgentSync-v1.0.2.exe`，运行前建议核对上述 SHA-256。执行 Provider 写入、导入或恢复前，请完全退出 Codex。
