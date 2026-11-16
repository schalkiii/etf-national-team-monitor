# 发布操作手册（开发/维护用）

面向维护者。面向读者的产品说明见 `README.md`。

## 通道现状（2026-11-16 实测）

截图与发布各有两条通道，当前环境**唯一验证可行的组合是「kwb 截图 + 真实 Edge 发布」**。

```bash
python scripts/snapshot_charts_kwb.py  # 截图：kimi-webbridge 驱动真实 Edge（已验证，7/7 可用）
python scripts/snapshot_charts.py      # 截图备选：Edge headless CLI + marker 像素定位
python scripts/publish_xhs.py          # 仅用其 build_content()/选图逻辑，发布动作见下
```

- `snapshot_charts.py` 在无桌面/沙箱会话下 Edge 渲染完可能不退出（network-service/sitelist 重试），
  已改为启动后轮询 PNG 落盘、稳定后再终止进程；该通道可工作但不是首选。
- `snapshot_charts_kwb.py` 走 scroll + 视口裁剪，输出文件名严格匹配
  `etf_ntm_<date>_NN_<name>.png`；`publish_xhs.py` 用正则筛选正图、排除调试残留。
- 两条通道产物不同：`snapshot_charts.py` 用包裹层把「图 N + 标题」烘进 PNG；
  `snapshot_charts_kwb.py` 直接裁剪图表 div、**图内无章节标题**，靠正文的「图1…图7」补位。

## 发布纪律

1. **先试发后公开**：默认「仅自己可见」，在 App 里人工验收图片与文案，确认无误才转公开。
2. **没有编辑/删除 API**：MCP 工具只有 `publish_content` / `check_login_status` /
   `get_my_profile`（2026-11-09 实测）。**更新已发笔记的图片 = 重发一篇新笔记**，旧笔记在 App 手动删。
3. **MCP 发布通道实测不可用**：`check_login_status` 只反映主站登录，不等于创作者中心鉴权通过。
   `publish_content` 即使把 Edge 的全部创作者中心 cookie（含 `x-user-id-creator`、
   `acw_tc@creator`、`last_web_session`）合并进 MCP 的 cookies.json，仍报「创作者中心登录失效，
   请重新扫码登录」；同批 cookie 注入真实 Edge 却可直接登录。卡点是 MCP 自带 rod 浏览器
   环境/指纹，不是 cookie。发布改走真实 Edge 图形界面。
4. **截图质量验证走像素**：发布前对每张 PNG 做四边贴边扫描（背景色差阈值），
   轴标签/图例溢出容器会在图缘留下文字像素。CDP `storage.traverse` 量包围盒会把未渲染的
   隐藏元素算进来产生假阳性，**以像素为准**。
5. **窗口默认最短档**：主图 120 日、K 线 120 日、轮动 20 日、热力 60 日、两融 120 日——
   社交读者不需要全历史，全历史稀释近期信号且加大文字渲染密度。
6. **echarts 图例配色坑**：某系列 `itemStyle.color` 用显式常量时不消耗调色板名额，
   后续系列错位取色。多系列图凡手工指定过一个系列颜色，其余全部显式指定。

## kimi-webbridge 驱动 Edge 的实测门槛

1. 本地报告页用 `127.0.0.1` HTTP 服务喂（`file://` 被扩展禁止，localhost 需 `--noproxy '*'` 绕代理）。
2. `evaluate` 参数是 `code`、`fill` 是 `value`；复杂 JSON 必须写临时文件后 `--data-binary @file`，
   内联 `-d` 会被 shell 破坏；`mouse_click` 需要 selector。
3. **上传图片**：扩展 `upload` 返回 `fileCount=7` 却无组件响应、`DOM.setFileInputFiles` 被拒绝，
   唯一可靠是 CDP `Input.dispatchDragEvent`（dragEnter→dragOver→drop，`files` 为正斜杠绝对路径，
   `mimeType:"Files"`）；且**必须等 `.skeleton` 遮罩消失**，否则 drop 落在骨架层上只生成幽灵草稿
   （草稿箱计数+1 但「暂无草稿」）。
4. **发布按钮藏在 `<xhs-publish-btn>` 的 closed shadow root**：JS `click` 无效、DOM 里搜不到文字。
   须 CDP `DOM.getDocument(pierce)→querySelector→describeNode(pierce)→getBoxModel` 取坐标，再用
   `Input.dispatchMouseEvent` mousePressed+mouseReleased 点 `button.ce-btn.bg-red`。
   顶部「发布笔记」只是导航按钮，点了会跳到视频上传页，不是发布。
5. **可见性下拉的点击层级**：`.d-select-description` 是描述文本，点它无效；要点父容器
   `.permission-card-select.custom-select-44` 或 `.d-select-main`，再点 `.custom-option` 中的
   「仅自己可见」。

**发布成功的唯一可靠判据** = `POST edith.xiaohongshu.com/web_api/sns/v2/note` 返回 200 +
跳转 `/publish/success` + 笔记管理页条目数 +1 且标注「仅自己可见」。草稿箱计数因幽灵草稿失真，
不可作判据。

跨项目通用手法另记于用户级记忆「kimi-webbridge 驱动真实 Edge 的通用浏览器自动化经验」。
