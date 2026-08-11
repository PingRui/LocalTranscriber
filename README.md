# LocalTranscriber 本地语音转写

LocalTranscriber 是面向 Windows 的本地视频/音频批量转写工具，支持递归扫描、断点续跑、Markdown/SRT/JSON 输出、来源上下文和可选的 DeepSeek 校订。

音视频识别由本机 Whisper 模型完成，不上传原始媒体。只有主动开启 DeepSeek 校订时，转写文本和来源上下文才会发送至 DeepSeek API。

> 当前阶段：Alpha。自动转写可能产生专有名词、数字和否定词错误，重要内容请人工复核。

## 快速安装

支持 Windows 10/11，建议使用 Python 3.9–3.12。首次安装需要联网下载依赖和约 1.5GB 的本地模型。

1. 下载或克隆源码。
2. 双击 `安装.cmd`。
3. 安装向导会检查 Windows、Python、内存、磁盘、WebView2 和 NVIDIA GPU。
4. 根据提示选择模型：
   - `Medium`：约 1.43GB，CPU 用户推荐。
   - `Large-v3 Turbo`：约 1.51GB，准确率更高，NVIDIA GPU 用户推荐。
5. 安装完成后，双击桌面的“本地语音转写”或项目中的 `开始本地转写.cmd`。

模型、历史记录和运行配置保存在 `%LOCALAPPDATA%\LocalTranscriber`，不会写入源码仓库，也不需要上传到 GitHub。

### CPU 与 GPU

- CPU 模式不要求 NVIDIA 环境，安装后即可使用，但长视频处理较慢。
- GPU 模式需要 NVIDIA 驱动、CUDA 12 和 cuDNN 9。
- 安装向导不会把 CUDA/cuDNN 复制进源码。GPU 依赖缺失时，默认的 `auto` 模式会回退到 CPU。

## 主要功能

- 同时添加多个视频或音频。
- 递归扫描多层文件夹并自动去重。
- 单文件失败不会中断整个批次。
- 支持取消、暂停、继续和跳过已有完整结果。
- 生成 Markdown、TXT、SRT 和结构化 JSON。
- 可为每个文件绑定独立来源网址，提取标题、人物和规范词汇。
- 可选 DeepSeek 上下文校订，原始结果与校订结果分别保留。
- 支持 Medium 与 Large-v3 Turbo 两套本地模型，结果不会互相覆盖。

## 使用方法

1. 在“上传视频”页面选择文件或递归选择文件夹。
2. 如有来源页面，可为每个文件填写 YouTube、Bilibili 等公开网址。
3. 在设置中选择语言、计算设备、输出位置以及是否启用 DeepSeek 校订。
4. 开始任务后，可在“内容”中查看状态和进度。
5. 转写完成后可查看准确稿、原始转写和校订记录。

默认在源文件旁创建“转写结果”文件夹，也可以统一保存到指定目录。同名文件会自动增加序号，避免覆盖。

## 输出文件

每个源文件至少生成：

- `.md`：带时间戳的 Markdown 转写稿。
- `.txt`：完整纯文本。
- `.srt`：字幕文件。
- `.json`：包含时间、置信度和复核原因的结构化结果。

启用 DeepSeek 校订后会额外生成 `.llm.*` 文件和 `.llm-corrections.json`。涉及数字或否定词变化的修改会标记为需要复核。

## 隐私与网络边界

- 本地转写：音视频只由本机 Whisper 模型读取。
- 模型安装：首次安装会从 Hugging Face 下载用户选择的模型。
- 来源上下文：填写网址后，程序会访问该公开页面并缓存标题、简介和规范词汇。
- DeepSeek 校订：启用后会发送转写分段、相邻上下文和来源信息；API Key 只传给当前子进程，不写入历史或结果文件。

## 命令行

安装完成后可以直接调用：

```powershell
.\.venv\Scripts\python.exe .\transcribe.py --model medium "D:\video.mp4"
```

来源上下文示例：

```powershell
.\.venv\Scripts\python.exe .\transcribe.py "D:\video.mp4" --model large-v3-turbo --language en --source-url "https://www.youtube.com/watch?v=VIDEO_ID"
```

模型管理：

```powershell
.\.venv\Scripts\python.exe .\model_manager.py status
.\.venv\Scripts\python.exe .\model_manager.py install --model large-v3-turbo
```

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile app_config.py model_manager.py transcribe.py gui.pyw llm_repair.py source_context.py
```

提交代码前请确认没有加入模型、CUDA DLL、虚拟环境、音视频、转写结果或 API Key。参见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。

## 许可证

项目源码采用 [MIT License](LICENSE)。第三方组件继续受其自身许可证约束，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
