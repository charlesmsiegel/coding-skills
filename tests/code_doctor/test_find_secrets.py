"""Secret detection over any text file."""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "find_secrets.py"


def records_of(result, smell_type):
    return [f for f in json.loads(result.stdout) if f["smell_type"] == smell_type]


def test_private_key_block_with_a_body_is_a_finding(repo, run_script):
    repo.write("deploy.sh",
               "#!/bin/sh\n-----BEGIN RSA PRIVATE KEY-----\n"
               "MIIEowIBAAKCAQEAx7Vn9kQmPqLs3TfWuYcZgHjKdNbRt4VpXeAoCiMlSzUyBgHw\n"
               "-----END RSA PRIVATE KEY-----\n")
    repo.commit("oops")  # the detector maps untracked to candidate, so commit it
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "private_key_material")[0]
    assert record["kind"] == "finding"
    assert record["severity"] == "high"


def test_bare_private_key_header_is_a_candidate(repo, run_script):
    """Documentation examples and fixtures show the header with no key."""
    repo.write("README.md", "Keys look like:\n\n-----BEGIN RSA PRIVATE KEY-----\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "private_key_material")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""


def test_a_credential_is_never_both_a_finding_and_a_candidate(repo, run_script):
    """A recognized token on a secret-shaped name must report once."""
    repo.write("settings.py", 'API_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert len(json.loads(result.stdout)) == 1
    assert records_of(result, "cloud_credential")
    assert not records_of(result, "hardcoded_secret_assignment")


def test_aws_access_key_is_a_finding(repo, run_script):
    repo.write("config.yaml", "aws_key: AKIA2E0RSCHEMAQ7VXBN\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "cloud_credential")


def test_documented_example_key_is_not_reported(repo, run_script):
    """AKIAIOSFODNN7EXAMPLE is AWS's own published placeholder.

    The wide walk deliberately reaches documentation, so a repo that quotes
    the vendor's example must not get revoke-and-purge advice for it.
    """
    repo.write("README.md", "For example:\n\n    aws_key: AKIAIOSFODNN7EXAMPLE\n")
    repo.commit("docs")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert not records_of(result, "cloud_credential")


def test_untracked_credential_is_a_candidate(repo, run_script):
    """A gitignored local token was never pushed; do not demand rotation."""
    repo.write(".gitignore", "local.yaml\n")
    repo.commit("ignore")
    repo.write("local.yaml", "aws_key: AKIA2E0RSCHEMAQ7VXBN\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "cloud_credential")[0]
    assert record["kind"] == "candidate"


def test_jwt_is_detected(repo, run_script):
    token = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
             "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkEifQ."
             "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVAdQssw5c")
    repo.write("config.yaml", f"auth: {token}\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "cloud_credential")


def test_high_entropy_assignment_is_a_candidate(repo, run_script):
    repo.write("settings.py", 'API_SECRET = "hJ8s0Kd93LwmZq2XvRt7YbNc4PfGh6Aa"\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "hardcoded_secret_assignment")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""
    assert record["also_caused_by"]


def test_unquoted_dotenv_assignment_is_detected(repo, run_script):
    """.env and YAML normally write the value bare — the highest-value case."""
    repo.write("prod.env", "API_TOKEN=hJ8s0Kd93LwmZq2XvRt7YbNc4PfGh6Aa\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "hardcoded_secret_assignment")


def test_low_entropy_placeholder_is_not_reported(repo, run_script):
    repo.write("settings.py", 'API_SECRET = "changeme"\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert json.loads(result.stdout) == []


def test_secret_in_a_lockfile_is_still_found(repo, run_script):
    """Secrets take the wide walk — a lockfile is not source but can leak."""
    repo.write("Cargo.lock", "token = AKIA2E0RSCHEMAQ7VXBN\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "cloud_credential")
