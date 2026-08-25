# CrossDeviceAgentSync

适用于 Windows 的便携式同步工具，用于在不同代理、Provider 和电脑之间选择性迁移 Codex 对话、代理工作目录及自定义文件。

[English](README.en.md)

## 主要功能

- 自动识别本机 Codex Provider。
- 切换选中对话的 Provider 归属，不产生重复会话。
- 按需复制对话，同时保留来源和目标 Provider 版本。
- 在两台电脑之间用一个迁移包导出项目文件和关联对话。
- 新电脑可分别选择项目文件和具体对话，并离线注册普通路径侧栏项目。
- 批量管理对话、项目关联和会话内嵌图片。
- 同步自定义文件夹，并保留双方冲突文件。
- 在数据修改前创建可恢复的完整备份。
- Provider 写入前执行统一预检。
- 保存轮转、脱敏的软件诊断日志。
- 检查 CrossDeviceAgentSync 自身在 GitHub Releases 发布的最新正式版本。
- 软件仅在用户确认后下载、校验并替换新版本，不会静默更新。

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

跨电脑传输使用首页的“两台电脑之间传输”。旧电脑可在一个包中选择项目文件、关联对话或两者；新电脑先检查包，再分别勾选项目文件和具体对话。无冲突时直接映射普通路径；同名或同路径冲突会弹出选择，可改名导入、复用现有项目、合并重复注册或跳过。软件不会自动合并两个分别修改过的项目文件。

正式导入要求新电脑的 Codex 完全关闭。工具直接写入普通路径项目注册，不调用会产生 `\\?\` 路径的 `codex app`。导出不会删除旧电脑数据；确认新电脑内容正常后，可在“内容管理”中扫描并批量清理旧机副本。

“内容管理 → 修复路径问题”会把每条项目注册 ID 分开显示，列出完整路径、普通/扩展/缺失状态、引用数量、关联对话和建议操作。每条记录使用完全相同的操作区：查看、保留、修复路径、更正目录、重命名、删除注册、彻底删除项目。只有经过实际前置条件检查后确认不能执行的按钮才会置灰，并显示具体原因。删除一条重复注册会把已知引用迁移到保留项；普通“删除注册”保留文件和对话，“彻底删除项目”则完整备份对话、清除注册并把项目文件移入可恢复区。

“备份与恢复”会按本机时间显示备份时间，并同时显示每条对话的最后活动时间。可按标题、任务 ID、项目路径或 Provider 筛选当前备份，也可检索全部可恢复的对话删除备份。恢复前可双击结果或点击“预览所选”，只读查看最近对话内容；跨备份搜索结果会使用它自身对应的备份来源执行恢复，不依赖上方当前选中的备份。

内容管理分为对话、项目和图片三个视图：对话按规范化后的项目路径分类，可用“项目分类”筛选当前显示范围；列表同时显示简短项目名和完整目录，Windows 的 `\\?\` 内部路径会转换为正常显示。对话可在删除前单选预览或双击预览，窗口只展示用户/代理消息摘录，不会修改原始数据。正式标题优先读取 Codex 侧栏保存的固定名称，UUID、`Work in`、英文连字符名称和用户重命名均保持不变；只有可逆的编码乱码会在显示层修复，无法可靠修复的疑似乱码会保留原文并标记。最初请求只在预览中展示，不会被推断成标题。可将所选对话归档，也可复原已归档对话；两种操作都会同步会话文件、主数据库和兼容索引，并按照当前 Codex 的权威移除语义删除旧侧栏目录行。复原后由 Codex 在下次启动时通过官方观察流程重建完整目录记录，软件不会猜测版本相关字段。操作前均创建完整备份。同名内容只标记为“可能重复”，不会自动删除；删除对话前同样完整备份；项目只移入软件项目回收区；图片按内容去重显示，可预览、查看影响评估、批量选择浏览器截图。默认建议“保留 1 份，清理重复图片”，彻底清理必须人工确认。

完整操作见 [用户操作手册](docs/USER-GUIDE.md)。

## 数据安全

- 默认不同步 `auth.json`、Cookie、私钥、`.env`、运行时套接字或浏览器登录状态。
- Provider 写入前检查进程、锁、SQLite 完整性、路径、元数据、权限和磁盘空间。
- 只流式处理选中 Rollout，并检测处理期间发生的文件变化。
- 操作失败时从事务备份恢复文件、索引和 SQLite。
- 分叉对话保留为独立分支，不拼接 JSONL 历史。
- 导入时同 ID 但内容不同的对话会保留新电脑原对话，并另建迁移分支；完全相同的对话会跳过。
- 内容管理中的删除和图片清理均可通过“备份与恢复”撤销；项目回收区提供独立恢复入口。
- 软件不会静默下载或替换更新；用户确认后才会下载、校验、替换并重启。

## 检查更新

在首页打开“检查更新”。

- 软件只读取本项目最新的正式 GitHub Release。
- 软件比较当前版本和最新版本，并显示 Release 更新说明、发布时间和附件。
- 发现新版本时会出现“立即更新”按钮。
- 确认后，软件直接下载 EXE 和 `SHA256SUMS.txt`，校验通过才会退出、替换当前 EXE 并重启。

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
powershell -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1 -ApplicationName CrossDeviceAgentSync-vX.Y.Z
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
