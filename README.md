# 视频知识空间

这是一个 Windows 本地桌面工具，用来把视频整理成可供 Agent 检索、并能回到原视频核对的个人知识库。

软件只保留两个主要入口：

- **生成知识**：选择视频或递归扫描文件夹，完成专业词汇确认、全文转录、可信校对、知识整理和写入。
- **Agent 接入**：在 GUI 中授权知识空间，再由 Codex、DeepSeek Harness 或其他支持 MCP 的 Agent 负责多轮会话、检索和回答。

## 最终会得到什么

你选择的目录就是一个完整、可移动的知识空间：

```text
知识空间/
├─ 视频/                 处理时复制的视频副本
├─ Obsidian知识库/       由可信索引投影出的双链知识图谱
│  ├─ 知识地图.md        领域、视频来源和概念的总入口
│  ├─ index.md           自动维护的知识导航
│  ├─ log.md             按视频记录的知识增长日志
│  ├─ 领域/              每个领域的知识分支
│  ├─ 概念/              可跨视频汇聚的独立概念笔记
│  └─ <领域>/            每个视频的来源总览与时间证据
├─ .knowledge/           永久可信证据、概念、主张、关系和编译规则
├─ knowledge-index.jsonl 可重建的兼容检索投影与视频时间证据
└─ .work/                可在验收后清理的临时过程文件
```

没有 SQLite 数据库。`.knowledge/sources/` 中的永久可信转录与视频时间定位是证据事实源；概念、原子主张和关系负责跨视频积累知识。`knowledge-index.jsonl` 和 Obsidian Wiki 是可重建的检索与阅读投影。同一概念会根据规范名称和别名跨视频汇聚，并保留每个来源的视频时间证据。移动整个知识空间后，相对路径仍然有效；如果视频被单独移走，可以通过文件指纹重新关联。

## 使用方法

1. 运行 `安装.cmd` 完成首次安装，然后运行 `开始本地转写.cmd`。
2. 左下角打开“设置”，填写 OpenAI 兼容接口的 Base URL、模型和 API Key，点击“测试连接并保存”。
3. 在“生成知识”中选择知识空间。
4. 选择单个或多个视频，或者扫描一个文件夹下的全部视频。
5. 开始任务。视频先复制到知识空间，原文件不会被移动或删除。
6. 系统本地转录少量样本，自动判断专业词汇并在后台辅助分类。无法分类不会阻塞任务；只有多数专业词候选缺少样本依据时才需要你确认词汇。
7. 等待全文转录、可信校对和知识写入完成。
8. 打开“Agent 接入”查看已授权空间。首次使用时运行 `安装Agent接入.cmd`，再复制页面中的 MCP 配置到你的 Agent。
9. 在 Agent 中选择空间并提问；Agent 可反复检索、展开可信原文，并从证据时间点打开本地视频。
10. 如需查看关系图谱，直接在 Obsidian 中将 `知识空间/Obsidian知识库` 作为 Vault 打开；从 `知识地图.md` 进入，或使用 Obsidian 的关系图谱查看完整网络。LocalTranscriber 只负责生成文件，不负责启动 Obsidian。
11. 验收后点击“清理临时文件”。这只删除本次 `.work` 过程文件，不删除视频、永久可信转录、知识主张、索引或 Obsidian Wiki。

## 处理路径

```text
复制视频
  → 本地转录少量样本
  → 模型建议专业词汇，分类仅作为后台辅助信息
  → 依据样本证据和可用的历史词库自动确认
  → 仅专业词证据异常时等待用户确认
  → Whisper 全文转录
  → 整文件可信校对
  → 将完整可信转录永久写入 .knowledge/sources
  → 生成带 evidence_id 和视频时间的原子主张
  → 复用概念别名、累积跨视频证据并更新关系
  → 生成可重建检索投影、来源页、概念页、index.md 和 log.md
```

