"""Fetch pinned public Viewflow sources and apply exactly one upstream-line contrast."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
PINS={'2.3.1':'d8a05462590eb8ef36fb435b92155b97d140db66',
      '2.3.2':'aeab1afab84a52844eeead2698e82488d62ce998'}
OLD='return value if value else self._default'
NEW='return self._default if value is None or value == "" else value'


def main():
    p=argparse.ArgumentParser();p.add_argument('--existing',type=Path);a=p.parse_args()
    folder=ROOT/'tmp/transfer_sources';folder.mkdir(parents=True,exist_ok=True)
    repo=a.existing or folder/'repository'
    if not (repo/'.git').exists():
        subprocess.run(['git','clone','--no-checkout','--depth','1','--branch','2.3.1',
                        'https://github.com/viewflow/viewflow.git',str(repo)],check=True,timeout=240)
    for version,commit in PINS.items():
        have=subprocess.run(['git','-C',str(repo),'cat-file','-e',commit+'^{commit}'],capture_output=True)
        if have.returncode:
            subprocess.run(['git','-C',str(repo),'fetch','--depth','1','origin',commit],check=True,timeout=240)
        subprocess.run(['git','-C',str(repo),'cat-file','-e',commit+'^{commit}'],check=True)
    destinations={}
    for treatment,version in [('original','2.3.1'),('getter_fix','2.3.1'),('fixed','2.3.2'),('reverted','2.3.2')]:
        dest=folder/treatment
        if not dest.exists():
            # Extract the pinned upstream tree into a separate treatment directory.
            import io,tarfile
            raw=subprocess.check_output(['git','-C',str(repo),'archive',PINS[version]])
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                archive.extractall(dest,filter='data')
            file=dest/'viewflow/fsm/base.py'
            if treatment in ('getter_fix','reverted'):
                old,new=(OLD,NEW) if treatment=='getter_fix' else (NEW,OLD)
                text=file.read_text();assert text.count(old)==1;file.write_text(text.replace(old,new))
        # Verify the entire Python code tree against the pinned commit, allowing
        # only the registered one-line change in the two contrast treatments.
        paths=subprocess.check_output(['git','-C',str(repo),'ls-tree','-r','--name-only',PINS[version],'viewflow'],text=True).splitlines()
        for path in paths:
            if not path.endswith('.py'):continue
            expected=subprocess.check_output(['git','-C',str(repo),'show',PINS[version]+':'+path])
            if path=='viewflow/fsm/base.py':
                if treatment=='getter_fix':expected=expected.replace(OLD.encode(),NEW.encode())
                if treatment=='reverted':expected=expected.replace(NEW.encode(),OLD.encode())
            assert (dest/path).read_bytes()==expected,(treatment,path)
        destinations[treatment]=str(dest.relative_to(ROOT))
    output=ROOT/'results/transfer';output.mkdir(parents=True,exist_ok=True)
    (output/'sources.json').write_text(json.dumps({'url':'https://github.com/viewflow/viewflow.git','pins':PINS,
        'treatments':destinations,'contrast_file':'viewflow/fsm/base.py','old_line':OLD,'fixed_line':NEW,
        'all_python_sources_verified':True},indent=2)+'\n')
    print(json.dumps(destinations))


if __name__=='__main__':main()
