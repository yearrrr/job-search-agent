# DeepSeek 真实连通性验证

验证时间：2026-09-13 23:32（Asia/Shanghai）。用户明确要求测试已在本机 `.env` 配置的 API Key。

| 项目 | 实测结果 |
| --- | --- |
| 结果 | 成功，官方 API 接受请求并返回可解析的非空响应 |
| 请求模型 | `deepseek-flash` |
| 请求地址 | `https://api.deepseek.com/chat/completions` |
| 真实请求次数 | 1，无自动重试 |
| 耗时 | 1.075406 秒 |
| 输入 Token | 16 |
| 输出 Token | 1 |
| 总 Token | 17 |
| 错误 | 无 |
| 金额 | 未配置价格，未估算 |
| 本地记录 | `data/app.sqlite3` 的 `api_attempts` 表 |
| 探测 ID | `d4d7be3a-cdd6-4661-a1b4-b5c64556e8e4` |

请求使用现有 `probe.py:check_api()`，只发送固定非敏感文本 `Reply with OK. This is a non-sensitive connectivity check.`，输出上限 32 Token。没有读取或发送个人资料，也没有回显 Key 或模型响应正文。

用户 `.env` 中仍为 `JOB_AGENT_MODE=mock`。本次依据明确测试指令，仅在调用对象中临时将模式设为 `deepseek`，没有修改 `.env`。以后通过命令行自行运行真实探测，需要把该配置改为 `deepseek` 并显式使用 `--live`；首页与虚构演示不会因此自动请求模型。

`.env` 已确认被 Git 忽略；恢复了一份 Key 留空的 `.env.example` 作为可公开的配置模板。

该结果证明本次使用此 Key、模型及端点的短文本调用成功。真实模型工具调用、资料提取、岗位分析及材料准确性仍待后续单独验证，不能由连通性成功推导业务验收通过。此前 48 项离线测试记录保持有效，本次仅配置及文档变化，没有再次运行完整测试或追加付费探测。
