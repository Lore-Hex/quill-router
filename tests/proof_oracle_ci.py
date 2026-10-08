"""Keep marker selection and the dedicated workflow's file scope in agreement."""
import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def proof_oracle_job():
    return yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']['proof-oracle']


def proof_oracle_command(job):
    commands = [shlex.split(step['run']) for step in job['steps']
                if 'uv run pytest' in step.get('run', '')]
    assert len(commands) == 1
    return commands[0]


def check_proof_oracle_paths(paths):
    command = proof_oracle_command(proof_oracle_job())
    files = {ROOT / arg for arg in command if arg.startswith('tests/')}
    assert files and all(path.is_file() for path in files)
    missing = set(paths) - files
    assert not missing, f'proof_oracle tests missing from dedicated CI job: {sorted(missing)}'
