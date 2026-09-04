from pathlib import Path


def test_service_template_caps_aggregate_child_resources() -> None:
    lines = {
        line.strip()
        for line in Path("deploy/kimi.service.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "TasksMax=128" in lines
    assert "MemoryMax=2G" in lines
    assert "CPUQuota=200%" in lines
