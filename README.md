# alipay-devtools-mcp

支付宝小程序开发者工具（「小程序开发者工具」）的 MCP 服务，对标微信侧的 `wechat-devtools-mcp`。

让 Claude Code / Cursor / ZCode 这类 Agent 能直接**构建、预览、驱动模拟器、看日志、截屏、读源码**支付宝小程序。

---

## 为什么是自己写的

支付宝官方**没有**把 IDE 包成 MCP，也没有对等的「IDE MCP」（官方公开的 MCP 只有支付侧的 `@alipay/mcp-server-alipay`，以及给商家做多端分发的 AI 开放平台）。

但支付宝侧的能力其实都在，只是没被封装：

| 能力 | 载体 | 状态 |
|---|---|---|
| 构建 / 预览 / 上传 / 提审 | 官方 CLI **`minidev`** | ✅ 现成，已封装为 `alipay_build` |
| 模拟器调试与自动化 | IDE 内建 **CDP over WebSocket** | ✅ 现成，已实测量明 |
| IDE 生命周期 | `minidev ide` / 进程控制 | ✅ 已封装 |

所以本服务 = **`minidev`（官方 CLI）+ IDE 的 CDP 调试通道（模拟器自动化）**。

---

## 安装

```bash
cd /Users/nk/Desktop/Ecology/alipay-devtools
uv sync
```

依赖：`mcp`、`websockets`、`pillow`（pillow 只用于把整屏帧裁成小程序可视区）。运行时还需要：

- **`minidev`**（`npm i -g minidev`，本机验证 v2.1.7）
- **「小程序开发者工具」** 已安装（默认探测 `/Applications/小程序开发者工具.app`）

### 两套校验

```bash
# 1) 纯逻辑，不需要 IDE（裁剪、CLI 参数拼装、项目解析、路径越界）
uv run python tests/selfcheck.py

# 2) 端到端，需要 IDE 里项目正在运行
ALIPAY_PROJECT_PATH=/path/to/miniprogram uv run python tests/live_check.py

# 3) 环境体检（不开 MCP，只打印发现结果与连通性）
ALIPAY_PROJECT_PATH=/path/to/miniprogram uv run alipay-devtools --doctor
```

## 注册到 MCP 客户端

`~/.zcode/cli/config.json` 的 `mcp.servers`：

```json
{
  "alipay-devtools": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--directory", "/Users/nk/Desktop/Ecology/alipay-devtools", "alipay-devtools"],
    "env": {
      "ALIPAY_PROJECT_PATH": "/Users/nk/Desktop/project/bc-alipay-app",
      "ALIPAY_APP_ID": "2021004156616335"
    }
  }
}
```

环境变量都可选：

| 变量 | 用途 |
|---|---|
| `ALIPAY_PROJECT_PATH` | 默认项目根目录（省略后每次都要显式传 `project`） |
| `ALIPAY_APP_ID` | `preview` / `upload` / `audit` 的必填 appId |
| `ALIPAY_IDE_APP_PATH` | IDE 安装路径（非标准位置时用） |
| `ALIPAY_MINIDEV_CLI` | minidev 可执行文件路径 |
| `ALIPAY_SHOTS_DIR` | 截图落盘目录，默认 `~/.alipay-devtools/screenshots` |
| `ALIPAY_IDE_CDP_PORT` / `ALIPAY_IDE_CDP_TOKEN` | 强制指定端点（自动发现失效时的逃生口） |

---

## 工具集

7 个聚合工具，命名与 `wechat-devtools-mcp` 一一对应，便于两边对照。

### `alipay_ide` — IDE 生命周期

`status`（是否在跑 / 端点 / 项目 / minidev 版本 / 连接与分层）、`discover`、`open`、`start_sim`、`login`、`quit`。

### `alipay_build` — 构建与发布（封装 minidev）

`build`、`preview`（真机预览二维码）、`upload`、`pack`、`audit`、`cancel_audit`、`dev`（DevServer）、`remote_debug`、`setup_build`、`clean_cache`、`download_assets`、`create`。

