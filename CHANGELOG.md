# 开发日志

## 0.2.0

- 简化配置项：移除 `file_server_host`、`file_server_port`、`public_base_url`，改为自动检测
- 优化文件命名：PDF 输出使用时间戳命名（`report_时间戳.pdf`），源文件使用 `source_` 前缀
- 数据目录适配 Docker：优先使用 `/AstrBot/data/pdf_reports`，本地环境回退到系统临时目录
- 明确模型默认值：`text_api_url` 默认 DeepSeek，`llm_model` 默认 `deepseek-chat`，`llm_vision_model` 默认 `qwen-vl-max`
- 简化 `fallback_models` 和 `fallback_vision_models` 配置说明

## 0.1.0

- 创建 `astrbot_plugin_xelatex_pdf` 插件骨架
- 添加 AstrBot `@register` 插件注册
- 添加 `/ai`、`/ask`、`/解答`、`/解析` 命令入口
- 添加消息解析结构：文本、Reply、图片、PDF
- 添加图片 / PDF 附件本地化处理
- 添加 XeLaTeX 编译入口
- 添加编译失败日志摘要返回
- 添加最近 5 轮会话记忆骨架
- 添加本地开发环境 fallback，便于脱离 AstrBot 做语法检查

## 待办

- 在 AstrBot Docker 容器内安装 TeX 编译链
- 使用真实 AstrBot 事件对象测试 `/ai`
- 完善 PDF 文件 HTTP 服务回传方式
- 完善图片路径、远程图片下载和 TeX 编译兼容性
- 完善 PDF 内容提取或 OCR 流程
- 接入真实 LLM 内容生成链路
