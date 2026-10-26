import pytest

from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import domain_for
from devproxy.gateway.names import validate_name


@pytest.mark.parametrize("name", ["codeup", "codeup-web", "a1-b2-c3", "x", "a-b", "x" * 63])
def test_valid_names_pass(name):
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name,reason",
    [
        ("Codeup", "大写"),
        ("a.b", "含点"),
        ("-x", "首字符连字符"),
        ("", "空"),
        ("a_b", "下划线"),
        ("a b", "空格"),
        ("x" * 64, "超长"),
        ("codeup\n", "尾随换行"),
        ("foo-", "尾随连字符"),
        ("a--", "尾随连字符"),
    ],
)
def test_invalid_names_raise(name, reason):
    with pytest.raises(InvalidNameError) as cm:
        validate_name(name)
    assert cm.value.name == name
    assert cm.value.reason


def test_domain_for_strips_leading_dot():
    assert domain_for("codeup", ".test") == "codeup.test"
    assert domain_for("codeup", "test") == "codeup.test"


def test_domain_for_validates_name():
    with pytest.raises(InvalidNameError):
        domain_for("bad.name", "test")