其它旗标用 `extra_flags` 透传，如 `{"minify": true, "typescript": true}`。

### `alipay_automator` — 驱动模拟器（核心）

| action | 说明 |
|---|---|
| `start` / `status` | 连接自检：模拟器 target、logic/view/top 三层、坐标偏移、页面栈 |
| `evaluate` | 在指定层执行 JS（默认 `logic`，可 `view` / `top`） |
| `find` | 按 `selector` / `text` 找元素（两者可叠加），返回坐标、可见性、是否在视口内 |
| `tap` | 点击（`selector`/`text`/`index` 或显式 `x,y`） |
| `input` | 输入文本（先点击聚焦，再派发真实输入） |
| `scroll` | 视图层滚动 |
| `page_stack` / `page_data` / `set_data` / `call_method` | 页面栈、页面数据、调 `setData`、调页面方法 |
| `navigate` | 跳转（tabBar 页自动改用 `switchTab`） |
| `system_info` / `storage` / `set_storage` | 系统信息与本地存储 |

**坐标约定**：`x`/`y` 用**视图层坐标系**（iPhone 模拟器里就是 `375x623` 那套视口 CSS px），服务内部换算成模拟器顶层坐标后派发真实输入事件。

### `alipay_inspector` — 日志与网络

`console` / `exceptions` / `network` / `all` / `clear`，支持 `duration` 采集时长、`level`、`keyword` 过滤、`detail_level="full"` 附原始 CDP 事件。

### `alipay_screenshot` — 截图

`mode="view"` 裁出小程序可视区（375x623，日常看页面用这个），`mode="full"` 整幅模拟器画面。返回值里带 `capture` 字段说明走了哪条通路、有没有裁成功。

### `alipay_navigate` — 跳转并抓该过程日志

跳页面 + 等待渲染 + 返回该过程的控制台输出与页面 data，可选同时截图。用来抓 `onLoad`/`onShow` 阶段日志。

### `alipay_file` — 项目源码

`project_info`（app.json 概览、分包、tabBar、appId 推断）、`list_pages`、`read_page`（一次给 `.axml`/`.js`/`.acss`/`.json`）、`read_file`、`search`。

---

## 反向工程结论

这部分是踩过的坑。探针脚本都在 `probe/`，可随时重跑复验。

### 1. 端点有两种来源，优先用 Electron 标准那个

**首选：`DevToolsActivePort`。** IDE 作为 Electron 应用会把 browser 级 CDP 端点写进
`~/Library/Application Support/小程序开发者工具/DevToolsActivePort`，两行内容：

```
51890
/devtools/browser/1fb05c86-046b-4a27-8896-95b2d1623076
```

连 `ws://localhost:51890/devtools/browser/<uuid>` 即可，**不需要 token**，而且不依赖任何面板是否打开。
（该端点的 HTTP `/json` 会因 Host 校验被拒，但 WS 能连；Host 只认 `localhost` 或 IP。）

**回退：从调试面板进程命令行解析。** 「模拟器调试」面板的渲染进程命令行里带着：

```
.../mini-ide-emulator-devtools-frontend/index.html
    ?ws=localhost:64835&env=kaitian&_token=<uuid>&project=<name>&projectId=<hash>
```

`_token` 是必需的：带 token 能拿到响应，不带则握手后立刻被 `1003 illegal url` 断开。
缺点是**这个进程只在调试面板打开时才存在**，面板一关就发现不到。

**端口每次启动 IDE 都会变**（实测 `52177` → `64835`），所以每次连接前都要重新发现，不能缓存。

### 2. 小程序藏在 `Simulator` target 里

连上后 `Target.getTargets` 能看到 IDE 自己的窗口（设置页、NPS 调查、Alipay GateWay 等）。
要拿小程序得 `Target.attachToTarget` 到标题含 **`Simulator`** 的 webview（`Lyra Simulator`）——
IDE 自己的调试面板也是这么连的。

