using System.Text.Json;
using System.Text.Json.Serialization;
using WinUia;
Console.OutputEncoding = System.Text.Encoding.UTF8;

// uiacli：微信 UIA 识别读通道的命令行桥。
// 用 WinUia 库对指定 hwnd 做只读查询，单行 JSON 回 stdout，
// 供 wechatauto/uiacli.py 以 subprocess 调用。投递层（点击/键入）不在这里。
//
// 用法：
//   uiacli tree <hwnd>
//   uiacli read-input <hwnd>
//   uiacli find <hwnd> <edit|list|button> <key>
//   uiacli list-items <hwnd> <container_aid> [max=12]
//   uiacli write-value <hwnd> <edit|button> <key> <text>
//
// 协议：查询失败输出 {"found": false}（或空结构）并 exit 0，由调用方降级；
// 参数错误写 stderr 并 exit 2。

if (args.Length < 2)
{
    Console.Error.WriteLine("usage: uiacli <tree|read-input|find|list-items> <hwnd> [args...]");
    return 2;
}

if (!long.TryParse(args[1], out var hwndRaw) || hwndRaw <= 0)
{
    Console.Error.WriteLine($"bad hwnd: {args[1]}");
    return 2;
}

var cmd = args[0];
nint hwnd = (nint)hwndRaw;

try
{
    using var ctx = new AutomationContext();
    var win = ctx.FromHandle(hwnd);
    switch (cmd)
    {
        case "tree":
            Emit(CmdTree(win));
            break;
        case "read-input":
            Emit(CmdReadInput(win));
            break;
        case "find":
            if (args.Length < 4) { Console.Error.WriteLine("find needs <kind> <key>"); return 2; }
            Emit(CmdFind(win, args[2], args[3]));
            break;
        case "write-value":
            if (args.Length < 5) { Console.Error.WriteLine("write-value needs <kind> <key> <text>"); return 2; }
            Emit(CmdWriteValue(win, args[2], args[3], args[4]));
            break;
        case "list-items":
            if (args.Length < 3) { Console.Error.WriteLine("list-items needs <container_aid>"); return 2; }
            var max = args.Length >= 4 && int.TryParse(args[3], out var m) && m > 0 ? m : 12;
            Emit(CmdListItems(win, args[2], max));
            break;
        default:
            Console.Error.WriteLine($"unknown command: {cmd}");
            return 2;
    }
    return 0;
}
catch (Exception ex)
{
    // 窗口失效/COM 异常等运行期失败：按协议回空结构，不让 Python 侧炸进程
    Console.Error.WriteLine(ex.GetType().Name);
    Emit(cmd == "list-items"
        ? new { items = Array.Empty<object>() }
        : new { found = false });
    return 0;
}

static void Emit(object o) =>
    Console.Out.WriteLine(JsonSerializer.Serialize(o, JsonOpt()));

// 顶层语句文件里静态字段不能跟在语句后，收成方法；UnsafeRelaxed 让中文/emoji 原样输出不转 \uXXXX
static JsonSerializerOptions JsonOpt() => new()
{
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
};

static Dictionary<string, object?> Ok(Dictionary<string, object?> d) { d["found"] = true; return d; }
static Dictionary<string, object?> Miss() => new() { ["found"] = false };

// —— 命令实现 ——

// 控件类型字符串 → ControlType；不认识的 kind 返回 null（调用方回 Miss）
static ControlType? ParseKind(string kind) => kind switch
{
    "edit" => ControlType.Edit,
    "list" => ControlType.List,
    "button" => ControlType.Button,
    "listitem" => ControlType.ListItem,
    _ => null,
};

// ValuePattern 读值；不支持的控件返回空串（Name/AutomationId 仍有效）
static string ReadValue(WinUia.Element e)
{
    try { return e.ValuePattern.Value; }
    catch { return ""; }
}

static Dictionary<string, object?> ToHit(WinUia.Element e)
{
    var d = new Dictionary<string, object?>
    {
        ["name"] = e.Name ?? "",
        ["value"] = ReadValue(e),
    };
    try
    {
        var r = e.BoundingRectangle;
        d["rect"] = new int[] { r.Left, r.Top, r.Right, r.Bottom };
    }
    catch { /* 离屏/最小化时矩形可能拿不到，缺字段即可 */ }
    return Ok(d);
}

