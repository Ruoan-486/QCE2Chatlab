# QCE2ChatLab 更新日志

## v1.5.1 (2026-07-11)

### Bug 修复
- **开机自启不生效**：`_get_app_exe_path()` 之前返回了 Python 解释器路径（`sys.executable`），导致自启 bat 只启动 python.exe 没带脚本。改为返回脚本路径（`sys.argv[0]`），并区分打包 exe 和脚本模式。
- Windows 开机启动 bat 内容改为 `pythonw main.py` 而非直接调用 exe 路径。

### 优化
- 自启 bat 写入时区分 frozen exe 模式和脚本模式。
