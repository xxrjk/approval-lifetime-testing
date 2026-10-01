"""One local Flutter process; persistent newline-JSON connection to real SQLite."""
import json
from pathlib import Path
import shutil
import socket
import subprocess
import time
import psutil

ROOT=Path(__file__).resolve().parents[1]


class Backend:
    def __init__(self, flutter='flutter', cache=None, offline=False, name='rpc_app',
                 test_file='approval_rpc_test.dart'):
        self.app=ROOT/'tmp'/name;self.app.mkdir(parents=True,exist_ok=True)
        source=ROOT/'artifact_templates/pubspec.yaml'
        if not source.exists():source=ROOT/'app/pubspec.yaml'
        shutil.copy2(source,self.app/'pubspec.yaml')
        shutil.copy2(ROOT/'app/pubspec.lock',self.app/'pubspec.lock')
        for folder,names in [('lib',['db.dart','models.dart','audit_binding.dart','fsrs.dart','stats.dart']),
                             ('test',['approval_rpc_test.dart','effects_test.dart',
                                      'lifecycle_test.dart','admission_test.dart'])]:
            (self.app/folder).mkdir(exist_ok=True)
            for name in names:shutil.copy2(ROOT/'app'/folder/name,self.app/folder/name)
        if test_file != 'approval_rpc_test.dart':
            shutil.copy2(ROOT/'app/test'/test_file, self.app/'test'/test_file)
        if cache:
            target=self.app/'.dart_tool/hooks_runner/shared/sqlite3/build'
            for path in Path(cache).glob('download-*'):
                if path.is_dir():shutil.copytree(path,target/path.name,dirs_exist_ok=True)
        self.log=(self.app/'bridge.log').open('w');started=time.perf_counter()
        subprocess.run([flutter,'pub','get']+(['--offline'] if offline else []),cwd=self.app,
                       stdout=self.log,stderr=subprocess.STDOUT,check=True)
        ready=self.app/'ready.json'
        if ready.exists():ready.unlink()
        self.process=subprocess.Popen([flutter,'test','--no-pub','--reporter','expanded',
            '--dart-define=RPC_READY='+str(ready),'test/'+test_file],cwd=self.app,
            stdout=self.log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+90;port=None
        while port is None:   # ready.json can exist before its content is written
            if self.process.poll() is not None:raise RuntimeError((self.app/'bridge.log').read_text())
            if time.monotonic()>deadline:raise TimeoutError('Bridge did not start')
            if ready.exists():
                try:port=json.loads(ready.read_text())['port']
                except (json.JSONDecodeError,KeyError):pass
            time.sleep(.05)
        self.socket=socket.create_connection(('127.0.0.1',port),timeout=60)
        self.socket.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        self.stream=self.socket.makefile('rwb');self.startup_seconds=time.perf_counter()-started

    def call(self, command):
        self.stream.write((json.dumps(command)+'\n').encode());self.stream.flush()
        line=self.stream.readline()
        if not line:raise RuntimeError('Bridge closed unexpectedly')
        result=json.loads(line)
        if not result['ok']:raise RuntimeError(result)
        return result

    def rss_bytes(self):
        total=psutil.Process().memory_info().rss
        try:
            for process in [psutil.Process(self.process.pid),*psutil.Process(self.process.pid).children(recursive=True)]:
                try:total+=process.memory_info().rss
                except psutil.NoSuchProcess:pass
        except psutil.NoSuchProcess:pass
        return total

    def close(self):
        try:self.call({'op':'shutdown'})
        finally:
            self.stream.close();self.socket.close()
            self.process.wait(timeout=20);self.log.close()
            if self.process.returncode:raise RuntimeError((self.app/'bridge.log').read_text())

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
