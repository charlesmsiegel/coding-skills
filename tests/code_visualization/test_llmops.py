"""Tests for llmops.py — LLM call sites, models, prompt lineage, mechanical gaps.

The detector's job is facts a reader can check: where the model is called, which
model, with which parameters, fed by which prompt file. The gaps it reports are
mechanical absences (no max_tokens, no timeout, a model id frozen at the call
site), never a judgment about prompt quality.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-visualization" / "scripts"


@pytest.fixture
def llmops(load_module):
    return load_module(SCRIPTS, "llmops")


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def index(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)).replace("\\", "/"): p for p in root.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- #
# Call sites
# --------------------------------------------------------------------------- #


def test_an_anthropic_call_site_is_detected_with_model_and_params(llmops, tmp_path):
    write(tmp_path, "agent/run.py",
          "import anthropic\n"
          "client = anthropic.Anthropic()\n"
          "def ask(q):\n"
          "    return client.messages.create(\n"
          "        model='claude-opus-5',\n"
          "        max_tokens=1024,\n"
          "        temperature=0.2,\n"
          "        messages=[{'role': 'user', 'content': q}],\n"
          "    )\n")

    sites = llmops.scan(index(tmp_path)).sites

    assert len(sites) == 1
    site = sites[0]
    assert (site.path, site.line) == ("agent/run.py", 4)
    assert site.provider == "anthropic"
    assert site.api == "messages.create"
    assert site.model == "claude-opus-5"
    assert site.params["max_tokens"] is True
    assert site.params["temperature"] is True
    assert site.params["timeout"] is False


def test_an_openai_chat_completion_is_detected(llmops, tmp_path):
    write(tmp_path, "svc/chat.js",
          "const res = await openai.chat.completions.create({\n"
          "  model: 'gpt-5',\n"
          "  messages,\n"
          "});\n")

    site = llmops.scan(index(tmp_path)).sites[0]

    assert (site.provider, site.api, site.model) == ("openai", "chat.completions.create", "gpt-5")


def test_a_gemini_call_is_detected(llmops, tmp_path):
    write(tmp_path, "svc/g.py",
          "import google.generativeai as genai\n"
          "resp = model.generate_content(prompt)\n")

    site = llmops.scan(index(tmp_path)).sites[0]

    assert site.provider == "google"


def test_a_call_site_never_borrows_another_call_s_model(llmops, tmp_path):
    """Reporting a model the call does not name would be confidently wrong: the
    inventory still carries the id, but this site says it does not name one."""
    write(tmp_path, "agent/run.py",
          "fast = client.messages.create(model='claude-haiku-4-5', max_tokens=8)\n"
          "slow = client.messages.create(model=CONFIGURED, max_tokens=8)\n")

    sites = llmops.scan(index(tmp_path)).sites

    assert [s.model for s in sites] == ["claude-haiku-4-5", None]


def test_a_file_naming_two_providers_is_labelled_by_the_api_it_calls(llmops, tmp_path):
    write(tmp_path, "agent/router.py",
          "import anthropic\n"
          "import openai\n"
          "res = oa.chat.completions.create(model='gpt-5')\n")

    site = llmops.scan(index(tmp_path)).sites[0]

    assert site.provider == "openai"


def test_a_repo_with_no_llm_usage_reports_nothing(llmops, tmp_path):
    write(tmp_path, "app/math.py", "def add(a, b):\n    return a + b\n")

    result = llmops.scan(index(tmp_path))

    assert result.sites == []
    assert result.models == {}
    assert result.gaps == []


def test_naming_an_api_without_calling_it_is_not_a_call_site(llmops, tmp_path):
    """Code that talks *about* the SDK — a detector's pattern table, a doc
    example, a config of allowed methods — is not code that calls it."""
    write(tmp_path, "tools/catalog.py",
          "SUPPORTED = ['messages.create', 'chat.completions.create']\n"
          "DOC = 'call messages.create to talk to the model'\n")

    assert llmops.scan(index(tmp_path)).sites == []


def test_the_word_model_alone_is_not_an_llm_call(llmops, tmp_path):
    """An ORM is full of models; none of them are language models."""
    write(tmp_path, "db/models.py",
          "class User(Model):\n"
          "    def create(self):\n"
          "        return self.objects.create(name='x')\n")

    assert llmops.scan(index(tmp_path)).sites == []


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


def test_model_ids_are_collected_even_away_from_a_call_site(llmops, tmp_path):
    write(tmp_path, "conf/settings.py", "DEFAULT_MODEL = 'claude-sonnet-5'\n")
    write(tmp_path, "agent/run.py", "resp = client.messages.create(model=DEFAULT_MODEL)\n")

    models = llmops.scan(index(tmp_path)).models

    assert "claude-sonnet-5" in models
    assert models["claude-sonnet-5"] == ["conf/settings.py:1"]


# --------------------------------------------------------------------------- #
# Prompt lineage
# --------------------------------------------------------------------------- #


def test_a_prompt_file_loaded_by_a_call_site_file_is_a_prompt_asset(llmops, tmp_path):
    write(tmp_path, "agent/run.py",
          "SYSTEM = open('prompts/system.md').read()\n"
          "resp = client.messages.create(model='claude-opus-5', max_tokens=8, system=SYSTEM)\n")
    write(tmp_path, "prompts/system.md", "You are helpful.\n")
    refs = [("agent/run.py", "prompts/system.md")]

    assets = llmops.scan(index(tmp_path), refs=refs).prompt_assets

    assert assets == {"prompts/system.md": ["agent/run.py"]}


def test_a_long_inline_prompt_is_reported(llmops, tmp_path):
    body = "You are a router. " * 40
    write(tmp_path, "agent/run.py",
          f'SYSTEM = """{body}"""\n'
          "resp = client.messages.create(model='claude-opus-5', max_tokens=8)\n")

    prompts = llmops.scan(index(tmp_path)).inline_prompts

    assert len(prompts) == 1
    assert prompts[0]["path"] == "agent/run.py"
    assert prompts[0]["chars"] >= 400


# --------------------------------------------------------------------------- #
# Gaps — mechanical absences only
# --------------------------------------------------------------------------- #


def test_an_anthropic_call_without_max_tokens_is_a_gap(llmops, tmp_path):
    write(tmp_path, "agent/run.py",
          "resp = client.messages.create(model='claude-opus-5', messages=msgs)\n")

    kinds = {g["kind"] for g in llmops.scan(index(tmp_path)).gaps}

    assert "no-max-tokens" in kinds


def test_a_call_with_neither_timeout_nor_retry_is_a_gap(llmops, tmp_path):
    write(tmp_path, "agent/run.py",
          "resp = client.messages.create(model='claude-opus-5', max_tokens=8)\n")

    gaps = llmops.scan(index(tmp_path)).gaps
    timeout_gaps = [g for g in gaps if g["kind"] == "no-timeout-or-retry"]

    assert timeout_gaps and timeout_gaps[0]["cite"] == "agent/run.py:1"


def test_a_retry_decorator_in_scope_clears_the_timeout_gap(llmops, tmp_path):
    write(tmp_path, "agent/run.py",
          "@retry(max_attempts=3)\n"
          "def ask(q):\n"
          "    return client.messages.create(model='claude-opus-5', max_tokens=8)\n")

    kinds = {g["kind"] for g in llmops.scan(index(tmp_path)).gaps}

    assert "no-timeout-or-retry" not in kinds


def test_a_model_id_frozen_at_the_call_site_is_a_gap(llmops, tmp_path):
    write(tmp_path, "agent/run.py",
          "resp = client.messages.create(model='claude-opus-5', max_tokens=8, timeout=30)\n")
    write(tmp_path, "agent/cfg.py",
          "resp = client.messages.create(model=settings.MODEL, max_tokens=8, timeout=30)\n")

    gaps = [g for g in llmops.scan(index(tmp_path)).gaps if g["kind"] == "hardcoded-model"]

    assert [g["cite"] for g in gaps] == ["agent/run.py:1"]


def test_an_interpolated_system_prompt_is_flagged_as_an_injection_surface(llmops, tmp_path):
    write(tmp_path, "agent/run.py",
          "resp = client.messages.create(\n"
          "    model='claude-opus-5', max_tokens=8, timeout=30,\n"
          '    system=f"You are helping {user_role}",\n'
          ")\n")

    kinds = {g["kind"] for g in llmops.scan(index(tmp_path)).gaps}

    assert "interpolated-system-prompt" in kinds
