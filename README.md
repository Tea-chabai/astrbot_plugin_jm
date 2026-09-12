# astrbot_plugin_jm

AstrBot 插件：使用 `/jm <编号>` 下载 JM 本子，转换为 PDF 后发送到当前会话。

## 功能

- `/jm <编号>` —— 下载指定编号的单章节本子，合并为 PDF 发送
- `/jmhelp` —— 查看帮助

两种编号都可以直接使用：

- 单章节作品的编号
- 多章节作品中某一章节的编号（会按「多章节」拒绝，见下）

多章节作品暂不支持，会直接回复「暂时无法输出多章节的本子」；
编号不存在或需要登录而当前未登录时回复「未查询到相关本子」。

## 安装

1. 把本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_jm/` 目录下
   （或在面板的插件市场安装）
2. 重载插件。AstrBot 会自动读取 `requirements.txt` 并把缺失的依赖装进
   它自己所在的 Python 环境，通常无需手动操作。
3. 若自动安装失败，再手动执行：

   ```bash
   pip install -r requirements.txt
   ```

### 在干净的 AstrBot 上部署

- **依赖会自动安装**：装的是 AstrBot 运行时那个解释器（`sys.executable`），
  不是系统 Python。依赖里 `pikepdf`、`lxml`、`curl-cffi` 都是二进制包，
  主流平台都有预编译 wheel，不需要编译器。
- **依赖被锁版本**：本插件用到了 jmcomic 的若干内部接口
  （`Img2pdfPlugin`、`JmModuleConfig.register_plugin`、`download_photo` 的
  返回结构等），所以 `requirements.txt` 里锁死了
  `jmcomic==2.7.6` / `img2pdf==0.6.3`，避免上游改接口后突然失效。
- **依赖缺失不会拖垮插件**：jmcomic 是按需导入的，即便没装上，插件照样能
  在面板里启用；只有真正执行 `/jm` 时才会回复
  「查询失败：缺少依赖 jmcomic 或 img2pdf，请执行：pip install -r requirements.txt」。
- **输出目录可用**：默认写在 `data/plugin_data/astrbot_plugin_jm/` 下，会在
  首次加载时自动创建。想让机器人和 AstrBot 分在不同盘，用 `output_dir` 指过去。
- **平台需支持发文件**：PDF 是通过消息文件段发送的，需要适配器支持
  （aiocqhttp、Telegram 等都支持）。只支持图片的平台发不出 PDF。
- **首次访问需要能连上 JM 站点**：插件会请求一次 JM 的 API 拿域名列表，
  网络不通会报查询失败。

## 在 OneBot v11（NapCat / SnowLuma）下的注意事项

本插件发 PDF 走的是 AstrBot 的 `aiocqhttp` 适配器，实际发出的是
`send_group_msg` / `send_private_msg` 里带 `{"type": "file", "data": {"file": "file:///...", "name": "xxx.pdf"}}`
的文件消息段，**不是** `upload_group_file` 这个独立 action。NapCat 与
SnowLuma 都实现了该消息段格式。

**该链路已在 SnowLuma 1.14.15 + AstrBot 4.27.4 + QQNT 上实测通过**：群聊中
发 `/jm 422866` 能正常下载并收到 PDF，多章节与不存在两条分支的回复也正常。

连接方式（AstrBot 侧是反向 WebSocket 服务端）：

- AstrBot：平台类型 `aiocqhttp`，`ws_reverse_host` / `ws_reverse_port`
  指定监听地址，`ws_reverse_token` 填 access token
- SnowLuma：在账号的 OneBot 配置里加一条 `wsClients`，`url` 指向
  `ws://<AstrBot 地址>:<端口>/ws`，`accessToken` 与上面一致，
  `role` 用 `Universal`，`messageFormat` 用 `array`

### 一个常见误解：机器人看不到自己发的命令

OneBot 实现默认**不上报自身消息**，所以机器人无法用 `/jm` 触发自己。
即使打开 SnowLuma 的 `reportSelfMessage`，AstrBot 也接不住 —— 它的
aiocqhttp 适配器只注册了 `on_message("group")` / `on_message("private")`
两个处理器，而自消息的 `post_type` 是 `message_sent`，匹配不上会被直接丢弃。
测试时请用**另一个账号**在群里发命令。

需要留意的两点：

1. **文件路径必须对 OneBot 实现可见。** AstrBot 是把本地绝对路径转成
   `file://` URI 交给 NapCat/SnowLuma 去读的，因此两者必须能访问到同一个
   文件。如果 AstrBot 和 NapCat 跑在不同的容器里，必须把插件的输出目录
   （默认 `data/plugin_data/astrbot_plugin_jm/`）挂载进 NapCat 容器，否则会
   发送失败或收到一个空文件。最稳妥的做法是两者同机部署。
2. **文件名已做保守处理。** PDF 文件名只保留字母数字、汉字和 `_ -` 空格，
   方括号、圆括号等会被替换成下划线 —— 这类符号在 QQ 客户端上容易导致
   文件名错乱或发送失败。

## 配置

在 AstrBot 面板的插件配置页填写（`_conf_schema.json` 定义了全部选项）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `output_dir` | 空 | PDF 输出目录。留空则用插件数据目录 |
| `username` / `password` | 空 | JM 账号。填了会先登录再访问，可查看需要登录的本子 |
| `cookies` | 空 | 也可直接填 `AVS=xxx; SESSION=xxx` 跳过登录 |
| `threads` | 8 | 图片下载线程数，建议 4~16 |
| `timeout` | 1800 | 单次下载超时（秒），长篇本子可调大 |
| `max_images` | 500 | 单本图片数量上限，超过则拒绝下载 |
| `delete_images` | true | 生成 PDF 后删除原始图片，节省磁盘 |
| `retry_times` | 3 | 接口请求失败的重试次数 |

> 账号密码、cookies 会明文保存在 AstrBot 的 `data/config/` 下，注意不要泄露该目录。

## 实现要点

- **多章节判定**：先取专辑详情 `get_album_detail()`，其 `episode_list` 长度大于 1
  即视为多章节。单章节作品再从 `episode_list[0]` 构造章节对象，
  这样既完成了校验，又避免了一次多余的请求。
- **PDF 生成**：注册了一个继承自 jmcomic `Img2pdfPlugin` 的自定义插件
  （`plugin_key = astrbot_jm_title_pdf`），覆写 `decide_filepath()` 让输出文件
  按本子标题命名，而不是默认的编号。标题会做 Windows 非法字符替换、保留名
  规避与长度截断。
- **不阻塞事件循环**：查询与下载都是同步阻塞的，统一放进
  `asyncio.to_thread()` 执行，并套一层 `asyncio.wait_for()` 做超时保护。
- **并发**：下载用 `asyncio.Lock` 串行化，避免多个下载同时进行时
  兜底查找挑错文件。

## 测试

`tests/run_test.py` 用假的 event 对象驱动插件的真实代码路径，不需要真正的
消息平台即可验证逻辑：

```bash
# 成功路径
JM_TEST_ID=422866 python tests/run_test.py

# 不存在的本子
JM_TEST_ID=999999999999 python tests/run_test.py

# 需要登录的本子
JM_TEST_ID=422866 JM_USER=你的账号 JM_PASS=你的密码 python tests/run_test.py
```

脚本会依次跑「指定编号 / 多章节 / 无参数 / 非数字」四个用例并打印结果。
