# astrbot_plugin_xelatex_pdf

AstrBot 的 XeLaTeX PDF 生成插件，使用 XeLaTeX 作为 PDF 渲染后端，支持中文排版。

## 功能特性

- 支持 `/ai`、`/ask`、`/解答`、`/解析` 命令触发 PDF 生成
- 支持文本、Reply 消息、图片、PDF 文件作为输入
- 内置 HTTP 文件服务，生成的 PDF 可直接通过链接访问
- 支持多模型切换和备用模型自动降级
- 支持视觉模型（OCR）识别图片内容
- 自动保存最近 5 轮会话记忆
- 编译失败时返回错误日志摘要

## 命令列表

| 命令 | 别名 | 说明 |
|------|------|------|
| `/ai <内容>` | `/ask`、`/解答`、`/解析` | 触发 AI 分析并生成 PDF |
| `/查模型` | - | 查看当前可用模型列表 |
| `/ai_source` | `/源文件` | 导出当前会话的 LaTeX 源文件 |

## 配置项

在 AstrBot 插件管理页面中配置以下参数：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `text_api_url` | 逻辑大脑接口地址（OpenAI 兼容） | `https://api.deepseek.com/v1` |
| `text_api_key` | API 密钥 | - |
| `llm_model` | 文本对话模型 | `deepseek-chat` |
| `llm_vision_model` | 视觉识别模型 | `qwen-vl-max` |
| `fallback_models` | 备用文本模型列表（逗号分隔） | - |
| `fallback_vision_models` | 备用视觉模型列表（逗号分隔） | - |

## 运行环境要求

插件运行环境需要安装 TeX 编译链：

```bash
# Ubuntu/Debian
apt-get install texlive-xetex texlive-lang-chinese latexmk

# 需要中文字体（如 SimSun、SimHei、FangSong 等）
```

## 数据存储

- **Docker 环境**：数据存储在 `/AstrBot/data/pdf_reports/`
- **本地环境**：数据存储在系统临时目录的 `astrbot_plugin_xelatex_pdf/` 下

生成的 PDF 文件和会话历史均保存在数据目录中。

## 安装方式

1. 在 AstrBot 插件管理页面中选择「从 GitHub 安装」
2. 输入仓库地址：`https://github.com/25456434337346646/astrbot_plugin_xelatex-pdf`
3. 安装后在配置页面填写 API 密钥和模型信息

## 开发日志

详见 [CHANGELOG.md](CHANGELOG.md)

## 相关链接

- [AstrBot 官方文档](https://astrbot.app/)
- [AstrBot 插件开发指南](https://astrbot.app/plugin/)
