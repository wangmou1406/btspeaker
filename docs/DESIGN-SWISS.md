# 蓝音管家控制台 · 瑞士风重设计规范（v2.0）

> 目标：把现有的「深色 Hi-Fi 科技风」控制台重做成**归藏 Swiss 风**——冷灰纸面、Helvetica 极细、唯一克莱因蓝、直角卡片、零渐变零阴影、发丝级分隔。功能、接口、DOM 契约、离线要求全部不变。
>
> 设计依据：`guizang-page-skill` 的 `references/design-rules.md`（瑞士风专有规则）、`assets/gz-swiss.css`（设计 token）、`references/themes.md`（ikb 主题）。
> 本文件是唯一视觉验收依据；实现细节冲突时以本文件 + 现有功能测试为准。

## 0. 硬约束（不可破坏）

1. **功能零回归**：4 页（总览/音箱/自动化/设置）所有交互、API 调用、`data-act` 事件委托、元素 id、`aria-*`、键盘可达性保持不变。
2. **离线**：不得引入任何 CDN / 外链字体 / 图片 / 第三方 JS。字体只用系统栈。
3. **测试契约**：`index.html` 继续直接引用 `app.js`，页面文本包含「蓝音管家」；`python -m unittest discover -s tests` 必须全绿。
4. 只允许改 `btspeaker/web/style.css`（整体重写）、`btspeaker/web/index.html`（骨架微调）、`btspeaker/web/app.js`（仅限模板 class/结构/微文案，禁止动逻辑）。
5. 不再使用深色背景 + 青紫霓虹；不做玻璃拟态、发光、渐变、投影。

## 1. 设计 token（写进 style.css 顶部 `:root`）

```css
--paper: #fafaf8;        /* 卡片/面板底 */
--canvas: #f0f0ee;       /* 页面底 */
--ink: #0a0a0a;          /* 主文字 */
--body: #252524;         /* 正文 */
--muted: #525252;        /* 次级 */
--helper: #737373;       /* 辅助/说明 */
--faint: #a3a3a3;        /* 占位/禁用 */
--line: #d4d4d2;         /* 发丝线（少用） */
--line-soft: #e6e6e3;    /* 更浅的线 */
--accent: #002fa7;       /* 唯一强调色：克莱因蓝 */
--accent-press: #001d6b;
--accent-soft: #d5deef;  /* 强调色浅底（仅选中态背景） */
--card: #fafaf8;
--canvas-2: #e9e9e6;     /* 表头/标签实心块的深一级灰 */
--font-sans: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', 'Segoe UI',
             'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans SC', Arial, sans-serif;
--font-mono: ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Consolas, 'Liberation Mono', monospace;
--sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 24px;
--sp-6: 32px; --sp-7: 48px; --sp-8: 64px; --sp-9: 96px;
--rail: 236px;           /* 桌面左侧导航栏宽度 */
--ease: cubic-bezier(.2,0,.38,.9);
```

- 全局 `border-radius: 0`；不写 `box-shadow`；不写 `linear-gradient`（装饰性点阵除外，最多一处）。
- 页面背景 `--canvas`，所有卡片/面板背景 `--paper`——**用背景色分层，不用边框框卡片**。
- 唯一允许的线：顶栏底部 1px `--line`、左侧 rail 右缘 1px `--line-soft`（或省略）、表格行间 1px `--line-soft`。
- 唯一允许的强调色用法：主按钮实底、选中态左轨条/下划线、播放中均衡器、KPI 大数字的个别强调、focus ring、标签主色。

## 2. 字体与字级（`font-weight` 用系统可用值，中文 ≥400，避免发虚）

| 角色 | 规格 |
| --- | --- |
| kicker / 微标签 | mono 10px, letter-spacing .18em, uppercase, `--helper` |
| 页面标题 h1 | sans 34px / 1.15, weight 500, letter-spacing -.01em |
| 页面副标题 | sans 13px, `--muted` |
| 卡片标题 | sans 12px / 1.2, weight 600, letter-spacing .04em |
| 正文 | sans 13.5px / 1.6, `--body` |
| 说明文字 | sans 12px / 1.55, `--helper` |
| 数据大数字 | sans 44–56px, weight 300, `letter-spacing: -.03em`, `font-variant-numeric: tabular-nums` |
| 表格数字 / MAC / 时间 / ID | mono 11.5–12px, `letter-spacing: .02em` |
| 按钮文字 | sans 12.5px, weight 500, letter-spacing .02em |
| 中文标题 | 不使用 `letter-spacing`（保持 0）；不使用斜体 |