不同视频不需要预先有关联，也不要求用户填写课程或分类。系统会复用已有概念、记录别名，并把相同主张的多个视频证据汇聚起来。Agent 检索同时覆盖已编译知识和完整可信转录；即使模型生成知识时遗漏某段内容，可信原文仍可召回并跳转对应视频时间。

## Agent 接入

普通桌面安装不包含 MCP 依赖，避免增加不使用 Agent 时的安装体积。需要接入 Agent 时运行：

```powershell
.\安装Agent接入.cmd
```

然后在桌面程序中打开“Agent 接入”，复制 MCP 配置。每个知识空间都有稳定 `space_id`；Agent 的每次查询都必须显式指定空间，不使用会串会话的全局“当前空间”。Agent 只能看到 GUI 已授权空间的名称、数量和知识内容，看不到任意本机目录，也不能传入绝对路径访问其他文件。

对外工具包括空间列表、空间目录、知识检索、证据读取、相邻原文展开、概念关系和视频时间点播放。LocalTranscriber 不保存 Agent 的聊天历史，也不生成最终回答；会话上下文、追问理解和回答由 Agent 自己管理。

MCP 要求 Python 3.10 或更高版本。桌面核心仍支持 Python 3.9–3.12；如果现有虚拟环境是 Python 3.9，需要重新用较新 Python 安装后才能启用 Agent 接入。详细契约见 [docs/AGENT_KNOWLEDGE_V2.md](docs/AGENT_KNOWLEDGE_V2.md)。

任务会在每个阶段保存断点。软件退出、任务取消或某个视频失败后，点击“从断点继续”即可复用已经复制的视频、样本分析、完整转录、可信校对和已发布知识，不会让整个批次从头再来。批次中某个视频需要人工确认时，其余视频仍会继续处理。

同一知识空间会在隐藏目录 `.knowledge/domain-hotwords.json` 中维护按内容分类隔离的热词学习记录。只有校对阶段实际接受的修改才能影响词库；连续多个视频的专业词校对率足够低后，该分类进入稳定状态，后续主要检查样本里的新增词，不再反复生成整套热词。这个文件由软件维护，不需要手工编辑。

## 隐私与网络边界

- 视频和音频只由本地 Whisper 读取，不上传到模型接口。
- 专业词汇判断、全文校对和知识整理会把必要的转录文本发送到你配置的 OpenAI 兼容接口。
- Agent 检索通过本机 MCP 服务读取已授权知识；Agent 本身如何发送问题和证据，由你使用的 Agent 及其模型配置决定。
- API Key 保存在 `%LOCALAPPDATA%\LocalTranscriber\model-providers.json`，不会写入知识空间、结果、日志或页面状态。
- 重要内容仍应通过回答提供的视频时间证据进行人工核对。

## 本地模型

支持 `medium` 和 `large-v3-turbo`。CPU 可以运行但速度较慢；NVIDIA GPU 模式需要本机驱动和相应运行库。

命令行转录入口仍可独立使用：

```powershell
.\.venv\Scripts\python.exe .\transcribe.py --model medium "D:\video.mp4"
```

## 开发与验证

```powershell
.\verify.ps1
```

验证会编译主要 Python 入口、运行不依赖真实模型/API 的自动化测试，并检查 Git 差异格式。

主要模块：

```text
gui.pyw                 桌面状态、知识空间授权与完整任务编排
ui/                     生成知识、空间管理与 Agent 接入界面
knowledge_service.py    Agent 中立的懒加载检索、可信证据与视频定位服务
localtranscriber_mcp.py MCP 工具适配器
evidence_player.py      独立视频证据播放器
knowledge_space.py      文件化知识空间、JSONL、Obsidian 与视频关联
knowledge_pipeline.py   可恢复的阶段编排、自动确认与异常转人工
domain_hotwords.py      分类热词学习、证据门槛与稳定度判断
knowledge_worker.py     样本转录和专业词汇分析进程
transcribe.py           本地 Whisper 全文转录
whole_file_review.py    整文件可信校对
llm_client.py           OpenAI 兼容接口客户端
```

详细边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 许可证

源代码采用 [MIT License](LICENSE)。第三方组件许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
