# 首次环境检查记录

检查日期：2026-09-13，Asia/Shanghai；环境探测时间为 22:23。范围是现有文件、开发约定与本地运行能力，**没有安装项目依赖、修改全局 Python、启动应用或调用真实 DeepSeek API**。

此文件保留首次检查的历史状态；后续已完成隔离环境、应用骨架和测试，最新结果见[阶段一记录](stage-one-report.md)。

## 1. 目录与开发约定

工作目录：`C:\Users\75796\Desktop\agent learning`。

检查前包含 `OpenJobAutofill-main/` 和 `OpenJobAutofill-main.zip`。读取扩展 README、manifest 和忽略规则，确认是 Manifest V3 的 JavaScript 浏览器填表扩展（manifest 版本 1.0.2），现有代码不构成 Python/LangGraph 应用骨架。

检查工作目录下的文件清单，并检查从盘根到工作目录的父级 `AGENTS.md`；未发现适用的 `AGENTS.md`。新项目采用用户原始需求中的开发约定。当前工作目录不属于 Git 仓库，已有扩展顶层也没有 `.git`。

新文档位于独立的 `job-search-agent/`。没有复用扩展里的资料或密钥配置，没有修改扩展或压缩包；原始需求另存只读用途的副本，附件本身保持原样。

## 2. 环境实测

| 检查 | 实际结果 | 意义与限制 |
| --- | --- | --- |
| 操作系统 | Windows 11，构建 26200 | 本次按 PowerShell 路线规划 |
| `python --version` | Python 3.12.4 | 可以作为下一步虚拟环境基础 |
| Python 启动器 | `py -0p` 显示 3.12（默认） | 检测到一个已注册的 3.12 解释器 |
| `python -m pip --version` | pip 24.0，绑定 Python 3.12 | 不需要为了首次开发强制安装 `uv` |
| `git --version` | Git 2.53.0.windows.3 | 来自 Codex 随附运行目录；独立终端的 PATH 仍需复查 |
| `uv` | 当前 PATH 未发现 | 可选工具，非阻塞 |
| `git rev-parse --show-toplevel` | 当前目录不是 Git 仓库 | 不是 Git 不可用；本次未初始化仓库 |
| SQLite | 3.45.3 | Python 标准库可连接内存库 |
| SQLite 基础读写 | 建表、参数化插入与查询通过 | 不是项目业务层或磁盘恢复测试 |
| SQLite FTS5 | 建虚表及 `LangGraph` 关键词查询通过 | 未验证中文分词/召回、排序和真实资料检索 |
| `venv` / `ensurepip` | 模块可发现 | 尚未实际创建和安装独立虚拟环境 |
| TLS 库 | OpenSSL 3.0.13 | 仅本地能力检查，不等于服务商网络连通 |
| `DEEPSEEK_API_KEY` | 当前进程未检测到非空值 | 仅检查是否存在，未打印密钥；不代表其他终端或配置文件一定没有 Key |

当前 `python` 路径：`C:\Users\75796\AppData\Local\Programs\Python\Python312\python.exe`。

当前 Git 路径：`C:\Users\75796\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe`。

`python3` 当前解析为 WindowsApps 路径；下一步优先使用已验证的 `python`/`py -3.12` 及 `.venv\Scripts\python.exe`，不假定该别名可正常运行。

通过安装元数据检查，当前解释器未安装：`fastapi`、`uvicorn`、`jinja2`、`python-multipart`、`pydantic`、`langgraph`、`langgraph-checkpoint-sqlite`、`pypdf`、`pytest`、`httpx`、`python-dotenv`。额外检查的 `openai` SDK 也未安装，当前设计不强制采用该 SDK。

没有枚举其他进程的环境、读取浏览器配置或搜索现有秘密文件。真实 API 可用性、余额、模型权限及安装网络均未验证。

## 3. 已有文件保护基线

在新增文档前计算，并在交付前复查以下摘要：

| 项目 | 初始值 |
| --- | --- |
| 现有扩展文件数 | 23 |
| 扩展目录聚合 SHA-256 | `64836f81176528df98ee9a9974688c4356c6febea14a80402aded5b406041e77` |
| 原压缩包 SHA-256 | `ccb0854c6c761f13addd4b1f22569d42aca1e0f91e776eb1e19fbecd771f23a8` |

目录摘要算法：按路径排序每个文件，对“相对工作目录的 POSIX 路径 UTF-8 字节＋空字节＋文件内容 SHA-256 原始摘要”依次累积 SHA-256。只读取文件字节计算，不改写内容。

复查结果及文档检查记录见[首次交付检查](first-pass-verification.md)。

## 4. 未执行与后续事项

- 尚无项目虚拟环境、锁定依赖、应用服务或业务自动化测试。
- SQLite 磁盘持久化、LangGraph 检查点重启、并发保存与真实 PDF 解析未测。
- 非敏感文本 API 连通性未测；本次真实模型请求数为 0，Token 与费用无实测记录。
- 已查阅 LangGraph 与 DeepSeek 官方文档辅助设计；这属于阅读公开文档，不是模型 API 连通性测试。没有发送个人资料。当前金额价格未完成核对，计划不写死价格。
- A15 独立环境复现仍未验收，不能由上述版本检查推导通过。

下一步创建隔离环境并验证基础骨架。若网络或包兼容出现实际问题，再依据错误处理；不提前假定必须升级系统、换技术路线或购买服务。
