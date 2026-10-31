"""Run the whole evaluation: the 32 labelled samples, then the custom cases.

Usage (from the repository root):  python evaluation/run_all.py [--offline]
"""
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    print("== labelled samples (dataset/sample_output.csv)")
    runpy.run_path(str(HERE / "evaluate.py"), run_name="__main__")
    print("\n== custom cases")
    runpy.run_path(str(HERE / "custom_cases.py"), run_name="__main__")
    sys.exit(0)
