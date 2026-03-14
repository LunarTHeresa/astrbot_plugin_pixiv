# AstrBot Pixiv 插件（含 R18）

一个可用于 AstrBot 的 Pixiv 插件，支持 QQ 指令获取普通/R18 插画和小说（小说以 txt 文件发送）。

## 功能

- 普通插画：`/pix 关键词`
- R18 插画：`/pixr 关键词`
- 普通小说（txt）：`/novel 关键词`
- R18 小说（txt）：`/novelr 关键词`

## 配置

在插件配置中填写：

- `pixiv_refresh_token`: 你的 Pixiv refresh_token
- `allow_r18`: 是否允许 R18 指令（true/false，默认 false）
- `pixiv_proxy`: Pixiv 代理地址（可选，例如 `http://127.0.0.1:7890`）
- `request_timeout_sec`: 请求超时秒数（默认 30，网络差可改 60）
- `send_image_as_file`: 图片是否先下载为本地文件再发送（默认 true，推荐）

## 常见问题

- 报错 `auth/token 400`：通常是 `pixiv_refresh_token` 已失效，请重新获取 token 后更新配置。
- 同标签重复作品：插件现已改为从匹配结果中随机返回一条，避免总是第一张。
- R18 搜索为空：插件会自动尝试追加 `R-18` 标签再检索；若仍无结果，可换更明确关键词。

> `refresh_token` 可用你目录中的 `get_pixiv_token_manual.py` 获取。

## 说明

- 插画优先发送原图 URL（AstrBot 支持图片 URL 时会直接发图）。
- 小说会抓取正文并保存为临时 txt 后发送。
- 若平台或适配器限制了文件发送，将自动降级为发送小说链接。
