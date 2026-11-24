# 把微信装进 Sandboxie：能力边界与多开配置

> 这份文件回答两件事：Sandboxie 能不能让微信「不在你用的桌面上抢键鼠」（**不能**），
> 以及怎么用它做多个微信实例（**能**）。边界结论来自本机 Sandboxie 源码与 Windows 会话模型，
> 多开配置依据官方文档与本机微信 4.x 的目录结构；本机未安装 Sandboxie，配置部分未做真机验证。
>
> 2026-11-19 · 本机微信 4.x：`D:\Program Files\Tencent\Weixin\Weixin.exe`（`--scene=desktop`）

## 一、先给结论

**Sandboxie 能做到多开，做不到「不抢鼠标键盘」。**

| 需求 | Sandboxie 能否满足 | 原因 |
|---|---|---|
| 多个微信同时在线 | **能** | 每个沙箱有独立的文件与注册表视图，`%APPDATA%\Tencent\xwechat` 不冲突 |
| 沙箱内微信不抢宿主机前台焦点 | **不能** | 沙箱只重定向文件/注册表/COM/网络，不隔离窗口与输入 |
| 沙箱内微信不移动宿主机光标 | **不能** | `SetCursorPos` / `SendInput` 作用的是同一个用户会话的光标 |
| 沙箱内微信不打断你正在打的字 | **不能** | 沙箱进程与宿主进程共享同一个 `WinSta0\Default` 桌面与输入队列 |

### 源码证据（`Sandboxie/Sandboxie/core/dll/gui.c`）

```
421:    GUI_IMPORT___(SetCursorPos);
472:    GUI_IMPORT___(SendInput);
546:    GUI_IMPORT___(SetForegroundWindow);
```

这三行是 **`GUI_IMPORT___`（导入原函数地址，供沙箱内转发调用）**，不是拦截钩子——
文件里没有对应的 `Gui_SetCursorPos` / `Gui_SendInput` 包装实现，也没有返回 `FALSE` 拒绝的分支。
Sandboxie 的 GUI hook 范围是 `CreateWindowEx`、`DefWindowProc`、`IsWindowVisible`、`MoveWindow`、
`SetThreadDesktop` 这类**窗口归属与消息转发**，目的是判定「这个窗口属于哪个沙箱」，而不是隔离输入。

沙箱进程仍然运行在你的用户会话里、同一个桌面上：它的窗口就在你的屏幕上，
你点它一样会激活，它调用 `SetForegroundWindow` 一样能抢前台。

> 因此「在沙箱里跑 = 独立环境 = 不抢键鼠」这个推断不成立。
> 除非换成**独立用户会话**（另一登录用户 / RDP 会话）或**虚拟机**——那是另一套东西，不是 Sandboxie。

## 二、它真正的价值：多开

微信 4.x 默认单实例（`Weixin.exe` 第二次启动会把已有窗口拉到前台）。
Sandboxie 给每个沙箱一套独立的文件/注册表命名空间，于是多个实例各自的登录态与配置互不覆盖。

### 2.1 安装

```powershell
# 从官网下载安装包（Sandboxie-Plus x64）
# https://github.com/sandboxie-plus/Sandboxie/releases
# 装完确认服务在跑：
sc query SbieSvc
```

安装需要写内核驱动 `SbieDrv`，**需要管理员权限，通常要重启一次**。这一步只能你本人做。

### 2.2 建沙箱（Sandboxie-Plus 界面）

`Sandbox → Create New Box`，沙箱名 `WeChat_A`，模板选 `DefaultBox`。

或直接写 `C:\Windows\Sandboxie.ini`：

```ini
[WeChat_A]
Enabled=y
AutoDelete=n
ConfigLevel=9
BoxNameTitle=n
BorderColor=#00A0FF,ttl
Template=AutoRecoverIgnore
Template=OpenBluetooth
Template=SkipHook
OpenFilePath=D:\Program Files\Tencent\Weixin\
OpenFilePath=D:\weixinlog\
OpenFilePath=%APPDATA%\Tencent\xwechat\
OpenKeyPath=HKEY_CURRENT_USER\Software\Tencent\Weixin
```

关键项说明：

- `OpenFilePath=D:\Program Files\Tencent\Weixin\` —— 微信**安装目录只读放行**（程序文件不必复制进沙箱）。
- `OpenFilePath=D:\weixinlog\` —— 聊天记录目录**直通宿主机**，方便本项目用 `db.py` 直接解密读取
  （不直通的话记录会落在沙箱目录里，本项目读不到）。
- `OpenFilePath=...\Tencent\xwechat\` —— 登录态/配置直通。
- `OpenKeyPath=HKEY_CURRENT_USER\Software\Tencent\Weixin` —— 注册表直通。

> **多开的关键**：`WeChat_B` 要把这三条 `OpenFilePath` / `OpenKeyPath` **指向另一套目录**，
> 不能和 A 共用，否则两个实例会抢同一份登录态。

### 2.3 启动

```powershell
& "C:\Program Files\Sandboxie-Plus\Start.exe" /box:WeChat_A `
    "D:\Program Files\Tencent\Weixin\Weixin.exe" --scene=desktop
```

