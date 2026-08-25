# astrbot_plugin_xelatex_pdf

AstrBot 的多模态问答 PDF 插件。它接收文本、引用消息、图片或 PDF，调用
OpenAI 兼容的文本/视觉模型生成结构化答案，再通过 XeLaTeX 和 ElegantBook
渲染为中文 PDF。

## 功能

- `/ai`、`/ask`、`/解答`、`/解析` 多个命令入口
- 解析普通附件及 QQ Reply 中的文本、图片和 PDF
- PDF 转图片后交给视觉模型，失败时回退到本地文本提取/OCR
- 文本模型和视觉模型均支持备用模型自动降级
- 文档标题与内容提要由模型结构化返回（`[TITLE]`/`[SUMMARY]` 标记），非硬编码
- 将 Markdown、公式和常用 ElegantBook 环境转换为可编译文档
- 保存最近 5 轮会话，并支持导出上一次生成的 LaTeX 源文件
- 通过内置 HTTP 文件服务向消息适配器回传 PDF

## 命令

| 命令 | 说明 |
| --- | --- |
| `/ai <问题>` | 生成回答并编译 PDF；别名为 `/ask`、`/解答`、`/解析` |
| `/ai clear` | 清空当前会话历史 |
| `/ai -m <模型>` | 切换当前文本模型 |
| `/查模型` | 查询接口可用模型 |
| `/ai_source` | 导出当前会话的 LaTeX 源文件；别名为 `/源文件` |

也可以发送“切换到 `<模型>` 模型”或“使用 `<模型>` 模型回答”。

## 配置

| 配置项 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `text_api_url` | 是 | `https://api.deepseek.com/v1` | OpenAI 兼容的文本接口根地址 |
| `text_api_key` | 是 | 空 | 文本接口密钥 |
| `llm_model` | 是 | `deepseek-chat` | 首选文本模型 |
| `fallback_models` | 否 | 空 | 逗号分隔的备用文本模型 |
| `ocr_api_url` | 否 | `text_api_url` | 独立的视觉接口根地址 |
| `ocr_api_key` | 否 | `text_api_key` | 独立的视觉接口密钥 |
| `llm_vision_model` | 图片/PDF 输入时 | `qwen-vl-max` | 首选视觉模型 |
| `fallback_vision_models` | 否 | 空 | 逗号分隔的备用视觉模型 |
| `file_server_host` | 否 | 空（自动用容器主机名） | 下载链接使用的 host；容器内插件+宿主机机器人时填 `127.0.0.1` 并映射端口 |
| `file_server_port` | 否 | `8765` | 内置文件服务端口 |

密钥只应保存在 AstrBot 插件配置或环境变量中，不要提交到仓库。

## 运行环境

- AstrBot `>=4.16,<5`
- Python 3.10+
- `xelatex` 和 ElegantBook 所需的 TeX Live 宏包
- 中文字体
- PDF 输入建议安装 PyMuPDF，或提供 `pdftoppm`/`pdftotext`
- 本地 OCR 回退需要 `tesseract` 及相应语言包

Debian/Ubuntu 可按需安装（ElegantBook 派生类还需要 `tex-gyre`/`fonts-texgyre` 字体、`extarrows`、`bbding`、`adforn`、`biblatex` 等，已含在下表）：

```bash
apt-get install texlive-xetex texlive-lang-chinese texlive-latex-extra \
  texlive-pictures texlive-fonts-recommended texlive-science \
  texlive-fonts-extra texlive-bibtex-extra tex-gyre fonts-texgyre \
  fonts-noto-cjk poppler-utils
```

> Docker 部署提示：容器内 `apt` 安装的依赖在容器重建后会丢失，建议把上述安装
> 固化进自定义镜像（`FROM soulter/astrbot:latest` + `RUN apt-get install ...`）。

插件在 Docker 中使用 `/AstrBot/data/pdf_reports/` 保存产物和会话历史；
其他环境使用系统临时目录。内置文件服务监听 `8765` 端口，部署时需确保消息
适配器能够访问容器返回的地址。

## 命名与文件管理

所有产物统一采用「QQ号_题目关键词_时间戳」命名，并按类别分目录存放：

```text
pdf_reports/
├── assets/    # 消息附件（图片/PDF 本地化副本）
├── sources/   # LaTeX 源码（.tex）
├── logs/      # xelatex 编译日志（.log）
├── pdfs/      # 最终 PDF 产物（.pdf）
└── chat_history.json
```

同一次问答的源码、日志、PDF 共用同一命名基名（例如
`123456789_证明根号2是无理数_20260825_125123`），保证三类文件一一对应。
内置文件服务按分类路由：`/pdf/{filename}`、`/source/{filename}`、`/log/{filename}`。

## 安装

在 AstrBot 插件管理页面选择从 GitHub 安装，并填写：

```text
https://github.com/25456434337346646/astrbot_plugin_xelatex-pdf
```

仓库内附带基于 ElegantBook 4.3 修改并按 LPPL 要求改名的
`astrbot-elegantbook.cls`，其来源、修改说明和许可证见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 开发验证

```bash
ruff check main.py tests
python -m compileall -q main.py tests
python -m unittest discover -s tests -v
```
