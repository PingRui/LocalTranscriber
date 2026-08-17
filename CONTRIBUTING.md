# 参与贡献

## 本地开发

1. 使用 Windows 10/11 和 Python 3.9–3.12。
2. 运行 `安装.cmd` 创建环境并选择本地模型。
3. 修改后运行：

```powershell
.\verify.ps1
```

这个入口与 GitHub Actions 使用相同的编译、单元测试和差异格式检查。

## 代码结构

- `gui.pyw`、`ui/`：桌面任务编排与界面。
- `knowledge_space.py`：文件化知识空间、JSONL/Obsidian 发布、检索和视频证据。
- `knowledge_worker.py`、`transcribe.py`：样本分析与本地全文转录。
- `whole_file_review.py`、`llm_repair.py`：可信校对与保守修改校验。
- `llm_client.py`：OpenAI 兼容接口。
- `tests/`：不依赖真实模型下载或真实 API 的自动化回归。

完整数据流见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 提交边界

不要提交：

- `.venv`、模型权重、CUDA/cuDNN DLL；
- 音视频、知识空间、完整转录稿和运行日志；
- API Key、浏览器配置、Cookies 或个人本地路径；
- 没有再分发授权的字幕、节目转录稿或测试语料。

准确率修复应保留原始结果并增加针对性测试，不要通过放宽可信校验或失败条件隐藏问题。新增知识字段必须保持旧 JSONL 可读取，新增清理能力必须有精确目录边界测试。
