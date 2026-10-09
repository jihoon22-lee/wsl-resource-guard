"""Signal only processes created here; verify partial termination with real pidfds.

On CI this is run in a disposable user's transient scope. No operational service
or existing session is selected. The separate unrelated child must survive.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from wsl_resource_guard.actions import Actions
from wsl_resource_guard.processes import ProcessSnapshot,SessionUsage,_read_process,signal_verified_process,kill_block_reason

PROGRAM=r'''
import ctypes,json,os,signal,sys,time
from pathlib import Path
ctypes.CDLL(None).prctl(15,b'codex',0,0,0)
child=os.fork()
if child==0:
    ctypes.CDLL(None).prctl(15,b'node',0,0,0)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path(sys.argv[1]+'.ready').write_text('ready')
    while True:signal.pause()
else:
    deadline=time.monotonic()+5
    while not Path(sys.argv[1]+'.ready').exists() and time.monotonic()<deadline:time.sleep(.01)
    Path(sys.argv[1]).write_text(json.dumps({'root':os.getpid(),'child':child}))
    while True:signal.pause()
'''


def main():
    assert os.geteuid()!=0,'Run the fixture as an unprivileged owner'
    with tempfile.TemporaryDirectory(prefix='wrg-action-lifecycle-') as folder:
        path=Path(folder);pidfile=path/'pids.json'
        parent=subprocess.Popen([sys.executable,'-c',PROGRAM,str(pidfile)],cwd=folder)
        unrelated=subprocess.Popen(['/usr/bin/sleep','60'])
        child=None
        try:
            deadline=time.monotonic()+8
            while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.02)
            ids=json.loads(pidfile.read_text())
            clock=os.sysconf('SC_CLK_TCK')
            def snapshot():
                processes={}
                for pid in ids.values():
                    p=_read_process(pid,clock,0)
                    if p:processes[pid]=p
                usage=SessionUsage(ids['root'],'codex','codex','isolated fixture',1)
                return ProcessSnapshot(processes,[usage],[],{p:ids['root'] for p in processes},{ids['root']:'codex'})
            snap=snapshot();child=snap.processes[ids['child']]
            assert not kill_block_reason(snap.processes[ids['root']]),'Run this test in a transient scope, not a protected service'
            actions=Actions(path/'actions.json')
            preview=actions.preview(snap,ids['root'],{},'fixture')
            result=actions.execute(preview['id'],snap,{},'fixture',os.getuid())
            assert len(result['signalled'])==2,result
            parent.wait(timeout=5)
            status=actions.status(preview['id'],snapshot(),'fixture')
            assert status['status']=='remaining' and status['remaining']==[ids['child']],status
            assert unrelated.poll() is None,'Unrelated process was affected'
            again=actions.execute(preview['id'],snapshot(),{},'fixture',os.getuid())
            assert again['signalled']==result['signalled'],'Duplicate request changed the operation'
            print('Real lifecycle passed: exact two-process tree, parent exited, child remained, unrelated process survived, replay did not signal again')
        finally:
            if child:
                try:signal_verified_process(child,os.getuid(),signal.SIGKILL)
                except ProcessLookupError:pass
            if parent.poll() is None:parent.terminate()
            parent.wait(timeout=5)
            if unrelated.poll() is None:unrelated.terminate()
            unrelated.wait(timeout=5)

if __name__=='__main__':main()
