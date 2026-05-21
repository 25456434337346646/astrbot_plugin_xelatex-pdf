# 旧 PDF 插件分析清单

> 记录对象：AstrBot 容器内旧插件 `astrbot_plugin_multimodal_pdf_router`
> 目的：为新插件 `astrbot_plugin_xelatex_pdf` 的迁移提供接口与行为对照。

## 必须兼容

- `/ai` 主命令
  - 入口：`main.py:567`
  - 负责文本 / 图片 / PDF / 引用消息的统一处理
- `/ask`、`/解答`、`/解析`
  - 都是 `/ai` 的别名
- `/ai clear`
  - 清空当前会话记忆
  - 旧逻辑使用 `session_id` 隔离群组和用户
- 图片输入
  - 需要继续支持消息中的 `Image` 组件
  - 旧插件会把图片转成 base64 后送入视觉模型
- PDF 文件输入
  - 需要继续支持消息中的 `File` 组件
  - 旧插件会把 PDF 下载或读取后转图片，再做 OCR
- 回复 / 引用消息
  - 需要继续支持 `Reply` 场景
  - 旧插件会从 `Reply.chain` 或底层 API 里提取被引用文本、图片、PDF
- 文件回传方式
  - 旧插件通过 `yield event.chain_result([File(name=..., url=http_url)])` 回传 PDF
  - 新插件建议保持同样的返回方式，避免破坏 llbot / AstrBot / QQ 的现有下载链路
- HTTP 文件服务
  - 旧插件在容器内启动 `8765` 端口提供 `/pdf/{filename}`
  - 新插件如果要保持兼容，建议继续保留文件服务
- 会话记忆
  - 旧插件保留最近 5 轮对话
  - 记忆文件：`chat_history.json`
  - 清理命令和上下文隔离建议保留
- 模型切换
  - 旧插件支持 `/ai -m 模型名`
  - 也支持自然语言模型请求，例如“使用 xxx 模型回答”
  - 新插件建议继续支持
- 模型查询
  - `/查模型` 用于查看可用模型列表
  - 新插件可保留为调试命令

## 可选兼容

- `/ai_source`
  - 导出上一轮生成前的源文件
  - 旧插件当前导出的是 HTML 源码
  - 新插件可改成导出 `.tex`，但保留命令更方便调试
- 格式清洗层
  - 旧插件有 `format_cleaner_enabled` 等配置
  - 若新插件仍依赖 LLM 输出结构化内容，可保留类似清洗层
- 备用模型列表
  - `fallback_models`
  - `fallback_vision_models`
  - 可保留，提升容错
- OCR 能力
  - 若新插件也要支持图片和扫描 PDF，建议保留
  - 若首版只做纯 PDF 编译，也可延后

## 建议重构

- PDF 渲染后端
  - 旧插件使用 HTML + MathJax + Playwright
  - 新插件目标应切换为 XeLaTeX 编译链
- 裸 LaTeX 修复逻辑
  - 旧插件的 `_fix_bare_latex()` 是为 HTML 渲染准备的
  - 换到 XeLaTeX 后应重写为 TeX 安全处理
- PDF 生成与发送耦合
  - 旧插件把“生成 PDF”和“发送文件”写在同一个流程里
  - 新插件建议拆成：内容生成 → TeX 模板 → 编译 → 交付
- 输出格式
  - 旧插件要求 LLM 输出 JSON + HTML 内容
  - 新插件应改成更适合 XeLaTeX 的结构化输入
- 旧的 HTML 源码缓存
  - 新插件更适合缓存 `.tex` 源码和编译日志

## 关键文件对照

- 旧插件主入口：`/AstrBot/data/plugins/astrbot_plugin_multimodal_pdf_router/main.py`
- 旧插件配置：`/AstrBot/data/plugins/astrbot_plugin_multimodal_pdf_router/_conf_schema.json`
- 旧插件说明：`/AstrBot/data/plugins/astrbot_plugin_multimodal_pdf_router/README.md`
- 旧插件网络说明：`/AstrBot/data/plugins/astrbot_plugin_multimodal_pdf_router/NETWORK_CONFIG.md`
- 新插件占位目录：`/root/astrbot_plugin_xelatex_pdf`

## 迁移优先级建议

1. 先保留交互入口和文件发送方式
2. 再保留图片 / PDF / 引用消息支持
3. 再迁移会话记忆与模型切换
4. 最后替换渲染后端为 XeLaTeX
