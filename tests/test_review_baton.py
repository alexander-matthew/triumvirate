"""Review-baton persistence and prompt compaction behavior."""
from __future__ import annotations

from types import SimpleNamespace

from agent_loop.lib import review_baton
from agent_loop.lib.persona import Persona
from agent_loop.phases import review_pr


def _settings(tmp_path):
    return SimpleNamespace(state_dir=tmp_path)


def test_append_review_persists_latest_head(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(review_baton, "settings", lambda: _settings(tmp_path))

    review_baton.append_review(
        pr_number=17,
        head_sha="abc123",
        reviewer="codex",
        round_n=1,
        verdict="APPROVE",
        summary="looks good",
        checklist="- [x] tests",
        notes="",
        additions=10,
        deletions=2,
        changed_files=1,
    )

    assert review_baton.has_reviews(17)
    assert review_baton.latest_head_sha(17) == "abc123"
    data = review_baton.load(17)
    assert data["reviews"][0]["reviewer"] == "codex"
    assert data["reviews"][0]["additions"] == 10


def test_render_includes_prior_review_and_delta(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(review_baton, "settings", lambda: _settings(tmp_path))
    review_baton.append_review(
        pr_number=18,
        head_sha="oldhead",
        reviewer="gemini",
        round_n=2,
        verdict="REQUEST_CHANGES",
        summary="remove unwrap",
        checklist="- [ ] src/app.rs:270 unwrap remains",
        notes="focus on panic paths",
        additions=30,
        deletions=5,
        changed_files=2,
    )

    rendered = review_baton.render(
        18,
        current_head="newhead",
        delta_diff="diff --git a/src/app.rs b/src/app.rs\n-unwrap()\n+if let Some(x) = x",
    )

    assert "## Review Baton" in rendered
    assert "remove unwrap" in rendered
    assert "src/app.rs:270" in rendered
    assert "diff --git" in rendered
    assert "oldhead" in rendered
    assert "newhead" in rendered


def test_clear_removes_baton_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(review_baton, "settings", lambda: _settings(tmp_path))
    review_baton.append_review(
        pr_number=19,
        head_sha="abc123",
        reviewer="codex",
        round_n=1,
        verdict="APPROVE",
        summary="ok",
        checklist="- [x] done",
        notes="",
        additions=1,
        deletions=0,
        changed_files=1,
    )

    assert review_baton.path_for(19).exists()
    review_baton.clear(19)
    assert not review_baton.path_for(19).exists()


def test_build_prompt_omits_baton_on_initial_review() -> None:
    persona = Persona(
        name="reviewer-codex",
        cli="codex",
        role="",
        voice="",
        timeout_min=1,
        prompt_template="Review PR #$PR_NUMBER: $PR_TITLE\n$ISSUE_BODY",
    )
    pr = {
        "number": 3,
        "title": "Add tests",
        "body": "Closes #2",
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
    }

    prompt = review_pr._build_prompt(
        persona,
        pr,
        "issue body",
        wrapper_facts="## Wrapper-Computed Review Facts\n- Diff size: +1/-0",
    )

    assert "Review PR #3: Add tests" in prompt
    assert "Wrapper-Computed Review Facts" in prompt
    assert "Runtime Optimization Context" not in prompt


def test_build_prompt_appends_baton_for_follow_up_review() -> None:
    persona = Persona(
        name="reviewer-gemini",
        cli="gemini",
        role="",
        voice="",
        timeout_min=1,
        prompt_template="Review PR #$PR_NUMBER: $PR_TITLE",
    )
    pr = {
        "number": 4,
        "title": "Fix unwrap",
        "body": "Closes #2",
        "additions": 2,
        "deletions": 1,
        "changedFiles": 1,
    }

    prompt = review_pr._build_prompt(
        persona,
        pr,
        "issue body",
        baton_context="## Review Baton\nPrior reviewer saw one panic.",
    )

    assert "Runtime Optimization Context" in prompt
    assert "Prior reviewer saw one panic" in prompt
    assert "Keep the same review standards as usual" in prompt


def test_wrapper_facts_lists_guardrail_inputs(monkeypatch) -> None:
    settings = SimpleNamespace(
        max_diff_loc=400,
        sensitive_path_prefixes=("src/agent.rs",),
        librarian_trigger_paths=("Cargo.toml",),
    )
    monkeypatch.setattr(review_pr, "settings", lambda: settings)
    pr = {
        "additions": 25,
        "deletions": 5,
        "labels": [{"name": "agent:authored-by-claude"}],
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"name": "clippy"}, {"name": "test"}],
    }

    facts = review_pr._wrapper_facts(
        pr,
        head_sha="abc123",
        changed=["src/agent.rs", "README.md"],
        bad_paths=["Cargo.toml"],
        too_large=False,
    )

    assert "Head SHA: `abc123`" in facts
    assert "Diff size: +25/-5 (30 LOC), cap 400: within cap" in facts
    assert "`src/agent.rs`" in facts
    assert "Protected path violations: `Cargo.toml`" in facts
    assert "Sensitive paths touched: `src/agent.rs`" in facts
    assert "Status checks seen: clippy, test" in facts
