# astrbot_plugin_xwdraw

AstrBot 小维绘图插件。对接 `https://sd.loping151.com` 的远端 SD 绘图服务，覆盖普通用户可用接口：图片生成、图生图、R18/R18G 自审、预设、自定义预设、画廊查询、图片 metadata/标签、视频生成、公告、队列、推荐提示和文档下载。

> 本插件不开放服务端 admin 账号创建、更新、删除、重置用量等高风险接口。

## 功能特性

- 文生图 / 图生图：`来点`、`小千来点`、`xwdraw`
- 当前群/会话总开关：群管理员可使用 `绘图开启`、`绘图关闭`
- 生成后 R18 自审：服务 metadata + 可插拔外部视觉审核接口
- 服务状态：账号配额、生成配置、公告、队列、推荐提示
- 预设管理：角色、风格、服装、自定义预设、预设详情、预设图片
- 图库能力：最近图片、图片 metadata、R18/R18G 标签更新、画廊列表和筛选项
- 视频能力：图生视频、历史、视频文件、缩略图、末帧、源图
- 文档能力：词条/法典类文档下载或文本预览

## 配置

在 AstrBot 插件配置页填写：

- `api_url`：生成接口地址，默认 `https://sd.loping151.com/api/generate`
- `api_key`：Bearer Token，必填
- `timeout`：生成、下载、视频和文档请求超时时间，默认 `60`
- `plugin_enabled`：绘图插件默认总开关，默认 `true`
- `group_admin_can_toggle`：是否允许群管理员控制当前群/会话开关，默认 `true`
- `switch_admin_user_ids`：额外开关管理员用户 ID，英文逗号分隔

R18 自审默认开启：

- `r18_review_enabled`：是否启用发送前自审，默认 `true`
- `r18_block_r18`：是否拦截 R18，默认 `true`
- `r18_block_r18g`：是否拦截 R18G，默认 `true`
- `r18_nsfw_score_threshold`：`nsfw_score` 拦截阈值，默认 `0.65`
- `r18_fail_without_metadata`：缺少 `nsfw_score/is_r18/is_r18g` 等自审 metadata 时是否保守拦截，默认 `true`
- `r18_allowed_session_ids`：逗号分隔的放行会话列表，默认空

外部审核默认关闭：

- `external_review_enabled`：是否启用外部审核接口，默认 `false`
- `external_review_protocol`：外部审核协议，`auto/openai/custom`，默认 `auto`
- `external_review_api_url`：外部审核接口地址；OpenAI 兼容中转站可填写 `https://example.com/v1`
- `external_review_api_key`：外部审核 Bearer Token，可选
- `external_review_model`：外部审核模型名，可选
- `external_review_timeout`：外部审核超时，默认 `30`
- `external_review_max_tokens`：OpenAI 兼容外审最大输出 Token，默认 `300`
- `external_review_fail_closed`：外部审核失败时是否保守拦截，默认 `true`
- `test_echo_image_enabled`：`测试来点` 是否允许回显输入图，默认 `false`

`external_review_protocol=auto` 时，如果地址以 `/v1` 或 `/chat/completions` 结尾，会自动使用 OpenAI-compatible Chat Completions：插件请求 `/chat/completions`，发送 `messages + image_url`，并从 `choices[0].message.content` 中解析 JSON。

自定义外部审核接口或 OpenAI-compatible 模型回复都应给出 JSON 对象，例如：

```json
{
  "safe": false,
  "level": "r18",
  "score": 0.91,
  "reason": "adult content"
}
```

其中 `score` 应表示不安全/NSFW 风险分，`0` 为安全、`1` 为高风险，不应表示“安全置信度”。

外审返回非 JSON、空响应、HTML 或接口错误时，`external_review_fail_closed=true` 会拦截图片。若同时缺少服务端 metadata，`r18_fail_without_metadata=true` 也会拦截图片；关闭该策略会降低安全性。

## 图片生成