### 3. 分层要靠行为，不能靠 URL 或「有没有 document」

模拟器内部有多个执行上下文，实测踩到的坑：

- **逻辑层是 appx worker，但它被实现成一个 iframe**（`file:///appx-ng/af-appx.worker.min.js`），
  所以它**也有 `document`**（只是只有 ~11 个节点）。用「有 document」当视图层判据会误判。
- 正确判据：**能调 `getCurrentPages` / 有 `my` 的就是逻辑层**。本服务就是这么分的。

| 层 | 帧 URL 特征 | 能力 |
|---|---|---|
| logic | `file:///appx-ng/af-appx.worker.min.js` | `my`、`getCurrentPages()`、`getApp()`、页面 `data`、storage |
| view | `.../index.html#<route>` | DOM（首页约 485 节点，能读到渲染出的商品文案） |
| top | 模拟器外壳 | 设备框、坐标基准 |

### 4. 视图层是「一页一帧」，必须按当前路由选

模拟器会为**每个页面实例**保留一个视图帧——退到后台的页面帧依然活着。
实测同时存在 3 个渲染过的页面帧（首页、分类、以及之前跳转过的页面），
按上下文顺序挑会挑到**已经不在前台的旧页面 DOM**，表现为「明明看得到元素却找不到」。

所以本服务每次按逻辑层的当前路由去匹配帧 URL；匹配不上时退回比节点数（渲染出页面的那个最多）。

### 5. 交互必须派发真实输入事件，且坐标要换算

- ❌ **视图层里 JS 合成 `MouseEvent` 无效**：实测派发后小程序毫无反应（文本长度与页面栈都不变）。
- ✅ **`Input.dispatchMouseEvent` 有效**：实测点首页一处，页面栈从 `pages/index/index` 变成 `pages/index/classify/classify`。

坐标这一串坑，都是一路踩出来的：

1. `getBoundingClientRect()` 给的是**视口相对**坐标，别再叠加 `scrollY`——多加了会让屏幕外元素算出「看起来合法」的坐标，点了没反应。
2. **屏幕外的元素要先弄进视口再量**。实测 `查看更多` 在 `y=673` 而视口只有 623 高，直接点必然落空。
3. **滚动默认是动画的**：`scrollIntoView` 不加 `behavior:'instant'`、或容器带 `scroll-behavior:smooth` 时，同步读到的坐标是「滚动途中」的位置——实测据此算出的点击点最终停在 `y=-1`。必须临时关掉平滑滚动。
4. **不要在页面里等帧**：`requestAnimationFrame` 在后台/被遮挡的帧里不会触发，用 `await new Promise(rAF)` 等滚动稳定会把 `Runtime.evaluate` 直接挂到超时（实测 15s 超时）。等帧不行，就在 Python 侧多轮「滚→复量」。
5. **活页面坐标会漂**：首页 swiper 的 data 带 `autoplay/interval/circular`，自动轮播会让元素自己移动，一次定位不够。本服务对 `tap` 做最多 3 轮重试，并在派发前用 `elementFromPoint` 复核那个点上到底是谁。
6. 弄不进视口就**拒绝点击**而不是照点：盲点视口外坐标会命中那里恰好存在的别的元素（实测误触发过 tabBar 跳转），比直接报错误导得多。

视图帧相对顶层文档的偏移用 `window.frameElement.getBoundingClientRect()` 拿（实测 `(10, 89, 375, 623)`），另有遍历 iframe 的兜底。

### 6. 截图要走 screencast，`captureScreenshot` 会被遮挡卡死

`Page.captureScreenshot` 走**合成器表面**，只在 target 当前可见时才可靠。实测模拟器被遮挡时
它会**一直不返回**（连试 6 次、4 种参数组合全超时），而且同一时刻可见的 target 能截、被遮挡的不能。

`Page.startScreencast` 走**渲染器主动推帧**，不受可见性影响，实测秒回。所以：

