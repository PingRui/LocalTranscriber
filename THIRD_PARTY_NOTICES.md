# 第三方组件说明

LocalTranscriber 使用以下主要第三方组件。各组件继续受其自身许可证约束。

| 组件 | 用途 | 许可证 |
|---|---|---|
| faster-whisper | 本地语音识别 | MIT |
| CTranslate2 | 模型推理 | MIT |
| pywebview | Windows 桌面 WebView 界面 | BSD-3-Clause |
| PyAV / FFmpeg libraries | 音视频解码 | BSD-3-Clause 及其包含组件的适用条款 |
| huggingface-hub | 模型下载 | Apache-2.0 |
| Whisper 模型权重 | 本地语音模型 | MIT，具体以对应模型卡为准 |
| Lucide Icons | 界面图标 | ISC；部分图标源自 Feather，MIT |

Lucide 的完整许可文本保存在 `ui/vendor/LUCIDE-LICENSE`。

CUDA 与 cuDNN 不属于本项目源码，也不应被项目许可证覆盖。用户如需 GPU 加速，应按 NVIDIA 官方文档自行安装并遵守 NVIDIA 的适用许可条款。
