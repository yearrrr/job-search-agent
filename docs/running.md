# 安装、运行与基础演示

适用范围：首版及第二版前两批，Windows 11、Python 3.12。第二版新入口见[第二批交付记录](v2-batch-two-report.md)，首版历史流程见[阶段三讲解](stage-three-walkthrough.md)。

## 1. 首次安装

需要 Python 3.12 和 Git；在包含 `pyproject.toml` 的项目目录运行。已有 `.venv` 可以跳过创建步骤，不要覆盖用户的其他环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
.\.venv\Scripts\python.exe -m pip check
```

`requirements.lock` 锁定本次实际安装成功的直接、间接及开发依赖版本，不含个人路径。安装需要网络，但不请求模型 API。项目代码与当前验证环境限定 Python 3.12；其它 Python 版本/操作系统尚未测试。

这些命令直接调用虚拟环境的 Python，不必激活环境，也不必改变 PowerShell 执行策略。开发安装 `-e .` 会立即使用源代码改动；普通使用可以去掉 `-e`，安装构建出的包。

若通过压缩包取得项目且没有 `.git`，先在新项目根目录执行 `git init`，供忽略规则测试使用；不要在包含个人资料及其他项目的父目录初始化仓库。本次本机已经初始化，无远程仓库、无提交或推送。

## 2. 启动与停止

```powershell
.\.venv\Scripts\python.exe -X utf8 -m job_search_agent.cli serve
```

访问 [本地工作台](http://127.0.0.1:8000) 和 [健康检查](http://127.0.0.1:8000/health)。服务只监听 `127.0.0.1`，默认端口 8000；在启动它的终端按 `Ctrl+C` 停止。若该端口已被占用，先确认是否已有服务，或在本地 `.env` 设置 `JOB_AGENT_PORT=8001` 后使用对应地址。

启动时首次创建 `data/app.sqlite3`。首页只读，不调用 DeepSeek。第二版资料导入和“使用 LLM 解析 JD”会按页面说明发送本次文字或图像，即使岗位任务的默认模式是 mock；岗位准备分析可单独选择离线或实际模型。只修改岗位状态不会发送模型请求。配置和数据库错误使用固定提示，不把 Key 或服务商原始报错写到页面。

## 3. 默认测试与代码检查

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
```

自动化测试默认阻断 socket，不读取用户 `.env`，不使用真实 Key 和真实资料。Web 测试仅允许 Windows 异步运行时需要的 `127.0.0.1`/`::1` 回环，并检查外部地址仍被拒绝；页面请求通过内存测试客户端执行。API 响应使用 HTTP 模拟传输，子进程恢复测试也显式禁止网络。

每次测试在被 Git 忽略的 `.local/pytest/<唯一目录>` 中产生独立虚构数据库。这样避开本机系统临时目录的旧权限，不修改系统权限或用户文件。目录保留以便排查；不要放真实资料，也不要把日志或缓存强制加入 Git。

当前有一个来自 Starlette/AnyIO 的上游弃用警告，不影响通过结果。项目自己的弃用警告仍作为错误处理；没有通过跳过业务断言掩盖失败。

## 4. 体验 LangGraph 暂停与恢复

此演示始终使用模拟模型，即使 `.env` 选择 DeepSeek 也不联网。它只查询代码中明确标记的虚构资料，不接触你的简历。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m job_search_agent.cli smoke start --task-id learning-demo
```

预期返回 `waiting_confirmation`、2 次模拟模型请求、1 次工具检索。此时可以关闭终端，再打开同一项目目录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m job_search_agent.cli smoke status --task-id learning-demo
.\.venv\Scripts\python.exe -X utf8 -m job_search_agent.cli smoke resume --task-id learning-demo
```

确认后返回 `completed`，模型请求数仍为 2；重复执行 `resume` 返回原结果，不再新增保存。若不想保存，在等待确认时执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m job_search_agent.cli smoke resume --task-id learning-demo --reject
```

`--reject` 只能拒绝尚待确认的任务，不删除已经保存的结果。任务 ID 决定同一演示身份；想重新开始时使用新的 ID。ID 仅接受字母、数字、下划线和连字符。

检查点在 `data/checkpoints.sqlite3`；虚构确认结果在业务库的 `smoke_results` 表。演示不创建个人事实、材料版本或真实投递记录。命令应串行运行；同一任务并发执行以及运行中强制中止的全面恢复留待后续阶段验证。

## 5. 本地配置 API Key

希望执行真实模型提取、岗位分析或连通性探测时需要配置，不要把 Key 发到聊天中。先在本地编辑器中将 `.env.example` 另存为 `.env`；如果 `.env` 已存在，直接修改需要的字段，不覆盖整个文件。

在 `.env` 内设置：

```dotenv
JOB_AGENT_MODE=deepseek
DEEPSEEK_API_KEY=在本地编辑器填写你的真实密钥
DEEPSEEK_MODEL=deepseek-flash
```

上面的中文是占位说明，不能原样用作 Key。`.env` 已被 Git 忽略；环境变量优先于文件，如果配置似乎没生效，检查当前终端中同名变量是否覆盖了它，但不要把变量值输出到日志或聊天。

模型默认名根据 2026-09-13 的 [DeepSeek 官方快速开始](https://api-docs.deepseek.com/)选取，可以在本地调整为账号支持的型号。程序固定访问官方端点，不自动切换第三方服务商。

## 6. 非敏感文本真实探测

配置后显式运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\check_api.py --live
```

