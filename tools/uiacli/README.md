# uiacli — 微信 UIA 识别读通道的 C# 桥

用本地 [win-uia](../../win-uia/)（buning-software/win-uia，.NET 11 UIA3 封装）对指定
窗口做**只读** UIA 查询，单行 JSON 回 stdout。由 `wechatauto/uiacli.py` 以
subprocess 调用；投递层（PostMessage 点击 / WM_CHAR / SetValue）不在这里。

## 接口契约（Python 侧按此解析，改命令名/字段前先同步改 uiacli.py）

```
uiacli tree <hwnd>                    → {"nodes": <int>, "has_input": <bool>, "has_search_list": <bool>}
uiacli read-input <hwnd>              → {"found": true, "name": "<会话名>", "value": "<草稿>", "rect": [l,t,r,b]}
uiacli find <hwnd> <edit|list|button> <key>
                                      → {"found": true, "name": "...", "value": "...", "rect": [l,t,r,b]}
uiacli list-items <hwnd> <container_aid> [max=12]
                                      → {"items": [{"name": "...", "aid": "..."}]}
```

- `find` 先按 AutomationId 精确匹配，再按 Name 精确匹配（发送按钮等无 aid 控件走 Name）。
- 查询失败（窗口失效、控件不在、COM 异常）→ `{"found": false}` 或空结构，**exit 0**，
  由调用方降级；参数错误 → stderr + **exit 2**。
- `hwnd` 为十进制窗口句柄。输入框 aid 固定 `chat_input_field`，会话列表 aid `session_list`，
  搜索浮层列表 aid `search_list`（搜索时才出现在主窗口树）。

## 构建

本机全局 NuGet 源只有 VS 离线包，win-uia 的 PublicAPIAnalyzers 需要在线源，
restore 时用 `-p:RestoreSources` 覆盖（不改 win-uia 仓库内文件）：

```
cd tools/uiacli
dotnet publish -c Release -r win-x64 --self-contained false -o publish \
  -p:RestoreSources=https://api.nuget.org/v3/index.json
```

产物：`tools/uiacli/publish/uiacli.exe`（依赖本机 .NET 11 运行时）。

## 验证记录（2026-11-23，微信 4.2.0.53 主窗口 hwnd 5637044）

- `read-input` → `{"name":"Tao_","value":"是啊","rect":[1016,613,1574,673],"found":true}`
- `find button 发送` → `{"name":"发送","rect":[1522,681,1570,708],"found":true}`（Name 路径）
- `list-items session_list 5` → 5 项，emoji 会话名以代理对转义输出，Python `json.loads` 可解
- `tree` → `{"nodes":162,"has_input":true,"has_search_list":false}`
- `badcmd` → exit 2