1. 主力通路 = screencast（拿整屏帧，再按视图偏移裁剪，需要 pillow）。
2. 兜底 = `captureScreenshot` 策略链，并记住成功过的调用形态。
3. 裁剪失败**不静默**——返回值里的 `capture.crop` 会说明原因（如「裁剪框超出画面」）。

### 7. 有几个非标准 CDP 扩展

- **`Tiny.Data.dataChanged`** —— 页面数据变更事件，支付宝私有域。事件缓冲不按域名白名单过滤，所以这类私有事件会自动流到 `alipay_inspector`。
- `Schema.getDomains` 返回的 35 个域是标准 CDP 列表，但**域声明与实际支持不一致**（例如部分命令会直接超时）。所以可选能力一律用 `try_call` 容错。

### 8. 抓不到「连上之前」的日志

CDP 事件只在监听建立后推送。要抓启动期日志，先 `alipay_automator(action='start')` 建立监听，
再触发一次重载/跳转，然后用 `alipay_inspector` 取结果。与微信 `wechat_inspector` 行为一致。

---

## 目录结构

```
alipay-devtools/
├── pyproject.toml
├── README.md
├── src/alipay_devtools/
│   ├── server.py       # 7 个 MCP 工具 + --doctor 自检
│   ├── cdp.py          # CDP over WebSocket：会话、分层、点击、截图、事件缓冲
│   ├── discovery.py    # 端点发现（DevToolsActivePort 优先，进程扫描兜底）
│   ├── minidev.py      # minidev CLI 封装（动作 → 参数表）
│   └── project.py      # 项目文件层：app.json / 页面三件套 / 检索 / appId 推断
├── tests/
│   ├── selfcheck.py    # 不依赖 IDE 的纯逻辑校验
│   └── live_check.py   # 端到端校验（需 IDE 在跑）
└── probe/              # 反向工程探针（保留作复验）
    ├── asar.py             # 最小 asar 解包工具（读 Electron 应用内部）
    ├── inspect_proc.py     # 打印调试面板进程的命令行，看端点写法
    ├── browser_port.py     # 验证 DevToolsActivePort 这条通路
    ├── ws_probe.py         # 端点发现 + 协议/鉴权验证
    ├── ws_probe2.py        # 协议域与上下文普查
    ├── ws_probe3.py        # 端点身份确认（模拟器外壳 vs 小程序）
    ├── ws_probe4.py        # 枚举并挂载全部 CDP target
    ├── ws_probe5.py        # 挂 Simulator：帧树 / 截图 / DOM
    ├── ws_probe6.py        # 逻辑层能力探测
    └── ws_probe7.py        # 交互原语实测（导航 / 合成点击 / 坐标派发）
```

---

## 已知边界

- **只驱动模拟器**，不驱动真机。真机走 `alipay_build(action='preview'|'remote_debug')`，需真机侧扫码。
- **IDE 必须开着项目并在运行**。IDE 没跑模拟器时没有 `Simulator` target，工具会返回明确提示而非静默失败。
- **截图依赖 pillow** 才能裁出 `view` 模式；没装则退回整帧，且 `capture.crop` 会说明。
- **`preview` / `remote_debug` 会等真机扫码**，CLI 阻塞到超时（默认 60s 后中断并返回已产出输出）。
- **`upload` / `audit` 会真的操作线上版本**，调用前请确认。
- **多窗口场景下无法按项目区分 Simulator target**（Simulator 的标题不含项目名），按项目名过滤只对进程端点有效。
- appId 推断顺序：`ALIPAY_APP_ID` → 项目内 json → 根目录 json 里的 `20xxxxxxxxxxxxxx`。
  IDE 把 appId 存在 Local Storage（leveldb）里，未解析，推断不出时请显式传。
- 截图默认落在 `~/.alipay-devtools/screenshots`，**刻意不写进项目目录**，免得污染业务仓库的 `git status`。