### 2.4 多开

```ini
[WeChat_B]
Enabled=y
AutoDelete=n
ConfigLevel=9
BoxNameTitle=n
BorderColor=#FFA000,ttl
Template=AutoRecoverIgnore
OpenFilePath=D:\Program Files\Tencent\Weixin\
OpenFilePath=D:\weixinlog_B\
OpenFilePath=%APPDATA%\Tencent\xwechat_B\
OpenKeyPath=HKEY_CURRENT_USER\Software\Tencent\Weixin_B
```

启动：

```powershell
& "C:\Program Files\Sandboxie-Plus\Start.exe" /box:WeChat_B `
    "D:\Program Files\Tencent\Weixin\Weixin.exe" --scene=desktop
```

第三个、第四个照抄，改 `_C`、`_D` 与端口/目录即可。

**验证多开**：

```powershell
Get-Process Weixin | Select-Object Id, Path, StartTime
# 期望看到多个 PID；每个沙箱的进程 CommandLine 里带各自的 box 标识
& "C:\Program Files\Sandboxie-Plus\Start.exe" /box:WeChat_A /listpids
& "C:\Program Files\Sandboxie-Plus\Start.exe" /box:WeChat_B /listpids
```

## 三、数据放在哪、怎么备份迁移

| 内容 | 路径 |
|---|---|
| 登录态、配置 | `%APPDATA%\Tencent\xwechat` |
| 聊天记录 / 文件 | `D:\weixinlog\xwechat_files`（微信 4.x 设置里可改） |
| 沙箱内的写入（未 `OpenFilePath` 的部分） | `C:\Sandbox\<用户名>\<沙箱名>\drive\C\...` |

**备份**：停掉对应微信 → 复制上面三处 → 完事。沙箱目录本身就是完整的文件快照。

**迁移到另一台机器**：

1. 拷 `C:\Sandbox\<用户>\<沙箱名>\` 整个目录到新机的同名路径；
2. 拷 `D:\weixinlog\xwechat_files` 与 `%APPDATA%\Tencent\xwechat`；
3. 新机建同名沙箱，`OpenFilePath` 指向新路径；
4. 启动沙箱内微信，登录态与聊天记录应恢复。

> 微信 4.x 的登录态是设备绑定的，换机后可能要求重新扫码；聊天记录本身可迁移。

## 四、「不抢焦点」这条，本项目已经用另一种方式解决

`wechatauto` 的发送链路已经改成全程消息投递，不需要微信在前台：

| 环节 | 做法 |
|---|---|
| 搜索 | `ValuePattern.SetValue`（实测能触发微信搜索） |
| 打开会话 | `PostMessage` 点击搜索结果，投给**落点所属的最小微信窗口**（搜索下拉是独立弹层） |
| 写入 | `ValuePattern.SetValue` |
| 发送 | `PostMessage` 点「发送」按钮 |
| 还原焦点 | 每次点击后把前台还给**记住的用户窗口** |

实测（微信被 1500×950 置顶窗口完全盖住）：

- `Chat.SendMsg(verify=True)` 成功并落库确认；
- 光标在发送前后**坐标不变**；
- 最终前台**回到用户的窗口**。

**仍然消不掉的**：微信控件收到鼠标消息后自己会 `SetForegroundWindow`（`WS_EX_NOACTIVATE` 也拦不住），
所以全程会有若干次**极短的前台切换**，只能在每次点击后立刻归还。
要做到绝对零切换，只有**独立用户会话 / 虚拟机**——Sandboxie 不在其列。

## 五、一条更轻的多开替代路线

如果只是要多开、不需要沙箱隔离文件，微信 4.x 支持用**独立数据目录**起多个实例：

```powershell
# 复制一份配置目录（首次）
Copy-Item -Recurse "$env:APPDATA\Tencent\xwechat" "$env:APPDATA\Tencent\xwechat_B"

# 用不同数据目录启动（具体开关以当前微信版本为准，4.x 常用 --user-data-dir）
& "D:\Program Files\Tencent\Weixin\Weixin.exe" --scene=desktop --user-data-dir="$env:APPDATA\Tencent\xwechat_B"
```

> 这条是否生效取决于微信版本是否仍接受该参数；Sandboxie 路线不依赖微信自身参数，兼容性更稳。