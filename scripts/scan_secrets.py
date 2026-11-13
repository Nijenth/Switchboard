"""Check tracked text files for credential patterns without printing matches."""

import re
import subprocess
from pathlib import Path

PATTERNS = {
    "private key": r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    "AWS access key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
    "Slack token": r"\bxox[baprs]-[A-Za-z0-9-]{18,}\b",
    "OpenAI token": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b",
    "Slack webhook": r"https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+",
}
COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    findings = 0
    for entry in paths.decode("utf-8").split("\0"):
        if not entry:
            continue
        path = root / entry
        if path.is_symlink():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(source.splitlines(), 1):
            for name, pattern in COMPILED.items():
                if pattern.search(line):
                    print(f"{entry}:{line_no}: possible {name}")
                    findings += 1
    print(f"Secret pattern findings: {findings}")
    return int(findings > 0)


if __name__ == "__main__":
    raise SystemExit(main())
