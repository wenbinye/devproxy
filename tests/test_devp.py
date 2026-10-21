"""devp 单元测试。devp 是无扩展名脚本，用 SourceFileLoader 加载。"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import socket
import unittest
from pathlib import Path
from unittest import mock

DEV = Path(__file__).resolve().parent.parent / "devp"


def load_devp():
    loader = importlib.machinery.SourceFileLoader("devp", str(DEV))
    spec = importlib.util.spec_from_loader("devp", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


devp = load_devp()


class TestValidateName(unittest.TestCase):
    def test_valid_names(self):
        # 含 63 字符（规格允许的最大长度）——锁住 len(name) > 63 的边界
        for name in ("codeup", "codeup-web", "a1-b2-c3", "x", "x" * 63):
            with self.subTest(name=name):
                devp.validate_name(name)  # 不抛异常即通过

    def test_invalid_names_exit(self):
        # "codeup\n" 必被拒：$ 会匹配末尾换行之前，name 拼进 host 会得到
        # "codeup\n.test"（永不匹配），且注册/注销的 URL 不一致
        for name in ("Codeup", "a.b", "-x", "", "a_b", "a b", "x" * 64, "codeup\n"):
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as cm:
                    devp.validate_name(name)
                code = cm.exception.code
                self.assertIsInstance(code, str, "sys.exit 应带字符串错误信息")
                self.assertTrue(code.strip(), "错误信息不应为空")
                self.assertIn(repr(name), code, "错误信息应包含被拒的 name")


class TestAllocPort(unittest.TestCase):
    def test_returns_bindable_ephemeral_port(self):
        port = devp.alloc_port()
        self.assertIsInstance(port, int)
        self.assertTrue(1024 < port < 65536)
        # 返回的端口确实可绑定（bind 后立即释放）
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()


class TestHeartbeatEnv(unittest.TestCase):
    """DEVP_HEARTBEAT 非法值不能让整个 CLI 起不来（模块级常量）。"""

    def test_numeric_heartbeat_used(self):
        with mock.patch.dict(os.environ, {"DEVP_HEARTBEAT": "45"}):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                mod = load_devp()
        self.assertEqual(mod.HEARTBEAT, 45.0)
        self.assertEqual(stderr.getvalue(), "")

    def test_non_numeric_heartbeat_falls_back_to_30(self):
        with mock.patch.dict(os.environ, {"DEVP_HEARTBEAT": "abc"}):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                mod = load_devp()
        self.assertEqual(mod.HEARTBEAT, 30.0)
        self.assertIn("DEVP_HEARTBEAT", stderr.getvalue())
