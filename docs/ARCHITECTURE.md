# 架构与数据边界

## 产品边界

LocalTranscriber 是本地优先的“视频 → 知识 → Agent → 视频证据”工具，不是通用数据库管理器，也不再承担 Agent 的会话系统。主流程不使用 SQLite，也不要求用户理解中间转录文件。

界面只有三处：生成知识、Agent 接入、设置。GUI 是知识管理控制面；Agent 负责会话、追问理解、多轮检索和最终回答。

## 主数据流

```text
用户选择视频或文件夹
  → 递归发现与路径去重
  → 按内容指纹复制到 知识空间/视频
  → 本地 Whisper 预转录有限样本
  → OpenAI 兼容模型建议专业词汇并提供后台辅助分类
  → 本地规则核对样本证据，并在分类可用时读取对应历史热词
  → 分类不确定不阻塞任务；只有专业词证据异常时才请求用户确认
  → 本地 Whisper 从头完成全文转录
  → 模型对整文件找错，本地规则保守应用修改
  → 已接受的专业词修正反馈到该分类的热词记录
  → 只接收 knowledge_ready=true 的可信分段
  → 将完整可信转录和 video_time 定位器永久写入 .knowledge/sources
  → 模型生成带 segment_id 证据的原子主张、概念别名和明确关系
  → 程序复用已有概念、汇聚跨视频证据并写入 claims/relations
  → 从永久知识生成可重建 JSONL、知识导航、增长日志和 Obsidian 双链
  → GUI 将用户选中的空间注册为 Agent 可用空间
  → Agent 通过 MCP 按 space_id 懒加载检索已编译知识和完整可信原文
  → Agent 按需展开相邻原文、查询概念关系，并打开视频起始秒数
```

## 文件结构

```text
知识空间/
├─ 视频/
├─ Obsidian知识库/
│  ├─ 知识地图.md
│  ├─ 领域/
│  │  └─ <模型判断的领域>.md
│  ├─ 概念/
│  │  └─ <知识点标题>.md
│  ├─ <模型判断的领域>/
│  │  └─ <视频 Wiki 标题>.md
│  └─ .obsidian/
│     ├─ core-plugins.json
│     └─ graph.json
├─ knowledge-index.jsonl
├─ .knowledge/
│  ├─ schema.md
│  ├─ space.json
│  ├─ concepts.json
│  ├─ claims.jsonl
│  ├─ relations.jsonl
│  ├─ sources/<video-id>/
│  │  ├─ source.json
│  │  ├─ transcript.raw.json
│  │  ├─ transcript.verified.json
│  │  └─ evidence-units.jsonl
│  └─ domain-hotwords.json
└─ .work/
   └─ tasks/<task-id>/
```

`.knowledge/sources/` 中的可信转录与 `evidence-units.jsonl` 是证据事实源。每个证据单元都有稳定 `evidence_id`、`source_id`、可信原文和 `video_time` 定位器，验收清理不能删除。

`.knowledge/space.json` 保存稳定 `space_id`。GUI 选择目录时，才把 `space_id` 与本机路径写入用户目录下的轻量注册表；MCP 启动和空间列表不读取知识索引。Agent 每次调用都必须显式传入 `space_id`，因此多个 Agent 窗口可以同时查询不同空间，不共享会串话的全局当前空间。

`.knowledge/concepts.json` 保存规范概念和别名；`claims.jsonl` 保存可跨视频累积来源的原子主张；`relations.jsonl` 保存证据支持的概念关系。`knowledge-index.jsonl` 每行保留兼容知识单元、视频相对路径和时间证据，但它是可从永久知识重建的检索投影，不再是唯一事实源。

Obsidian 不维护第二份知识事实。发布时程序从知识模型重建关系投影：`知识地图/index.md → 领域 → 视频来源 → 概念`，`log.md` 记录每次视频入库涉及的知识变化。规范名称和别名可以跨视频汇聚到同一个概念，每条内容和时间证据仍按原视频分别保留。关系图和反向链接使用 Obsidian 内置插件，不要求安装第三方插件。

`.knowledge/domain-hotwords.json` 是软件维护的分类热词状态，不是需要用户管理的数据库。候选词只在样本中有明确词形和依据时进入本次转录；词条经过多个视频和可信校对后才会从候选提升为可复用或稳定。连续三个视频累计至少 10 次词形出现、专业词修正率不高于 5%，且新增词不超过 1 个时，该分类被视为已稳定。不同分类互不复用热词。

