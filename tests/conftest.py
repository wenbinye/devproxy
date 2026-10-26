import pytest

from devproxy.core.paths import Paths


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Point HOME at a temporary directory -- tests must never write into the real home.

        Both Path.home() and os.path.expanduser("~") honour HOME: this keeps default_paths()
    from conjuring files inside the real ~/.config/devproxy or ~/.local/state/devproxy, and
    keeps home-directory paths such as the dnsmasq config and ~/.pki/nssdb away as well.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    return home


@pytest.fixture(autouse=True)
def isolated_cli_platform(monkeypatch):
    """Swap the CLI platform for FakePlatform -- tests must never touch the real system.

    The CLI's setup rewrites the units under ~/.config/systemd/user/ and the DNS config
    under /etc, and the migration restarts real services. All those paths are decided by the
    platform object, so as long as the platform is still the real LinuxPlatform, one
    `setup --yes` changes things on the user's machine (it once really overwrote the user's
    own caddy.service).

    This is the global fallback: every test gets an isolated fake platform by default; tests
    that genuinely need the real one monkeypatch over it themselves (later wins).
    """
    from devproxy.platform.fake import FakePlatform

    platform = FakePlatform()
    monkeypatch.setattr("devproxy.cli._shared._platform", lambda: platform)
    return platform
