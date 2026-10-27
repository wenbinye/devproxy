import pytest

from devproxy.gateway.names import InvalidNameError
from devproxy.gateway.names import domain_for
from devproxy.gateway.names import port_env_name
from devproxy.gateway.names import route_name_from_port_env
from devproxy.gateway.names import validate_name


@pytest.mark.parametrize("name", ["codeup", "codeup-web", "a1-b2-c3", "x", "a-b", "x" * 63])
def test_valid_names_pass(name):
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name,reason",
    [
        ("Codeup", "uppercase"),
        ("a.b", "contains a dot"),
        ("-x", "leading hyphen"),
        ("", "empty"),
        ("a_b", "underscore"),
        ("a b", "space"),
        ("x" * 64, "too long"),
        ("codeup\n", "trailing newline"),
        ("foo-", "trailing hyphen"),
        ("a--", "trailing hyphen"),
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


@pytest.mark.parametrize("name", ["api", "code-up", "a-b-c", "9lives"])
def test_port_env_name_round_trip(name):
    """The env var and its inverse must agree, or a file that spells the
    variable the natural way gets rejected as an undeclared reference."""
    assert route_name_from_port_env(port_env_name(name)) == name


def test_route_name_from_port_env_ignores_foreign_names():
    assert route_name_from_port_env("BACKEND_PORT") is None
    assert route_name_from_port_env("DEVPROXY_API") is None
    assert route_name_from_port_env("DEVPROXY__PORT") is None
