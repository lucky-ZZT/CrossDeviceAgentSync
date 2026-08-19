# CrossDeviceAgentSync 发布与持续优化操作手册

本文档是 CrossDeviceAgentSync 的固定维护流程。目标是让功能修改、预览验证、正式发布和发布后回验可以重复执行，并避免因为中断或网络失败重复运行已经通过的步骤。

## 一、基本规则

1. `skills/cross-device-agent-sync/` 是唯一设计源，不能直接在 GitHub 发布目录中长期修改代码。
2. 普通功能修改阶段保持当前正式版本号，生成无版本号的 preview EXE 供验证。
3. 只有用户明确说“发布”“提交发布”或含义相同的话后，才递增版本号、生成正式 EXE、提交 Git、推送标签和创建 Release。
4. 正式版本只递增一次。修改期间反复封装 preview 不改变版本号。
5. 本地完整测试只对最终源码运行一次。仅修改版本号、发布说明或维护文档后，不重复运行相同测试。
6. 正式 EXE 只在源码、依赖或打包参数变化后重新构建；同一产物不重复封装。
7. GitHub Actions 是独立复核，不替代本地测试，也不要求本地再次重复 CI 已完成的检查。

## 二、目录与产物

设计源：

```text
C:\Users\ZZT\Documents\Codex\skill-design-project\skills\cross-device-agent-sync
```

发布模板：

```text
C:\Users\ZZT\Documents\Codex\skill-design-project\publishing\cross-device-agent-sync
```

正式 EXE：

```text
skills\cross-device-agent-sync\assets\CrossDeviceAgentSync-vX.Y.Z.exe
```

预览 EXE：

```text
skills\cross-device-agent-sync\assets\previews\CrossDeviceAgentSync-preview-功能名.exe
```

发布检查点：

```text
.skill-tests\cross-device-agent-sync-release\vX.Y.Z.json
```

检查点记录源码指纹、测试状态、构建状态、SHA-256、提交和发布状态。输入未变化时，脚本自动跳过已经成功的步骤。

## 三、功能修改阶段

1. 修改设计源和对应测试。
2. 保持 `APP_VERSION` 为当前正式版本。
3. 运行受影响模块的定向测试。
4. 风险较高或准备交给用户验证时，运行完整测试并封装 preview EXE。
5. 在 `design-notes/cross-device-agent-sync.md` 记录改动、测试、预览路径、SHA-256 和已知风险。
6. 不提交 GitHub Release，不推送新标签。

预览构建示例：

```powershell
& .\skills\cross-device-agent-sync\scripts\build_windows_exe.ps1 `
  -OutputDirectory .\skills\cross-device-agent-sync\assets\previews `
  -ApplicationName CrossDeviceAgentSync-preview-transfer-progress
```

## 四、正式发布

用户明确批准发布后：

1. 确定下一个语义版本，例如 `1.0.5`。
2. 同时更新两个 GUI 入口的 `APP_VERSION`：

```text
scripts\simple_sync_gui.py
scripts\cross_device_agent_sync_gui.py
```

3. 更新 `.github/RELEASE_NOTES.md`，至少说明：

- 用户可见变化
- 数据安全和兼容性影响
- 采用与未采用的方案
- 测试数量和结果
- 正式 EXE 的 SHA-256

4. 从项目根目录执行固定发布脚本：

```powershell
& .\scripts\publish-cross-device-agent-sync.ps1 -Version 1.0.5 -Publish
```

脚本按顺序完成：

```text
版本一致性检查
源码指纹计算
完整测试与静态校验（一次）
正式 EXE 构建（一次）
EXE 自检与 SHA-256
准备干净 GitHub 仓库
提交 main
创建并推送 vX.Y.Z 标签
保存发布检查点
```

## 五、中断后继续

网络、GitHub 或构建中断后，重新执行同一条命令：

```powershell
& .\scripts\publish-cross-device-agent-sync.ps1 -Version 1.0.5 -Publish
```

只要源码指纹没有变化，脚本会跳过已经通过的测试和构建，从失败步骤继续。

