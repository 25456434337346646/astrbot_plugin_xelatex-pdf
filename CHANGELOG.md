# 开发日志

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
