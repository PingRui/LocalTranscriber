# 安全策略

## 报告问题

请不要在公开 Issue 中提交 API Key、完整转写内容、私人媒体路径或可识别个人身份的信息。仓库公开后，维护者应在 GitHub 的 **Settings → Security** 中启用 private vulnerability reporting；安全漏洞通过 **Security → Report a vulnerability** 私密报告。普通缺陷可以使用公开 Issue，但必须先移除敏感数据。

## 数据边界

- 音视频识别默认在本机执行。
- 来源网址功能会访问用户提供的公开页面。
- 远程 OpenAI 兼容校订是可选云端功能，会把转写文本和来源上下文发送到用户配置的接口。
- 模型 API Key 可明文保存在 `%LOCALAPPDATA%\LocalTranscriber\model-providers.json`；请保护当前 Windows 账号，并且不要复制、提交或分享该文件。
- 本地 Qwen 校订和课程问答只允许连接 `127.0.0.1`、`localhost` 或 `::1` 的 HTTP 服务。
- 本地课程索引保存在用户状态目录，只包含校订文本、时间戳和原视频路径，不复制视频。
- 批量清洗只读取用户选择目录中的结构化原始转写，并在原文件旁写出独立校订稿；不会覆盖原稿，也不会自动更新知识库。
- API Key 仅通过当前转写子进程的环境变量传递，不应写入配置、日志或输出。

## 支持范围

当前为 Alpha 阶段，只对最新源码提供安全更新。请不要在来源不可信的共享电脑上保存 API Key。
