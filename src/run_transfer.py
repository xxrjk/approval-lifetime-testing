"""Reexecute all registered source/encoding conditions through the native library."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--existing-source',type=Path);a=p.parse_args()
    command=[sys.executable,str(ROOT/'src/prepare_viewflow.py')]
    if a.existing_source:command+=['--existing',str(a.existing_source.resolve())]
    subprocess.run(command,cwd=ROOT,check=True)
    sources=json.loads((ROOT/'results/transfer/sources.json').read_text())
    for treatment,source in sources['treatments'].items():
        for value in (0,4):
            command=[sys.executable,str(ROOT/'src/viewflow_transfer.py'),'--source',str(ROOT/source),
                     '--treatment',treatment,'--approved',str(value)]
            subprocess.run(command,cwd=ROOT,check=True)
    subprocess.run([sys.executable,str(ROOT/'src/analyze_transfer.py')],cwd=ROOT,check=True)


if __name__=='__main__':main()
