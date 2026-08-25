from evals.models import (
    ModelSpec,
    build_eval_provider,
    eval_provider_config,
    load_models,
)


def test_codex_model_goes_into_codex_model_field_not_settings_default():
    spec = ModelSpec(
        label="codex-cand",
        provider_name="codex",
        model="gpt-5.5-some-variant",
        base_url="",
        api_key_env="",
    )
    cfg = eval_provider_config(spec, api_key="")
    # The factory resolves codex as `config.codex_model or config.model`; the
    # candidate model MUST land in codex_model so it is never overridden by a
    # settings default.
    assert cfg.codex_model == "gpt-5.5-some-variant"
    assert cfg.provider_name == "codex"


def test_non_codex_model_uses_model_field():
    spec = ModelSpec(
        label="anthropic-cand",
        provider_name="anthropic",
        model="claude-x",
        base_url="",
        api_key_env="",
    )
    cfg = eval_provider_config(spec, api_key="k")
    assert cfg.model == "claude-x"
    assert cfg.api_key == "k"


def test_openai_compat_eval_forwards_reasoning_effort_and_request_id_header():
    spec = ModelSpec(
        label="deepseek-flash-opencode",
        provider_name="openai_compat",
        model="deepseek-v4-flash",
        base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        reasoning_effort="xhigh",
        request_id_header="X-Client-Request-Id",
    )

    cfg = eval_provider_config(spec, api_key="k")

    assert cfg.openai_reasoning_effort == "xhigh"
    assert cfg.openai_request_id_header == "X-Client-Request-Id"


def test_build_eval_provider_guards_effective_model_mismatch():
    class FakeProvider:
        model = "WRONG-model"

    spec = ModelSpec(
        label="x", provider_name="anthropic", model="claude-x", base_url="", api_key_env=""
    )
    try:
        build_eval_provider(spec, _create=lambda cfg: FakeProvider())
    except ValueError as exc:
        assert "effective model" in str(exc)
    else:
        raise AssertionError("expected a guard ValueError on model mismatch")


def test_load_models_parses_baseline_candidate_judge(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "baseline:\n"
        "  label: kimi\n"
        "  provider_name: openai_compat\n"
        "  model: kimi-k2.6\n"
        "  base_url: http://x\n"
        "  api_key_env: MODEL_API_KEY\n"
        "candidates:\n"
        "  newone:\n"
        "    provider_name: anthropic\n"
        "    model: claude-x\n"
        "    max_output_tokens: 32768\n"
        "judge:\n"
        "  label: judge\n"
        "  provider_name: anthropic\n"
        "  model: claude-opus\n"
        "  api_key_env: ANTHROPIC_API_KEY\n"
    )
    models = load_models(path)
    assert models.baseline.model == "kimi-k2.6"
    assert models.candidates["newone"].model == "claude-x"
    assert models.candidates["newone"].max_output_tokens == 32768
    assert models.candidates["newone"].effective_max_tokens(65_536) == 32_768
    assert models.candidates["newone"].effective_max_tokens(16_384) == 16_384
    assert models.judge.model == "claude-opus"


def test_load_models_tolerates_missing_candidates_and_panel(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "baseline:\n"
        "  provider_name: openai_compat\n"
        "  model: kimi-k2.6\n"
        "  base_url: http://x\n"
        "judge:\n"
        "  provider_name: anthropic\n"
        "  model: claude-opus\n"
    )
    models = load_models(path)
    assert models.candidates == {}
    assert models.judge_panel == []


def test_load_models_rejects_openai_compat_with_empty_base_url(tmp_path):
    # An unfilled template must fail at load time, not as scrubbed error replies
    # after the run has already burned its token budget.
    path = tmp_path / "models.yaml"
    path.write_text(
        "baseline:\n"
        "  provider_name: openai_compat\n"
        "  model: kimi-k2.6\n"
        '  base_url: ""\n'
        "judge:\n"
        "  provider_name: anthropic\n"
        "  model: claude-opus\n"
    )
    try:
        load_models(path)
    except ValueError as exc:
        assert "base_url" in str(exc)
    else:
        raise AssertionError("expected load_models to reject an empty openai_compat base_url")


def test_public_evals_models_example_is_valid():
    # The shipped evals baseline must be loadable: a broken or unfilled template
    # should fail here, not mid-run after burning the token budget. We deliberately
    # do NOT assert it equals whichever model production currently runs as chat,
    # because the production brain is a config choice, not a correctness property,
    # and pinning it would break this test on every model swap.
    from evals.run import EVALS_DIR

    models = load_models(EVALS_DIR / "models.example.yaml")
    assert models.baseline.model
    assert models.baseline.provider_name
    assert models.judge.model