中文避头尾：`line-break: strict; overflow-wrap: break-word; text-spacing-trim: normal;`（用 `:lang(zh)` 作用域即可）。

## 3. 布局

### 桌面（≥1024px）
```
┌──────────┬─────────────────────────────────────────────────────────┐
│ rail     │ top chrome（kicker + h1 + 副标题 / 右侧状态与刷新）      │
│ 236px    ├─────────────────────────────────────────────────────────┤
│ 品牌块   │ content（12 列网格，gutter 16px，最大 1280px）           │
│ 01 总览  │                                                         │
│ 02 音箱  │                                                         │
│ 03 自动化│                                                         │
│ 04 设置  │                                                         │
│ ...      │                                                         │
│ 底部状态 │                                                         │
└──────────┴─────────────────────────────────────────────────────────┘
```
- rail 固定在左，背景 `--paper`，右缘 1px `--line-soft`；nav 项：编号 mono 10px `--faint` + 中文 14px，行高 44px，选中=最左 2px 克莱因蓝竖条 + 文字 `--ink`，未选中 `--muted`，hover 背景 `--canvas`。
- rail 顶部品牌：中文名 18px weight 600 + `BTSPEAKER` mono 9px letter-spacing .22em；下方一条 1px 线（可选）。
- rail 底部：`● 在线`（状态点用 7px 方块，实心=在线，空心=异常）、版本 mono、`访问令牌` 文本按钮。
- 顶部 chrome 高度 96px：kicker（例 `SYSTEM / 01`）+ h1 + 副标题分两行；右侧状态簇：`SIM 模拟` 标签、当前音箱一格、刷新方按钮。
- 页面切换用 hash 路由不变。

### 平板（≤1023px）：rail 折叠成顶部条（品牌 + 4 个页签横排），content 单列 + 2 列网格。

### 手机（≤767px）：顶部品牌条 + 底部固定 tabbar（4 项：编号+中文，选中克莱因蓝上边条），内容单列，卡片内边距 16px，表格类组件允许横向滚动（`overflow-x:auto`），触控目标 ≥40px。

## 4. 组件

- **按钮**：`height:36px; padding:0 16px; border-radius:0;` 主按钮=克莱因蓝底白字，hover `--accent-press`；次按钮=`--paper` 底 + 1px `--ink` 边框；ghost=透明+1px `--line` 边框 + `--muted` 文字；危险操作（删除）用 ghost + hover 变 `--ink` 底白字，**不引入红色**；`btn-sm` 高 28px/内边距 10px。
- **卡片**：`background:var(--paper); border:0; border-radius:0; padding:20px 20px 18px;` 卡与卡之间间距 16px；卡片之间可加 1px `--line-soft` 顶部线（可选，整页不超过 3 处）。
- **KPI 统计**：4 个横排，无卡片，之间 1px 竖线 `--line-soft`；数字 44px weight300，标签 kicker 样式。
- **表格**：表头 `background:var(--canvas-2); color:var(--ink); font:mono 11px; letter-spacing:.08em;` 行高 44px，行间 1px `--line-soft`，行 hover `--canvas`；数字列右对齐；规则列表、日志、声音库都用表格语言。
- **标签 tag**：主标签=克莱因蓝实底白字（每行最多 1 个）；中性标签=`--canvas-2` 底 + `--ink` 文字；幽灵标签=1px `--line` 边框 + `--muted`；统一 `height:20px; padding:0 8px; font:mono 10px; letter-spacing:.06em; border-radius:0`。
- **开关 switch**：34×18px 直角矩形，`--line` 边框，关闭=透明+左侧 14px 方块，开启=`--accent` 实底+右侧方块；`role="switch"` 保留。
- **输入/选择**：`height:38px; background:transparent; border:0; border-bottom:1px solid var(--faint); border-radius:0;` focus 时下边线 2px 克莱因蓝；label 用 kicker 样式（mono 10px uppercase）。textarea 用 1px `--line` 直角框。分段选择器（seg）= 直角、选中 clay 蓝底白字。
- **音量滑块**：轨道 2px `--line`，已填充部分克莱因蓝，滑块 12×12 正方形克莱因蓝；用 `input[type=range]` 自绘（web/style 需兼容 Chromium WebKit 伪元素）。
- **均衡器**：5 根 2px 宽竖条，间距 3px，高 10-22px 循环动画，颜色克莱因蓝；空闲时全部 6px 高、`--faint`、无动画。
- **弹窗 modal**：遮罩 `rgba(10,10,10,.4)`；面板 `--paper` 直角，`min-width:360px; padding:28px`；标题 18px weight 600；按钮右对齐（次按钮 + 主按钮）。保留 `role="dialog"`。
- **toast**：左下或右下直角卡片，白色底 + **2px 克莱因蓝左边线** + mono 时间戳；成功/失败只用图标与文案区分（不引入绿/红）。
- **图标**：沿用现有内联 SVG sprite；统一 16×16，`stroke: currentColor; stroke-width:1.5`；禁止 emoji。
- **日志**：表格化；level 用 tag（INFO=幽灵、WARN/ERROR=黑底白字）；时间/模块 mono。
- **空状态**：左侧 8px 克莱因蓝方块 + 一句引导文案 + 一个文本链接按钮；不要插画。

