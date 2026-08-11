# 参与贡献

感谢参与 LocalTranscriber。

## 本地开发

1. 使用 Windows 10/11 和 Python 3.9–3.12。
2. 运行 `安装.cmd` 创建环境并选择模型。
3. 修改后运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile app_config.py model_manager.py transcribe.py gui.pyw llm_repair.py source_context.py
```

## 提交边界

不要提交以下内容：

- `.venv`、模型权重、CUDA/cuDNN DLL。
- 音视频、完整转写稿、来源页面缓存和运行日志。
- API Key、浏览器配置、Cookies 或个人本地路径。
- 没有再分发授权的字幕、节目转写稿或测试语料。

准确率修复应保留原始结果，并为新的判断规则增加针对性测试。不要通过放宽现有复核或失败条件来隐藏问题。