// 树规模探测：nodes 是全后代总数，has_input/has_search_list 钉微信关键 AutomationId
static Dictionary<string, object?> CmdTree(WinUia.Element win)
{
    var all = win.FindAll(e => true, TreeScope.Descendants);
    bool hasInput = false, hasSearch = false;
    foreach (var e in all)
    {
        var aid = e.AutomationId;
        if (aid == "chat_input_field") hasInput = true;
        else if (aid == "search_list") hasSearch = true;
    }
    return new Dictionary<string, object?>
    {
        ["nodes"] = all.Count,
        ["has_input"] = hasInput,
        ["has_search_list"] = hasSearch,
    };
}

// 输入框读值： aid=chat_input_field；Name 即当前会话名（2026-11-23 实测）
static Dictionary<string, object?> CmdReadInput(WinUia.Element win)
{
    var hits = win.FindAll(e => e.AutomationId == "chat_input_field" && e.ControlType == ControlType.Edit,
        TreeScope.Descendants);
    return hits.Count > 0 ? ToHit(hits[0]) : Miss();
}

// find：先 AutomationId 精确，再 Name 精确——搜索浮层等无 aid 控件靠 Name
static Dictionary<string, object?> CmdFind(WinUia.Element win, string kind, string key)
{
    var ct = ParseKind(kind);
    if (ct is null) { Console.Error.WriteLine($"unknown kind: {kind}"); Environment.Exit(2); }
    var byAid = win.FindAll(e => e.ControlType == ct && e.AutomationId == key, TreeScope.Descendants);
    if (byAid.Count > 0) return ToHit(byAid[0]);
    var byName = win.FindAll(e => e.ControlType == ct && e.Name == key, TreeScope.Descendants);
    return byName.Count > 0 ? ToHit(byName[0]) : Miss();
}

// write-value：按 find 的同一路径定位控件后 ValuePattern.SetValue。
// 走 hwnd+树搜索而不是屏幕命中测试（ControlFromPoint），被遮挡、
// 其他进程占用 UIA COM 时同样可用——SetValue 是写路径唯一的入口。
static Dictionary<string, object?> CmdWriteValue(WinUia.Element win, string kind, string key, string text)
{
    var ct = ParseKind(kind);
    if (ct is null) { Console.Error.WriteLine($"unknown kind: {kind}"); Environment.Exit(2); }
    var byAid = win.FindAll(e => e.ControlType == ct && e.AutomationId == key, TreeScope.Descendants);
    var e = byAid.Count > 0 ? byAid[0]
        : win.FindAll(x => x.ControlType == ct && x.Name == key, TreeScope.Descendants) is { Count: > 0 } byName ? byName[0] : null;
    if (e is null) return Miss();
    try
    {
        e.ValuePattern.SetValue(text);
        return Ok(new Dictionary<string, object?> { ["written"] = true, ["value"] = ReadValue(e) });
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine(ex.GetType().Name);
        return new Dictionary<string, object?> { ["written"] = false };
    }
}

// 容器（如 session_list）下的直接子项清单，按 Name 供调用方精确匹配
static Dictionary<string, object?> CmdListItems(WinUia.Element win, string containerAid, int max)
{
    var containers = win.FindAll(e => e.AutomationId == containerAid, TreeScope.Descendants);
    if (containers.Count == 0) return new Dictionary<string, object?> { ["items"] = Array.Empty<object>() };
    var kids = containers[0].FindAll(e => true, TreeScope.Children);
    var items = new List<object>();
    foreach (var k in kids)
    {
        if (items.Count >= max) break;
        var row = new Dictionary<string, object?> { ["name"] = k.Name ?? "", ["aid"] = k.AutomationId ?? "" };
        try
        {
            var r = k.BoundingRectangle;
            row["rect"] = new int[] { r.Left, r.Top, r.Right, r.Bottom };
        }
        catch { /* 离屏行缺 rect，调用方按 aid 走 find */ }
        items.Add(row);
    }
    return new Dictionary<string, object?> { ["items"] = items };
}
