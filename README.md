# astrbot_plugin_xelatex_pdf

AstrBot 的 XeLaTeX PDF 插件骨架。

## 目标

- 使用 XeLaTeX 作为 PDF 渲染后端
- 兼容 `/ai`、`/ask`、`/解答`、`/解析`
- 支持文本、Reply、图片、PDF 输入
- 支持把生成的 PDF 作为文件回传

## 当前状态

这是一个最小可运行骨架，已完成：

- 插件注册与初始化骨架
- 命令入口骨架
- 消息输入解析骨架
- 图片 / PDF 本地化处理骨架
- XeLaTeX 编译入口骨架
- 编译失败日志摘要返回
- 会话记忆骨架

## 运行要求

插件运行时所在环境需要可用的 TeX 编译链，例如：

- `xelatex`
- `latexmk`
- `texlive-lang-chinese`
- 中文字体

## 说明

当前仓库主要用于继续完善 AstrBot 中的 XeLaTeX PDF 生成流程。
