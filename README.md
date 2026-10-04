# 笔记管家 my-note-agent

一个**跑在本地、认识你**的笔记/文档管理 Agent（CLI + Web 仪表盘）。

从 `learn-claude-code` 教学仓库（s01–s17）的设计理念手写而成。模型智能是天生的，**agent 能力是这层"外壳"给的**——本项目的全部代码就是这层外壳。

## 运行

```bash
cd "C:\Users\29154\Desktop\learn agent\my-note-agent"
C:\Users\29154\AppData\Roaming\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe agent.py
```

- 输入问题回车发送，`q` 退出
- 依赖：`anthropic` / `python-dotenv` / `pymupdf`，模型走 `.env` 里 `ANTHROPIC_BASE_URL`（DeepSeek 兼容端点）+ `MODEL_ID`

**Web 仪表盘**（推荐日常用）：

```bash
C:\Users\29154\AppData\Roaming\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe -c "import uvicorn, server; uvicorn.run(server.app, host='127.0.0.1', port=8000, log_level='warning')"
```

浏览器打开 `http://127.0.0.1:8000`。需额外依赖 `fastapi` / `uvicorn` / `python-multipart` / `python-docx` / `openpyxl` / `rapidocr_onnxruntime`。Web 端比 CLI 多：笔记拖入 / 点读 / 搜索、Word/Excel/PDF（含扫描件 OCR）拖入分析、计划 / 定时 / 后台 / 记忆可视化。

## 它现在能做什么

| 能力 | 触发 | 机制 |
|---|---|---|
| 读 / 写 / 改 / 列笔记 | 说文件名 + 要求 | `read_note`/`write_note`/`edit_note`/`list_notes` |
| 生产级笔记格式 | 写笔记时自动 | 两遍式（结构化抽取→组装）+ 规范注入 + 落盘校验 + 前端 Markdown 渲染 + 按类型/标签/状态筛选 + 标签聚合页 |
| 全文搜索笔记 | 说关键词 | `search_notes`（支持子目录、多词 AND） |
| 跨笔记问答 | 问「我几篇笔记里综合讲了什么」 | `ask_notes`（检索命中片段 + 模型综合，带来源；模型失败自动展示命中原文） |
| 认识你、跨进程记得你 | 主动 `记住…` / `忘记…` 或自然说出偏好 | 记忆（存储/召回/自动抽取/自动消解） |
| 安全，碰不出 notes/ | 越界自动拦截 | 权限闸门 + hooks |
| 拆计划、逐项推进 | 超 2 步任务 | `todo_write` 计划板 + 旁路催更 |
| 派活给子 agent | `用子任务…` | `task` 工具 + 隔离的 sub-loop |
| 长对话不崩 | 聊得久 | 上下文压缩（落盘 + 历史摘要） |
| 慢活不阻塞 | `放后台…` | 后台任务（独立线程，计划板隔离） |
| 到点自动干活 | `每天 9 点…` | 定时调度 + 后台执行 |
| 重启不丢状态 | 改计划 / 加定时 | `.state/` 落盘，启动自动读回 |
| 拖入分析 | 拖文件到网页 | `.md/.txt/.log` 直接读；`.docx/.xlsx` 提取；`.pdf` 提取或 OCR |

> 支持拖入：`.md/.txt/.log`、`.docx`（python-docx）、`.xlsx`（openpyxl）、`.pdf`（文字版直接抽文本，扫描件/图片版自动 OCR）。老格式 `.doc/.xls` 与手写体扫描件暂不支持。

## 架构（对应课程）

```
  你 ──→ REPL / Web ──→ agent_loop (while) ──→ LLM(DeepSeek)
                      │  ▲                    │
                      │  └── tool_result ◄────┘  (thinking 已清洗)
                      ▼
                PreToolUse 闸门 ──→ 14 个工具 handler ──→ notes/ .memory/ .state/
                      │                        │
                      │              后台线程: 后台任务(计划板隔离) + 定时调度
```