## 5. 逐页要求

### 总览 Dashboard
- 顶部 KPI 条 4 项：适配器（地址 mono）、当前音箱、播放状态、规则启用数。
- 左 7 列：「当前播放」大字号块（标题 22px weight 400；来源 mono 11px；时长/音量 mono；底部播放控制按钮）；下方「快捷动作」2×2 直角功能块（TTS 播报输入、播放提示音、扫描、停止）。
- 右 5 列：下一批定时任务（表格：时间 mono / 规则名 / 剩余）、最近日志（紧凑表格 5 行）、规则统计（数字 + 进度条用 2px 直角条）。
- 底部 `模拟模式` 说明用 kicker + 说明文字，不放大色块。

### 音箱 Devices
- 顶部：适配器信息条（地址/电源/扫描状态）+ 扫描按钮。
- 「已配对音箱」表格：名称/地址 mono/状态 tag/操作（连接、断开、设为默认、删除都保留）。
- 「附近设备」表格：名称/地址/状态/配对按钮；扫描中显示线性进度提示（mono 文字 + 2px 进度线）。
- 音量与播放：一行内 音量滑块 + 数值 mono + 声音库下拉 + 播放/停止。

### 自动化 Rules
- 顶部工具条：`导入 / 导出 / 新建规则`（ghost + 主按钮）。
- 规则列表改为表格：编号 mono（R-01…）/ 名称 / 触发（中文描述）/ 动作（最多 2 个 tag + `+N`）/ 上次执行 / 开关 / 操作（立即执行、编辑、删除）。
- 编辑页：左侧 12 列中「基本信息 + 触发器 + 条件」占 7 列，「动作编排」占 5 列（sticky）；条件/动作用直角编号列表（01、02…），可上下移/删除；所有字段 label 用 kicker。

### 设置 Settings
- 两列 12 栅格：左 7 列 = 系统信息（定义列表表格，值 mono）+ TTS + 播放器；右 5 列 = 蓝牙 + 安全 + 运行诊断（诊断行：状态方块 + 名称 + 详情 mono + hint）。
- 每个配置区块：kicker + 18px 标题 + 16px 间距 + 表单网格（2 列，label 上、input 下边线样式）。
- 模拟模式提示：一条 2px 克莱因蓝左边线 + 说明文字，不用大色块。

## 6. 禁止清单（出现即不合格）

- 深色/黑色页面背景、霓虹青紫、渐变、发光、磨砂玻璃、圆角 > 4px（按钮/卡片/标签必须 0）。
- 卡片投影、彩色状态色（绿/黄/红）、emoji 当图标、外部字体/图片。
- 破坏 `data-act`、id、aria-label、`role` 的 DOM 改动；改动 API 逻辑。
- 一屏出现超过 1 个主按钮实底（主按钮用于每页唯一主操作，其余 ghost/次按钮）。

## 7. 验收

1. `node --check btspeaker/web/app.js` 通过。
2. `python -m unittest discover -s tests` 全绿（291 项）。
3. 用模拟模式自查渲染（无需浏览器）：`BTSPEAKER_SIM=1 python -m btspeaker serve --data-dir .btsdata --port 8723` 后 `curl /` 能返回新 HTML；不要长期占用端口。
4. Lead 用浏览器截图验收 4 页 + 交互（扫描/播放/TTS/规则创建/设置保存/弹窗）。
5. 375px 与 1440px 两种宽度都要能用（响应式断点 1023/767）。
