# CrossDeviceAgentSync

适用于 Windows 的便携式同步工具，用于在不同代理、Provider 和电脑之间选择性迁移 Codex 对话、代理工作目录及自定义文件。

[English](README.en.md)

## 主要功能

- 自动识别本机 Codex Provider。
- 切换选中对话的 Provider 归属，不产生重复会话。
- 按需复制对话，同时保留来源和目标 Provider 版本。
- 在两台电脑之间导出和导入选中对话。
- 同步自定义文件夹，并保留双方冲突文件。
- 在数据修改前创建可恢复的完整备份。
- Provider 写入前执行统一预检。
- 保存轮转、脱敏的软件诊断日志。
- 检查 CrossDeviceAgentSync 自身在 GitHub Releases 发布的最新正式版本。
- 软件只提示版本和更新说明，不自动下载或安装。

## 下载

从 [Releases](../../releases) 下载最新的 `CrossDeviceAgentSync-vX.Y.Z.exe`。

软件为单文件便携版，不需要安装 Python。首次使用前建议核对 Release 中公布的 SHA-256。

## 快速开始

1. 启动 EXE。
2. 在首页选择需要的工作流。
3. Provider 操作中先点击“自动识别 Provider”，选择来源和目标，再加载来源对话。
4. 使用勾选框、“全选”、“全不选”或“反选”选择对话。
5. 执行“执行前检查”，处理报告中的全部问题。
6. 写入、导入或恢复前完全退出 Codex。
7. 需要恢复能力时保留完整备份选项。

跨电脑导入分两步进行：先点击“检查迁移包”，确认新增、跳过和保留两份的项目；确认无误后再点击“开始导入”。检查不会写入目标数据。

完整操作见 [用户操作手册](docs/USER-GUIDE.md)。

## 数据安全

- 默认不同步 `auth.json`、Cookie、私钥、`.env`、运行时套接字或浏览器登录状态。
- Provider 写入前检查进程、锁、SQLite 完整性、路径、元数据、权限和磁盘空间。
- 只流式处理选中 Rollout，并检测处理期间发生的文件变化。
- 操作失败时从事务备份恢复文件、索引和 SQLite。
- 分叉对话保留为独立分支，不拼接 JSONL 历史。
- 导入时同 ID 但内容不同的对话会保留新电脑原对话，并另建迁移分支；完全相同的对话会跳过。
- 软件不会自动下载、执行、安装或替换任何更新代码。

## 检查更新

在首页打开“检查更新”。

- 软件只读取本项目最新的正式 GitHub Release。
- 软件比较当前版本和最新版本，并显示 Release 更新说明、发布时间和附件。
- 发现新版本时可以打开 Release 页面手动下载。
- 软件不会自动下载、安装或替换当前 EXE。

`codex-provider-sync` 和 `codex-rehome` 的变化由维护本项目的 Codex 定期审查，不属于软件界面功能。是否值得采用、如何修改、测试和发布由 Codex 负责。维护流程见 `references/maintenance-update-review.md`。

## 开发与构建

环境要求：

- Windows 10 or Windows 11
- Python 3.12+
- PyInstaller

运行测试：

```powershell
py -m unittest discover -s tests -v
```

构建便携 EXE 和 SHA-256 文件：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1 -ApplicationName CrossDeviceAgentSync-v1.0.2
```

校验 Skill：

```powershell
py path\to\skill-creator\scripts\quick_validate.py .
```

## 项目结构

```text
agents/       Skill interface metadata
assets/       Release EXE and SHA-256 checksum
docs/         User and release documentation
references/   Architecture and upstream integration notes
scripts/      Application and synchronization implementation
tests/        Unit and GUI layout regression tests
SKILL.md      Codex skill instructions
```

## 日志

软件日志位置：

```text
%LOCALAPPDATA%\CrossDeviceAgentSync\logs\application.log
```

日志达到 5 MB 后轮转，保留五个历史文件；已知对话正文和凭据字段会被脱敏。

## 许可证

当前尚未选择许可证。公开仓库前应确定许可证，明确他人使用和再发布条件。
