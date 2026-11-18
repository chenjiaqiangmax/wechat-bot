"""冻结后能否真正读微信：解密数据库 + 列出会话。

比 probe.py 更进一步——真走 WeChat 的读取链路（读 Weixin.exe 内存取密钥、
SQLCipher 解密、查 session 表），不做任何发送，也不需要 UIA 交互。
"""
import sys


def main() -> int:
    print(f'frozen={getattr(sys, "frozen", False)}')

    from wechatauto import WeChat
    wx = WeChat()

    db = getattr(wx, '_db', None) or getattr(wx, 'db', None)
    print(f'db={type(db).__name__}')
    print(f'nickname={getattr(wx, "nickname", "")!r}')

    session = wx.GetSession()
    print(f'GetSession ok={session.ok if hasattr(session, "ok") else "?"}')
    print(f'summary={str(getattr(session, "data", ""))[:120]!r}')

    contacts = db.search_contact('God-Cap') if db else []
    print(f'search_contact(God-Cap)={contacts[:2]}')

    print('READ OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