```text
来点 <提示词>
小千来点 <提示词>
xwdraw <提示词>
```

附带图片时自动走图生图：

```text
[图片] 来点 add more details -d 0.6
```

支持标记生成内容：

```text
来点 1girl, solo --r18
来点 horror scene --r18g
```

说明：

- `--r18`、`--r18g` 会随生成请求传给服务端，用于画廊标签/metadata。
- 发送图片前仍会执行本插件自审。默认策略下 R18/R18G 或 `nsfw_score >= 0.65` 不会发出。
- `-d <0.0-1.0>` 仅用于图生图去噪强度。
- `-s`、`-c`、`-w`、`-h`、`-m` 等高级参数保留在提示词中，由远端服务解析。

测试输入图获取：

```text
测试来点 <提示词>
```

`测试来点` 默认只提示已检测到图片，不回显原图，避免用户借测试命令绕过自审回传敏感图片。如开启 `test_echo_image_enabled`，回显前仍会执行同一套自审流程。

## 总开关

```text
绘图状态
绘图开启
绘图关闭
绘图开关 开
绘图开关 关
```

说明：

- 开关作用于当前群/当前会话，关闭后会拦截图片生成、预设、图库、视频、文档等功能。
- `绘图帮助` 和 `绘图状态/绘图开关` 始终可用，方便查看和恢复。
- 默认允许群主/群管理员控制；如果平台无法识别群管身份，可在 `switch_admin_user_ids` 中配置额外用户 ID。
- 开关状态会保存到插件数据目录的 `switches.json`，重启后仍然生效。

## 服务状态

```text
绘图账号
绘图配额
生成配置
绘图公告
绘图队列
推荐提示
```

## 预设

```text
预设列表
角色列表
风格列表
服装列表
预设详情 <character|style|costume> <名称>
预设图片 <类型> <图片名>
服装预览 <名称>
```

自定义预设：

```text
添加预设 <名称>|<内容>
删除预设 <名称>
我的预设
```

## 图库

```text
最近图片 [数量] [--r18] [--r18g]
图片元数据 <日期>/<文件名>
更新图片标签 <日期>/<文件名> r18=true r18g=false
画廊列表 [页码] [关键词] [--r18] [--r18g]
画廊筛选
```

默认查询会排除 R18/R18G。需要包含对应内容时显式传 `--r18`、`--r18g` 或 `--all`。

## 视频

视频生成需要附带起始图片：

```text
[图片] 视频生成 <提示词> [-t 秒] [-fps 帧率] [-n 负面提示词]
```

其他视频接口：

```text
视频历史
视频查看 <timestamp> <username>
视频缩略图 <timestamp> <username>
视频末帧 <timestamp> <username>
视频源图 <timestamp> <username>
```

如果当前 AstrBot 平台不支持直接发送视频/文件，插件会把文件保存到插件数据目录并回复路径。

## 文档

```text
绘图文档
绘图文档 涩涩词条大全
绘图文档 常规法典
绘图文档 色色法典
```

文本类文档会直接预览前部内容；二进制文档会保存或尝试作为文件发送。

## 测试

本仓库的 `test_plugin.py` 使用本地 AstrBot stub 和假客户端，不需要真实 `api_key`：

```bash
python test_plugin.py
```

## 更新日志

### v0.3.0

- 重构 API client、生成结果解析、图片发送和 R18 自审流程
- 新增 R18/R18G metadata 审查和可插拔外部审核接口
- 补齐账号、生成配置、公告、队列、推荐提示、画廊、图片标签、视频、文档等普通用户接口命令
- 修复 README 中存在但代码缺失的 `生成配置` 命令
- 替换需要真实 token 的测试脚本为本地无网络测试

### v0.2.0

- 新增预设管理功能
- 新增自定义预设功能
- 新增帮助系统
- 修复图片下载地址问题

### v0.1.0

- 初始版本
- 支持基础文生图和图生图