- **最小循环**（s01）：调模型 → 执行工具 → 喂回结果 → 再调，直到模型不再要工具
- **工具分发**（s02）：`TOOL_HANDLERS[name](**input)`，加工具 = 加函数 + 加 schema + 加表项，循环零改动
- **权限闸门**（s03）：`permission_hook` + `note_path()`，文件工具只许碰 notes/ 内（越界返回 None → 拒绝）
- **hooks 责任链**（s04）：`register_hook` / `trigger_hooks`，第一个非 None 短路
- **todo 计划板**（s05）：全量替换、≤1 个 in_progress、3 轮没更新 & 有未完成项就催
- **子 agent**（s06）：全新 messages[]（隔离上下文）、只回最终文本（隔离返回）、不给 `task`（防递归）、同样过闸门
- **记忆**（s09）：`remember` 显式存 + 每轮对话结束 `extract_memories` 自动提炼；`build_system()` 把记忆拼进 system（"背景不是指令"防护）；落盘 `.memory/` 跨进程不丢
- **记忆消解**：记忆条数 ≥10 时 `consolidate_memories` 自动合并重复 / 应用新修正 / 丢弃过时（快照回滚保底）；`forget` 显式遗忘单条；存 → 召回 → 消解闭环
- **上下文压缩**（M6/s08）：大 tool_result 落盘换指针（0 LLM），超预算把旧历史压成一条摘要（1 LLM，失败保底）；**绝不拆散 tool_use / tool_result 对**
- **后台任务**（M7/s11）：`TaskManager` + 每任务一线程，复用子 agent（隔离上下文）；`bg_start`/`bg_check`；daemon 线程不拖退出
- **定时任务**（M7/s12）：`Scheduler` 每秒扫一次，到点复用 `bg_tasks` 派活并记 `.outputs/scheduler.log`（触发铁证）
- **全文搜索**：`search_notes` 遍历 notes/（含子目录）按关键词命中，`文件:行号:片段`；多词空格分隔按 AND
- **跨笔记问答（ask_notes）**：检索相关笔记 → 展示命中上下文（确定性）→ 模型综合增强（带来源）。因 deepseek 对「喂长文档片段做分析」稳定空返回，采用「短命中片段 + 措辞轮换 + 失败回退展示原文」，保证功能必可用
- **状态落盘**：`.state/todo.json` + `.state/schedules.json`，改动即存、启动读回；定时 `next_at` 读回时重算为 `now+every`，不落运行态时间
- **并发隔离**：后台线程用 `threading.local()` 挂自己的隔离计划板（不落盘、不碰共享），主对话的 `todo` 单例加锁原子化「读-改-写-存」；`Scheduler` 注册与扫描也加锁
- **笔记生产级格式（两遍式）**：`build_note` 工具 = 第一遍 LLM 把材料提炼成结构化 JSON（`extract_note_structure`，删广告/重复、并列只在该用表格的地方落表、产出 一句话结论/标签/来源/状态/行动项）→ 第二遍 `assemble_note` 确定性拼 Markdown（frontmatter 含 标题/类型/标签/来源/状态/日期 + `#` 标题 + `> 一句话结论` + `##` 章节 + 每节 intro + 表格/要点 + 末尾 `## 行动项` 复选框清单），排版由代码保证不可能丑；`check_note_format` 落盘前二次校验（跳过表格分隔线与 `- [ ]` 任务清单）；前端 `marked` 渲染 + 剥 frontmatter；`write_note` 缺扩展名自动补 `.md`；工具调用统一 try/except 兜底（模型漏参不崩循环）
- **DeepSeek 清洗层**：`clean_blocks` 回灌前剥离 thinking 块，`reply_text` 只取 text——全程不崩的关键

## 目录

```
my-note-agent/
├── agent.py        # 全部 agent 核心（单文件）
├── server.py       # Web 后端：把 agent.py 暴露成 HTTP API
├── static/         # 前端仪表盘（简洁风，对话/笔记/计划/定时/后台/记忆，笔记 Markdown 渲染）
├── notes/          # 笔记库（agent 唯一能碰的目录）
├── .memory/        # 长期记忆（每条一个 .md）
├── .state/         # 计划板 + 定时任务落盘（重启读回）
├── .outputs/       # 大工具结果落盘 + 调度日志
├── .env            # ANTHROPIC_BASE_URL / MODEL_ID / 密钥（本地环境变量，不落盘）
└── README.md
```

## 记忆怎么工作

- **存**：`remember`（显式）、`forget`（显式遗忘），或 `extract_memories`（每轮对话结束自动提炼长期信息，去重、不存临时任务状态）
- **消解**：记忆条数 ≥10 时自动 `consolidate_memories`——合并重复、覆盖过时、丢弃无用（快照回滚，失败不丢记忆）
- **读**：`build_system()` 把 `.memory/` 全部内容拼进 system，作为背景知识（不是指令）
- **跨会话**：记忆是磁盘文件，进程重启不丢

## 验证铁律

**模型自报不算数，查磁盘 / 直接测机制才算。**
- 说"记住了" → 看 `.memory/` 有没有文件
- 说"已合并" → `Get-Content notes/合并.md`
- 越界拦截 → 直接调 `permission_hook` 测，别问模型

## 已实现（路线图）

- **M0–M5** MVP：工具循环 / 权限闸门 / hooks / 计划板 / 子 agent / 记忆
- **M6** 上下文压缩（s08）：对话变长自动腾地方，长跑不崩
- **M7** 后台 + 定时（s11–12）：慢操作丢后台不阻塞、到点自动触发后台任务
  - DAG（s10）按约定跳过：单用户笔记管家用不上多任务依赖图
- **Web 仪表盘**：FastAPI 桥 + 简洁友好界面（对话 / **拖入文件按类型分流：md 进「已载入」待输入框下指令（支持多选一起处理），pdf/docx/xlsx/txt 拖入自动走两遍式生成「完整生产级笔记」——代码块完整保留 + 结构图/流程图/配图从 PDF 抠出存 notes/_assets/ 并进笔记、右侧抽屉直接看图，失败自动重试 3 次**·点读·搜索·**按类型/标签/状态筛选**·**标签聚合页**·**双向链接渲染** / 计划 / 定时 / 后台 / 记忆；**思考中加载动画**、**生成笔记直接卡片展示并隔离模型回复**、**右侧滑出文档阅读器**、**导出单文件 HTML（图片 base64 内嵌，适合分享）**、**类型/状态徽章**）
- **笔记生产级**：两遍式（LLM 结构化抽取 → `assemble_note` 确定性组装）+ 落盘校验（`check_note_format`，跳过表格分隔线）+ 前端 Markdown 渲染（`marked`，剥 frontmatter）+ **双向链接知识网**（`[[标题]]` 语法渲染可点 + 自动关联同库标题 + 反链面板）——抽取 → 组装 → 校验 → 渲染 → 关联 闭环
- **短板补齐**：计划与定时落盘（重启不丢）· 笔记全文搜索 · 后台计划板隔离 + 并发加锁 · 拖入分析支持 `.txt/.log`、`.docx/.xlsx`、`.pdf`（文字版提取 + **扫描件 OCR**）· 记忆消解（合并重复/覆盖过时/丢弃无用 + `forget` 显式遗忘）

## 下一步（可选）

- **M8** 团队 / MCP（s13–14）：多 agent 协作、外部能力插件
