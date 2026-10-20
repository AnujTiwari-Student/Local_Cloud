"""Start one normal-license reference, run networking probe and remove owned resources."""
import json,os,pathlib,subprocess,time,urllib.request,uuid
ROOT=pathlib.Path(__file__).resolve().parents[1]
# Pinned reference shared with the earlier captures.
IMAGE='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
name='localcloud-rea-storage-'+uuid.uuid4().hex[:10]
env=dict(os.environ,LOCALSTACK_AUTH_TOKEN=(pathlib.Path.home()/'.config/localstack/rea-auth-token').read_text().strip())
report={};created=False
try:
    subprocess.run(['docker','run','-d','--name',name,'--label','localcloud.audit='+name,'-p','127.0.0.1::4566','-e','LOCALSTACK_AUTH_TOKEN','-e','EC2_VM_MANAGER=mock',IMAGE],env=env,check=True,capture_output=True);created=True
    info=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    port=info['NetworkSettings']['Ports']['4566/tcp'][0]['HostPort']
    for _ in range(100):
        try:
            with urllib.request.urlopen('http://127.0.0.1:'+port+'/_localstack/health',timeout=1) as r:
                if r.status==200:break
        except OSError:time.sleep(.5)
    else:raise RuntimeError('Reference health deadline exceeded')
    p=subprocess.run(['docker','exec','-i',name,'/opt/code/localstack/.venv/bin/python','-'],input=(ROOT/'scripts/ec2-storage-scenario.py').read_text(),capture_output=True,text=True,timeout=160)
    report={'exit':p.returncode,'reference_image':IMAGE,'records':json.loads(p.stdout) if p.returncode==0 else [],'stderr':p.stderr[-2000:]}
finally:
    if created:
        info=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
        assert info['Config']['Labels']['localcloud.audit']==name
        subprocess.run(['docker','rm','-f','-v',name],check=True,capture_output=True)
        report['cleanup_completed']=True
(ROOT/'docs/research/rea/ec2-storage-reference.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'exit':report.get('exit'),'statuses':{r['case']:r['status'] for r in report.get('records',[])},'stderr':report.get('stderr'),'cleanup_completed':report.get('cleanup_completed')}))

raise SystemExit(0 if report.get("exit")==0 and not any(r["case"]=="probe-interrupted" for r in report.get("records",[])) else 1)