`.work/tasks/<task-id>/task.json` 是断点事实源。复制、样本分析、全文转录、可信校对和发布均通过已落盘且可校验的成果决定是否跳过；重启只把未完成的运行状态转换为“可继续”，不会删除已完成成果。人工确认也写回原任务，而不是创建新任务。

最终成果只依赖相对路径，因此整个知识空间可以移动。视频单独缺失时，重新选择文件后先校验内容指纹，再复制回 `视频/` 并更新索引。

## 模块职责

| 路径 | 职责 |
|---|---|
| `gui.pyw` | pywebview 桥接、任务状态机、空间授权、子进程编排、暂停/继续/取消和设置 |
| `ui/` | 生成知识页、Agent 接入管理页和设置弹窗 |
| `knowledge_service.py` | Agent 中立的空间隔离、懒加载缓存、结构化检索、证据展开、关系和视频定位 |
| `localtranscriber_mcp.py` | 将 KnowledgeService 暴露为跨 Agent 的 MCP 工具 |
| `evidence_player.py` | 从可信证据时间点打开本地视频的独立播放器 |
| `knowledge_space.py` | 知识空间初始化、媒体发现/复制/指纹、任务临时目录、可信发布、JSONL 与重新关联 |
| `knowledge_pipeline.py` | 单视频隔离、阶段断点、自动确认、异常排队和同任务继续 |
| `domain_hotwords.py` | 按分类隔离的热词证据、升降级、校对反馈与稳定度 |
| `knowledge_worker.py` | 单视频样本转录和专业词汇建议 |
| `transcribe.py` | Whisper 推理和结构化全文转录 |
| `whole_file_review.py` | 整文件模型校对、本地风险规则、可信结果输出 |
| `task_hotwords.py` | 样本文字、内容分类和专业词汇结构 |
| `llm_client.py` | OpenAI Chat Completions 兼容客户端与 JSON 输出解析 |
| `model_provider_config.py` | 用户目录中的接口配置持久化 |

`batch_clean.py`、`llm_repair.py` 和 `trusted_pipeline.py` 是底层/命令行能力，桌面主流程不再把它们暴露成分散页面。

## 一致性与安全约束

- 复制视频使用“文件大小 + 头尾内容”的 SHA-256 指纹去重。
- 所有 JSONL、任务状态和 Markdown 都先写临时文件，再执行原子替换。
- Obsidian 页面只投影永久知识中的可信内容；概念合并不改变 source_id、evidence_id、原视频或时间证据。
- 只有 `status=applied` 的可信校对记录能够成为热词学习依据；重复恢复同一视频不会重复累计证据。
- 批次中的热词异常只暂停该视频，其余视频继续，最终逐个集中确认。
- Wiki 只引用当前可信分段的 ID；模型伪造或越界的证据 ID 会被丢弃。
- Agent 只能访问 GUI 注册的 `space_id`，不能传入绝对路径读取任意本机目录。
- 检索结果只返回稳定 ID、内容摘要、文件名和时间定位，不返回知识空间绝对路径。
- Agent 应先检索再按 `evidence_id` 读取可信原文；证据不足时不能用模型常识伪装成知识库结论。
- 清理函数只允许删除 `.work/tasks/task-*` 的精确任务目录。
- 源视频永不由程序删除；复制中断只清理 `.copying` 临时文件。
- API Key 不进入前端快照、知识索引、Obsidian 文件或子进程命令行，只通过环境变量传给需要的进程。

## 网络边界

- Whisper 全程本地读取音视频。
- 专业词汇建议发送少量转录样本。
- 可信校对发送单视频完整转录文本。
- Wiki 生成发送可信分段。
- MCP 服务使用本机 stdio，不监听网络端口。Agent 如何把问题和检索证据发送给模型，由 Agent 自己的配置决定。
- 视频二进制、本机绝对路径和知识空间目录结构不发送到模型接口。

## 验证

统一入口：

```powershell
.\verify.ps1
```

重点回归覆盖：递归扫描、去重复制、可信证据永久化、别名概念合并、跨视频主张累积、知识遗漏时的原文召回、空间授权隔离、懒加载缓存、并发只读检索、MCP 协议调用、视频时间定位、旧索引无模型迁移、知识空间整体移动、视频重新关联、任务目录安全清理、设置密钥隔离和文件选择器空路径错误。
