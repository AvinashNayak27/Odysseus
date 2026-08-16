from pathlib import Path

from odysseus.yukon.frontier import quarantine_submissions

BENCHMARK_ID = "1b5f7092-c2df-4ee8-94e7-1c03836d6b4e"


def test_submissions_are_inert_neutered_and_redacted_without_public_render_failure(
    tmp_path: Path,
) -> None:
    artifact = quarantine_submissions(
        benchmark_id=BENCHMARK_ID,
        text=(
            "\x1b[31m<script>bad()</script> token=synthetic-secret\x1b[0m\n"
            "Traceback (most recent call last): hostile data\n"
            "https://untrusted.invalid/never-fetch"
        ),
        quarantine_root=tmp_path,
    )
    content = artifact.path.read_text()
    assert "\x1b" not in content
    assert "<script" not in content.casefold()
    assert "synthetic-secret" not in content
    assert "[REDACTED TRACE]" in content
    assert "[UNFOLLOWED URL]" in content
    assert artifact.source == "yukon submissions --all"