本次最多发送一次固定的短文本 `Reply with OK. This is a non-sensitive connectivity check.`，输出上限 32 Token，关闭思考模式，不自动重试。不会读取 PDF、原始资料目录或个人事实。真实调用可能产生服务商 API 费用；当前未配置价格，不给出虚假的金额估算。

没有 `--live`、未选择 `deepseek` 模式或 Key 为空时，不发送请求。响应输出仅包含探测 ID、状态、错误类别、耗时和用量；不回显模型正文或 HTTP 错误正文。用量缺失用 JSON `null` 表示未知。探测尝试写入 `api_attempts`，请求前先登记；同一内部探测 ID 不重复调用。

退出码：0 为当前命令成功；1 为任务/探测失败；2 为模式、配置或参数不满足。鉴权、参数、余额、限流、网络和非法输出均有限处理。`MODEL_TIMEOUT_SECONDS` 控制 HTTP 操作超时，不是任务总时长保证。

HTTP 适配器一次调用只有一次尝试。阶段四业务任务会按持久化的 `MAX_RETRIES` 对 HTTP 429/500/503 有限重试，次数计入总预算；探测和字段提取仍不自动重试。新执行一次 `check-api --live` 是新的显式付费尝试。若进程在请求发送后异常关闭，本地可能无法确定是否已计费，已有 `started` 记录不能当作“零消耗”。

将模式改回 `mock` 后，文档提取与基础探测默认不发真实请求。阶段三岗位页另有明确的单次运行方式选择：选 DeepSeek 并点击开始仍会调用本机 Key，不改写 `.env`。真实 Key 配好后只需告知“已在本机配置”，不必发送内容。

## 阶段二虚构演示

```powershell
.\.venv\Scripts\python.exe -X utf8 -m job_search_agent.cli seed-demo
```

也可点击看板的“加载虚构测试样例”。默认打开虚构区；真实资料请使用“切换到真实资料区”。每个区的事实、偏好和岗位独立查询，学习笔记不作为经历。

要重现一次真实模型字段提取测试，可运行 `python scripts/check_stage_two_api.py --live`；脚本只发送公开虚构简历，同一操作已完成后只返回已保存的指标，不重复请求。它临时选择 DeepSeek，不改写 `.env`。仍需在本机配置有效 Key。

虚构 PDF 已打包，不需要另装 PDF 生成工具即可使用。仅在重新制作样例 PDF 时，`scripts/create_fictional_pdf.py` 需要 reportlab 及 Windows 宋体；开发时使用了 Codex 提供的文档运行时。公开样例保留在 `sample_data`，`output/` 和 `data/` 默认被 Git 忽略。


## 阶段三任务恢复与独立真实测试

岗位页点击“开始分析岗位”创建任务；后续始终打开同一个任务链接。等待补充或确认时可停止服务，重启后步骤和调用次数保留。处理中断的任务需点击“继续未完成任务”。刷新 GET 不执行模型；所有写入动作仍校验同源和 CSRF。

正式业务图检查点在 data/workflow.sqlite3，任务摘要与材料在 data/app.sqlite3。备份时应先正常停止服务，再备份整个 data 目录，不能只保留其中一个数据库。

固定合成样本的真实接口验证：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 scripts\check_stage_three_api.py --live
~~~

脚本需要本机 Key，但不需要修改全局模式；最多 6 次模型、8 次工具尝试，无自动重试。使用独立的 data/stage-three-live-fixture，只有脚本内的固定合成文本会被读取和发送。运行结果存入该目录的 report.json；同一脚本操作会打开原任务，不重复创建付费测试。结果停在待确认草稿，供核对；本机网页仍使用主 data 目录的档案。

## 阶段四可靠性验证

使用既有开发环境运行：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest
~~~

仅运行阶段四新增的故障与引用回归场景：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_stage_four.py tests/test_stage_four_recovery.py tests/test_stage_four_boundaries.py tests/test_stage_four_web.py tests/test_draft_reference_regression.py
~~~

测试通过 MockTransport 模拟服务商响应，子进程用合成数据执行硬退出；所有数据位于隔离的临时目录，默认禁用网络。不要对主 data 目录照搬故障注入。正式业务图的恢复与错误处理步骤见[可靠性说明](stage-four-walkthrough.md)。

新业务任务会保存 MAX_RETRIES；默认仅针对 HTTP 429/500/503 额外重试一次，并计入 MAX_MODEL_CALLS。此前任务缺少该配置时不自动重试。阶段一 smoke 是教学命令，阶段四的并发领取和硬退出保证以正式业务任务为验收入口。

## 第二版第三批：定制简历

安装更新后按同样命令启动服务，健康页应显示 release 为 v2-batch3、schema_version 为 7。安装依赖时使用当前 requirements.lock 或项目安装命令，新增依赖为 python-docx、ReportLab、lxml。

从岗位页点击“选择经历并生成简历”，按“推荐项目（可选）→选择与排序→生成→编辑保存→确认版本→下载 Word/PDF”体验。基本信息与学历也可增减，只有已确认经历会出现在选择页。同一输入会复用已有生成；想调整内容时修改选择、顺序或写作偏好。

Word 为可编辑 DOCX。PDF 使用中文字体，本机 Windows 默认读取系统宋体并嵌入文件，不要求安装 Word/LibreOffice；其他平台需要相应中文字体。两种格式使用同一内容，可能有分页差异。离线演示文件带标记；正式投递前应核对使用实际模型生成的版本。

已确认历史版本不会随着后续档案或草稿修改而改变。档案变更后，旧草稿可编辑但不能确认为新版本，需要重新选用最新经历生成。
