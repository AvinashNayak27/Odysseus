from pathlib import Path

import pytest

from odysseus.yukon.parser import (
    SUPPORTED_CLI_VERSION,
    YukonParseError,
    parse_benchmark_list,
    parse_benchmark_show,
    parse_clone_report,
    parse_version,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "yukon"
LIGHTER_ID = "1b5f7092-c2df-4ee8-94e7-1c03836d6b4e"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text().replace("\\033", "\x1b")


def test_parses_observed_versioned_aligned_human_cli_contract() -> None:
    version = parse_version(fixture("version.txt"))
    assert version.value == SUPPORTED_CLI_VERSION
    records = parse_benchmark_list(fixture("benchmark-list.txt"), cli_version=version)
    assert [item.goal for item in records] == ["lower", "higher", "lower"]
    assert records[1].benchmark_id == LIGHTER_ID
    show = parse_benchmark_show(
        fixture("benchmark-show.txt"), cli_version=version, benchmark_id=LIGHTER_ID
    )
    assert show.goal == "higher score is better"
    assert show.source_url == "https://github.com/example-org/sanitized-challenge.git"
    assert show.source_ref == "7e3d2b41c5f6a891"
    assert show.source_branch == "main"
    assert show.setup_command == "bun install --frozen-lockfile"
    assert show.benchmark_command == "bun run benchmark"
    assert show.description.startswith("This synthetic, sanitized fixture")


def test_rejects_layout_drift_missing_fields_and_non_uuid_identifiers() -> None:
    version = parse_version(fixture("version.txt"))
    with pytest.raises(YukonParseError, match="columns"):
        parse_benchmark_list("Benchmark  Status  ID\nThing  open  x\n", cli_version=version)
    with pytest.raises(YukonParseError, match="UUID"):
        parse_benchmark_show(
            "Goal  higher score is better\n", cli_version=version, benchmark_id="x"
        )
    with pytest.raises(YukonParseError, match="missing"):
        parse_benchmark_show(
            "Goal  higher score is better\n", cli_version=version, benchmark_id=LIGHTER_ID
        )


def test_parses_aligned_clone_paths_and_restart_as_a_hard_stop_input() -> None:
    report = parse_clone_report(
        fixture("clone-restart.txt"), cli_version=parse_version(fixture("version.txt"))
    )
    assert report.root == "/tmp/yukon-clones/lighter"
    assert report.workdir.endswith("challenges/prover")
    assert report.editable_paths == ("src", "Cargo.toml")
    assert report.restart_requested
