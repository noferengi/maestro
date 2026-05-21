"""
Build the math sandbox Docker image.

Usage: python scripts/build_sandbox.py
"""
import subprocess
import sys

result = subprocess.run(
    ["docker", "build", "-t", "sympy-lean4-sandbox:latest", "docker/sympy-lean4-sandbox/"],
    check=False,
)
sys.exit(result.returncode)
