"""打包可行性探针：验证 PyInstaller 冻结后核心依赖能否 import 并真正工作。

不启动机器人、不连微信，只把打包最容易出问题的几类依赖各调用一次：
UIA 控件树、winrt 投影命名空间、SQLCipher 解密用的 cryptography、
微信数据库里用到的 zstandard。
"""
import sys


def main() -> int:
    print(f'frozen={getattr(sys, "frozen", False)} python={sys.version.split()[0]}')
    print(f'exe={sys.executable}')

    import uiautomation as auto
    import psutil
    import pyperclip
    import zstandard
    from PIL import Image
    import cryptography
    import win32gui

    print(f'uiautomation={auto.__version__ if hasattr(auto, "__version__") else "?"}')
    print(f'psutil={psutil.__version__} pillow={Image.__version__}')
    print(f'cryptography={cryptography.__version__} zstandard={zstandard.__version__}')

    # winrt 投影命名空间是数据文件，冻结时最容易漏；这里真 import 一遍
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    print(f'winrt OCR langs={len(OcrEngine.available_recognizer_languages)}')

    import wechatauto
    print(f'wechatauto={wechatauto.__version__}')

    # 真实调用一次 UIA：跨进程枚举顶层窗口，验证 comtypes/uiautomation 冻结后可用
    root = auto.GetRootControl()
    children = root.GetChildren()
    print(f'UIA root children={len(children)}')

    # win32gui 真实调用
    hwnd = win32gui.GetForegroundWindow()
    print(f'foreground hwnd={hwnd} title={win32gui.GetWindowText(hwnd)[:40]!r}')

    # zstandard 真实压缩一次
    cctx = zstandard.ZstdCompressor()
    assert cctx.compress(b'hello' * 100) is not None
    print('zstandard roundtrip ok')

    print('PROBE OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
