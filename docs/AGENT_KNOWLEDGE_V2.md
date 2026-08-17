# Agent 知识服务 V2

## 目标

在保留桌面知识生成能力的前提下，把会话、追问理解和最终回答交给 Agent。LocalTranscriber 只提供可验证、可组合、按需加载的知识与视频证据能力。

当前桌面版基线保存在 Git 提交 `2cda30d`。V2 不修改已有知识空间的事实模型；`.knowledge/sources/`、概念、主张、关系和视频时间定位仍是永久可信数据。

## 产品边界

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| GUI 管理端 | 注册/移除知识空间、导入视频、知识生成、任务恢复、异常处理 | Agent 会话和最终回答 |
| KnowledgeService | 空间隔离、懒加载缓存、检索、证据展开、关系查询、视频定位 | 模型调用和聊天历史 |
| MCP 适配器 | 将 KnowledgeService 暴露为跨 Agent 工具 | 保存全局当前空间 |
| Agent | 会话、指代消解、问题拆解、多轮检索、回答和引用 | 直接读取知识空间文件 |

## 知识空间身份与授权

- 每个知识空间在 `.knowledge/space.json` 保存稳定 `space_id`。
- GUI 选择目录时，将 `space_id -> 本机绝对路径` 写入 `%LOCALAPPDATA%/LocalTranscriber/knowledge-spaces.json`。
- Agent 只能访问注册表中启用的空间，不能传入任意文件系统路径。
- 对 Agent 返回空间名、领域、数量和更新时间；不返回知识空间绝对路径。
- 整体移动空间后，GUI 重新选择目录即可用原 `space_id` 更新注册位置。

## 加载规则

1. 程序和 MCP 启动只读取小型空间注册表。
2. `list_knowledge_spaces` 不读取知识索引。
3. 第一次查询某个 `space_id` 时，加载该空间的索引、可信证据、概念和关系。
4. 同一进程后续查询复用编译后的检索快照。
5. 索引、概念或关系文件版本变化时，只刷新受影响的缓存部分。
6. 不存在数据变化时禁止重新读取完整索引。

## V2 工具契约

### `list_knowledge_spaces()`

返回已由 GUI 授权的空间摘要，不加载任何知识内容。

### `get_space_catalog(space_id)`

返回领域、视频来源和概念目录，用于 Agent 缩小检索范围。

### `search_knowledge(space_id, query, limit, domain, source_id, include_related)`

返回结构化候选证据、匹配字段、稳定 ID 和视频时间定位；不生成最终答案。

### `get_evidence(space_id, evidence_id)`

返回单条永久可信原文和定位器。

### `expand_evidence_context(space_id, evidence_id, before, after)`

按同一视频的时间顺序返回相邻可信片段，避免只看到孤立句子。

### `get_related_concepts(space_id, concept_id, limit)`

返回证据支持的直接概念关系。默认不进行无界图遍历。

### `open_video_evidence(space_id, evidence_id)`

在本机独立播放器中打开证据视频并跳转到起始时间。绝对路径不进入工具结果。

## 会话和并发

- 插件没有“当前知识空间”全局变量；每次工具调用必须携带 `space_id`。
- 不同 Agent 会话可同时查询不同空间。
- 读操作可并行；知识生成继续受现有单程序/单写入保护。
- MCP 工具不得把聊天消息写入知识空间。
- Agent 必须使用工具返回的 `evidence_id` 作为事实引用；证据不足时不得用模型常识补齐。

## 兼容和发布

- 桌面核心继续支持 Python 3.9–3.12。
- 官方 MCP Python SDK 2.x 要求 Python 3.10+，因此使用独立 `requirements-mcp.txt`，不增加普通桌面安装重量。
- GitHub `main` 保留桌面版基线；V2 在 `codex/agent-knowledge-v2` 开发并单独提交。
- DeepSeek Harness 首版通过 MCP 使用；只有需要 Harness 专用会话节点或 UI 时，才增加薄原生插件。

## 验收标准

1. 未选择知识空间时，启动不读取 20 MB 索引。
2. GUI 选择空间后，Agent 可列出该空间但看不到绝对路径。
3. 同一空间连续检索只加载一次未变化索引。
4. 两个 KnowledgeService 实例可并发只读查询同一空间。
5. Agent 可搜索、展开上下文、查询关系并打开正确视频时间点。
6. 未注册、禁用或伪造 `space_id` 均被拒绝。
7. 现有知识生成、迁移、增量 Obsidian 和单实例保护回归通过。
8. MCP 工具列表与逐项调用通过自动化测试。
