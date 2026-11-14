# EmailBoom · 邮件重复发送系统

基于 **Flask + SQLite + Jinja2** 的邮件批量/重复发送系统。所有发送逻辑在**独立后台进程**中执行——关闭浏览器、停止 Web 服务都不影响任务按计划运行。

## 功能特性

- **多 SMTP 服务器**：可配置任意多个发信服务器，支持 SSL / STARTTLS / 明文，一键连接测试；任务级选择策略（轮询 / 随机 / 优先级 / 固定）与连续失败自动切换（failover），认证失败的服务器本轮自动跳过。
- **任务级精细配置**：
  - 速度：发送间隔（毫秒）、随机抖动、批次大小、批次间停顿、每小时上限；
  - 重试：最大重试次数、指数/线性/固定退避与基数、连接/发送超时、5xx 永久失败是否重试；
  - 安全阀：整体失败率熔断自动中止；去重模式（跳过历史已发送，失败者自动重新排队）；
  - 邮件内容：内置富文本编辑器（工具栏排版、变量插入、所见即所得 / HTML 源码切换）、纯文本正文、Jinja2 个性化变量（`{{ name }}`、`{{ email }}`、自定义列）、自定义邮件头、抄送/密送、附件（多文件）。
- **定时与重复执行**：手动执行 / 指定时刻一次执行 / 5 段 Cron 表达式（`分 时 日 月 周`，支持 `*`、`*/n`、`a-b`、`a,b`、英文月周名）/ 固定间隔重复；可设最大执行轮数。
- **独立后台进程**：调度器守护进程（文件锁单实例、优雅退出）每 2 秒扫描到期任务，为每个任务拉起独立 Worker 子进程执行；心跳超时/进程死亡自动崩溃恢复。
- **任务列表页**：统计概览、实时进度（3 秒轮询刷新）、运行/暂停/停止一键控制；任务详情页含实时日志控制台与收件人明细（状态/重试/错误过滤），状态变化就地更新、不整页刷新。
- **收件人**：文本粘贴（`邮箱,姓名,变量JSON`、`姓名 <邮箱>`、`#` 注释）或 CSV 上传（支持任意自定义变量列），按邮箱去重。
- **管理控制台**：登录认证（CSRF 保护）、系统设置页可修改密码、调度器状态查看与重启。

## 快速开始

```bash
pip install -r requirements.txt   # 仅 Flask，其余资源全部本地化，无 CDN
python app.py                     # http://127.0.0.1:5000
```

访问后使用管理员账号登录：

| 用户名 | 密码 |
|--------|------|
| `admin` | `admin1324` |

登录后请在「系统设置 → 修改密码」中立即更换默认密码。

启动 Web 服务时调度器会自动拉起；即使之后停止 `app.py`，已在运行的调度器与 Worker 仍会继续按计划发送。也可手动管理：

```bash
python -m core.runner.scheduler    # 手动启动调度器（文件锁保证单实例）
python -m core.runner.worker <ID>  # 手动运行某个任务
```

## 项目结构

```
├── app.py                     # 入口（调用 core 的应用工厂）
├── config.py                  # 配置（密钥/数据库/调度参数）
├── requirements.txt
├── core/                      # 应用核心
│   ├── app.py                 # 应用工厂、CSRF、上下文注入
│   ├── db.py                  # SQLite（WAL + busy_timeout + 线程本地连接）
│   ├── models/                # user / server / task / recipient / log
│   └── runner/                # 独立后台进程
│       ├── scheduler.py       # 调度器守护（flock 单实例、崩溃恢复）
│       ├── worker.py          # 每任务 Worker（限速/重试/多服务器/控制标志）
│       └── process.py         # 进程管理（拉起/探测/重启）
├── routes/                    # Web 蓝图：auth / tasks / servers / settings / api
├── services/
│   ├── mailer.py              # SMTP 发送、Jinja2 渲染、错误分类、退避
│   └── schedule.py            # 自写 5 段 Cron 解析与下次执行计算
└── static/
    ├── css/                   # fonts / tailwind / base / showcase / app（全本地）
    ├── fonts/                 # 49 个 woff2 字体文件
    ├── js/                    # lucide.min.js（本地全量图标）/ app.js
    └── templates/             # Jinja2 模板（base/login/tasks/servers/settings）
```

## 运行机制

```
浏览器（可随时关闭）
   │  表单 / 轮询 API
   ▼
Flask Web ──写──▶ SQLite（tasks / recipients / task_logs …）
                     ▲            │
   调度器守护进程 ──读/写───────────┤（2 秒轮询、到期派发、心跳恢复）
   （flock 单实例，脱离 Web 独立）   ▼
                            Worker 子进程（每任务一个）
                            限速 → 服务器池 → SMTP → 重试/退避
                            控制标志轮询（运行/暂停/停止）
```

- Web 与 Worker/调度器仅通过数据库交互，互不依赖；
- Worker 每封邮件前写心跳，调度器检测心跳超时或 PID 消失即自动恢复；
- 手动模式任务保存后进入待运行队列，调度器拉起 Worker 执行一轮；重复类任务（cron/interval）按计划循环，`dedupe` 开启时第二轮起自动跳过已发送者。

## 测试

开发阶段已通过端到端冒烟测试（Flask 测试客户端 + 本地模拟 SMTP 服务端）：

- 登录/登出/改密全流程、服务器 CRUD 与连接测试、任务创建与 Cron 校验；
- 调度器 → Worker → 模拟 SMTP 全链路：5 封发送成功 + 1 封 550 永久失败、个性化主题、自定义头、HTML 正文；
- 去重第二轮跳过、附件上传并以 `multipart/mixed` 发出（中文文件名保留）、Worker 独立日志与优雅停止。

## 安全说明

- 密码使用 `pbkdf2_sha256` 哈希存储，会话含 CSRF 保护；
- SMTP 授权码仅保存在本地 `data/emailboom.db`（已列入 `.gitignore`）；
- `data/` 目录存放数据库、附件与 Worker 日志，不会提交到仓库。