如果本次功能源码已经在同一次工作中完成全量测试，之后只修改了版本号、构建脚本或发布文档，可登记已有测试结果，避免重复运行：

```powershell
& .\scripts\publish-cross-device-agent-sync.ps1 -Version 1.0.5 -Publish -ReuseVerifiedTests
```

该参数只登记当前测试指纹，不能用于跳过尚未实际执行的测试。

只有以下情况需要强制重跑：

```powershell
# 测试环境或依赖发生变化
& .\scripts\publish-cross-device-agent-sync.ps1 -Version 1.0.5 -Publish -ForceTests

# PyInstaller、运行时 Hook 或打包参数发生变化
& .\scripts\publish-cross-device-agent-sync.ps1 -Version 1.0.5 -Publish -ForceBuild
```

不要为了“更放心”无条件使用 `-ForceTests` 或 `-ForceBuild`，否则会失去检查点的意义。

## 六、发布后回验

GitHub Actions 创建 Release 后，只做一次外部产物回验：

1. 确认 Release 不是 draft 或 prerelease。
2. 确认附件只有当前版本 EXE 和 `SHA256SUMS.txt`。
3. 下载 Release 中的 EXE 和校验文件。
4. 比较下载 EXE 的 SHA-256 与校验文件。
5. 对下载副本运行一次 `--self-test`。
6. 在设计记录中写入最终 commit、tag、Release 地址和 SHA-256。

这一步验证的是 GitHub 实际提供给用户的文件，因此不能只用本地 EXE 代替。

## 七、检查何时必须重跑

必须重新运行完整测试：

- Python 业务逻辑或 GUI 行为发生变化
- 数据库、会话文件、备份、归档、导入或 Provider 写入逻辑变化
- 测试代码或运行依赖变化

不需要重新运行完整测试：

- 只修改版本号
- 只修改 Release 说明或维护文档
- 网络推送失败后重试

必须重新构建 EXE：

- 被打包的源码变化
- Python、PyInstaller、Pillow 等依赖变化
- runtime hook 或构建参数变化
- EXE 不存在或哈希不符合检查点

不需要重新构建 EXE：

- 只修改 GitHub 文档
- Git 提交或推送失败
- Release 工作流暂时失败

## 八、导出与导入优化验收

每次修改跨电脑迁移流程时，至少确认：

1. 导出显示扫描、元数据、生成迁移包和完成阶段。
2. 大型 rollout 流式写入 ZIP，不把全部会话同时载入内存。
3. 进度显示已处理对话数量和容量，界面不会表现为无响应。
4. 导入检查显示校验、目标扫描和冲突比较，不写入目标数据。
5. 正式导入显示备份、写入和验证阶段。
6. 完全相同内容跳过；冲突内容生成独立分支；新电脑现有内容不被覆盖。
7. 失败时能够恢复备份并重新启用按钮。
8. Codex 对话和自定义文件两种迁移包都覆盖进度测试。

## 九、已知发布风险

- GitHub 网络不可用时只重试网络步骤，不重跑测试或构建。
- PyInstaller one-file EXE 首次运行可能被 SmartScreen 或安全软件短暂占用；runtime hook 只重试读取权限错误，不吞掉其他异常。
- 构建输出目录必须先转换成绝对路径，避免将刚生成的正式 EXE误判为旧版本删除。
- 不允许强推 `main`，不允许覆盖已有版本标签或 Release。
- 不发布 preview EXE，也不把旧版本 EXE混入当前 Release。

## 十、后续优化方式

每次发现发布过程中的重复、失败或人工判断点时：

1. 先记录具体步骤、输入和失败原因。
2. 判断能否由源码指纹、文件哈希或远端状态自动确认。
3. 能可靠确认的步骤加入发布脚本和检查点。
4. 涉及版本决策、数据风险或上游代码取舍的事项仍由 Codex审核。
5. 同步更新本手册和 `docs/RELEASING.md`。

目标不是取消必要检查，而是让同一输入只检查一次，让失败后的重试只重复失败部分。
