# 🔗 QCE2ChatLab

> QQ 聊天记录自动同步工具  
> QCE 导出 → ChatLab 导入，全流程无人值守

---

## 开源重要声明

1. 本项目遵循 **自定义非商用开源协议**，**仅允许个人非商用学习使用**；
2. 严禁任何商业售卖、付费部署、企业商用，**违者追究法律责任**；
3. 所有二次修改、分发程序/源码，**必须完整保留原作者署名及本协议**，不得删除；
4. 详情查看仓库根目录 [`LICENSE`](LICENSE) 文件。

---

## 功能

| 功能 | 说明 |
|------|------|
| 📡 一键同步 | 添加好友/群聊，一键完成 QCE 导出→ChatLab 导入 |
| ⏰ 定时自动同步 | 每天/每周/每月，精确到分钟，到点自动执行 |
| 🤖 智能服务管理 | 自动检测 QCE/ChatLab 是否运行，未启动自动拉起 |
| 🪟 QQ 进程处理 | 同步时检测 QQ 运行状态，不打断用户 |
| 📜 同步记录 & 重试 | 区分手动/定时同步，失败可一键重试 |
| 🚀 一键/后台启动 | CMD 一闪即关，支持桌面快捷方式 |

## 技术架构

```
QCE（QQ Chat Exporter）
    ↓ 导出 JSON 文件
QCE2ChatLab（Python FastAPI）
    ↓ Push API 导入
ChatLab（本地知识库）
    ← 浏览器控制页 http://127.0.0.1:15520
```

所有数据不出你的电脑。

## 快速开始

1. 安装 [Python 3.11+](https://python.org)
2. 下载 [QCE](https://github.com/QQ-Chat-Exporter) 和 [ChatLab](https://github.com/chatlab)
3. 下载本仓库最新版本
4. 运行 `启动.bat` 或 `一键启动.bat`
5. 浏览器打开 `http://127.0.0.1:15520`

详细说明见 [`使用说明.txt`](使用说明.txt)

## 依赖

```
pip install fastapi uvicorn httpx pydantic
```

## 许可证

[自定义非商用 NC-BY-SA 协议](LICENSE) — 禁止商用，强制保留署名

---

*本工具由若安独立开发 · 有问题反馈至 48650918@qq.com*
