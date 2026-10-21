"""devp 单元测试。devp 是无扩展名脚本，用 SourceFileLoader 加载。"""
import importlib.machinery
import importlib.util
import socket
import unittest
from pathlib import Path

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
        for name in ("codeup", "codeup-web", "a1-b2-c3", "x"):
            devp.validate_name(name)  # 不抛异常即通过

    def test_invalid_names_exit(self):
        for name in ("Codeup", "a.b", "-x", "", "a_b", "a b", "x" * 64):
            with self.assertRaises(SystemExit):
                devp.validate_name(name)


class TestAllocPort(unittest.TestCase):
    def test_returns_bindable_ephemeral_port(self):
        port = devp.alloc_port()
        self.assertIsInstance(port, int)
        self.assertTrue(1024 < port < 65536)
        # 返回的端口确实可绑定（bind 后立即释放）
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()
