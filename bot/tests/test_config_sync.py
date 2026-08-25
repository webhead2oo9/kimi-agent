"""Contract guards keeping the config surface in sync.

`config/settings.py` is the source of truth for typed settings. The committed
`.env.example` template and `docs/configuration.md` reference are hand-maintained
mirrors of its field list, and the repo's stated contract (see the doc header) is
that *every* `Settings` field appears in both. These tests turn that discipline
into a checked invariant so a new field can't ship undocumented.

The env var for a field is its name upper-cased (case-insensitive load); see
`config/settings.py` `model_config`.
"""

from __future__ import annotations

from config.settings import Settings
from tests.helpers import PROJECT_ROOT, env_example_declarations


def _settings_env_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_every_setting_is_in_env_example() -> None:
    expected = _settings_env_keys()
    documented = env_example_declarations().keys()
    missing = sorted(expected - documented)
    assert not missing, (
        "Settings fields absent from .env.example (add each as `KEY=` or a "
        f"commented `# KEY=<default>`): {missing}"
    )


def test_every_setting_is_in_configuration_doc() -> None:
    expected = _settings_env_keys()
    text = (PROJECT_ROOT.parent / "docs/configuration.md").read_text(encoding="utf-8")
    missing = sorted(key for key in expected if key not in text)
    assert not missing, (
        "Settings fields absent from docs/configuration.md (document each under "
        f"the matching section): {missing}"
    )
