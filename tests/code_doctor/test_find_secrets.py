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


def test_redacted_key_block_is_a_candidate(repo, run_script):
    """BEGIN / <redacted> / END has both markers and no key."""
    repo.write("README.md",
               "Example:\n\n-----BEGIN RSA PRIVATE KEY-----\n"
               "<redacted>\n-----END RSA PRIVATE KEY-----\n")
    repo.commit("docs")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "private_key_material")[0]
    assert record["kind"] == "candidate"


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


# --------------------------------------------------------------------------- #
# A1 — tracked-ness of the *filename* is not tracked-ness of the *bytes*.
# --------------------------------------------------------------------------- #

def test_credential_added_to_tracked_file_without_committing_is_a_candidate(repo, run_script):
    """`git ls-files` proves the path is tracked, not that these bytes are.

    A developer who adds a key to an already-tracked file without staging or
    committing must not get "committed to the repository — revoke it now.":
    the committed/staged blob for this path does not contain the credential.
    """
    repo.write("config.yaml", "existing: true\n")
    repo.commit("base")
    repo.write("config.yaml", "existing: true\naws_key: AKIA2E0RSCHEMAQ7VXBN\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "cloud_credential")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""


def test_credential_staged_but_not_committed_is_a_finding(repo, run_script):
    """`git add` (no commit) puts the bytes in git's index — that counts.

    This is the flip side of the test above: once the credential is actually
    staged, the index blob (`git show :path`) contains it, so it is treated
    the same as committed rather than held to "was it ever pushed".
    """
    repo.write("config.yaml", "existing: true\n")
    repo.commit("base")
    repo.write("config.yaml", "existing: true\naws_key: AKIA2E0RSCHEMAQ7VXBN\n")
    repo.git("add", "config.yaml")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "cloud_credential")[0]
    assert record["kind"] == "finding"


# --------------------------------------------------------------------------- #
# A2 — unknown tracking state must not produce a finding.
# --------------------------------------------------------------------------- #

def test_git_unavailable_makes_credential_a_candidate_not_a_finding(tmp_path, run_script):
    """None means 'could not establish' — the same treatment False gets,
    with a different reason (A2)."""
    (tmp_path / "config.yaml").write_text("aws_key: AKIA2E0RSCHEMAQ7VXBN\n", encoding="utf-8")
    result = run_script(SCRIPT, tmp_path, "--format", "json")
    # `tracked` staying None adds a `tracking_state` completeness note, which
    # wraps the JSON payload in {"completeness": ..., "findings": [...]}
    # instead of the bare list every other test in this file gets.
    payload = json.loads(result.stdout)
    findings = [f for f in payload["findings"] if f["smell_type"] == "cloud_credential"]
    record = findings[0]
    assert record["kind"] == "candidate"
    assert any("unavailable" in reason for reason in record["also_caused_by"])


# --------------------------------------------------------------------------- #
# A3 — only the first credential on a line was reported; all distinct ones
# must be.
# --------------------------------------------------------------------------- #

def test_multiple_distinct_credentials_on_one_line_are_all_reported(repo, run_script):
    repo.write("secrets.txt",
               "AKIA2E0RSCHEMAQ7VXBN ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    records = records_of(result, "cloud_credential")
    assert len(records) == 2
    snippets = {r["code_snippet"] for r in records}
    assert any(s.startswith("AKIA2E0RSC") for s in snippets)
    assert any(s.startswith("ghp_ABCDEF") for s in snippets)


def test_distinct_high_entropy_secret_beside_a_credential_is_still_reported(repo, run_script):
    """Suppression of the generic candidate is span-specific, not whole-line."""
    repo.write("mix.env",
               'aws_key: AKIA2E0RSCHEMAQ7VXBN\n'
               'secret_token="hJ8s0Kd93LwmZq2XvRt7YbNc4PfGh6Aa"\n')
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "cloud_credential")
    assert records_of(result, "hardcoded_secret_assignment")


# --------------------------------------------------------------------------- #
# A4 — encrypted PKCS#8 armor is standard and was missed.
# --------------------------------------------------------------------------- #

def test_encrypted_private_key_is_a_finding(repo, run_script):
    repo.write("key.pem",
               "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
               "MIIFDjBABgkqhkiG9w0BBQ0wMzAbBgkqhkiG9w0BBQwwDgQIWZ6vJIYmdBUCAggA\n"
               "-----END ENCRYPTED PRIVATE KEY-----\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "private_key_material")[0]
    assert record["kind"] == "finding"


# --------------------------------------------------------------------------- #
# A5 — single-line PEM with escaped `\n` newlines was downgraded to a
# header-only candidate.
# --------------------------------------------------------------------------- #

def test_single_line_pem_with_escaped_newlines_is_a_finding(repo, run_script):
    """A whole key on one physical line, `\\n`-escaped, must not lose its body."""
    payload = ('{"private_key": "-----BEGIN PRIVATE KEY-----\\n'
              'MIIEowIBAAKCAQEAx7Vn9kQmPqLs3TfWuYcZgHjKdNbRt4VpXeAoCiMlSzUyBgHw\\n'
              '-----END PRIVATE KEY-----\\n"}\n')
    repo.write("service-account.json", payload)
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "private_key_material")[0]
    assert record["kind"] == "finding"


# --------------------------------------------------------------------------- #
# B2 — `--ignore a, b` must strip whitespace on every entry, this detector
# included.
# --------------------------------------------------------------------------- #

def test_ignore_strips_whitespace_between_entries(repo, run_script):
    repo.write("settings.py", 'API_SECRET = "hJ8s0Kd93LwmZq2XvRt7YbNc4PfGh6Aa"\n')
    result = run_script(SCRIPT, repo.path, "--format", "json",
                        "--ignore", "cloud_credential, hardcoded_secret_assignment")
    assert not records_of(result, "hardcoded_secret_assignment")


# --------------------------------------------------------------------------- #
# C2 — UTF-16 text was classified binary and skipped entirely.
# --------------------------------------------------------------------------- #

def test_credential_in_a_utf16_file_is_still_found(repo, run_script):
    """UTF-16 is NUL-riddled by construction; a BOM marks it as text anyway."""
    (repo.path / "config.txt").write_bytes(
        "aws_key: AKIA2E0RSCHEMAQ7VXBN\n".encode("utf-16"))
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "cloud_credential")
