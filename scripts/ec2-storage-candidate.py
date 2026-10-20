"""Replay the same network contract through an isolated LocalCloud Query endpoint."""
import importlib.util,json,os,pathlib,subprocess,tempfile
root=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('audit',root/'scripts/rea-runtime-audit.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
image='localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872'
with tempfile.TemporaryDirectory() as d:
    with a.backend(pathlib.Path(d)/'db') as (engine,client,token):
        env=dict(os.environ,EC2_PROBE_ENDPOINT=engine.endpoint,EC2_PROBE_IMAGE='ami-local',EC2_PROBE_TOKEN=token)
        p=subprocess.run(['docker','run','--rm','--network','host','-i','-e','EC2_PROBE_ENDPOINT','-e','EC2_PROBE_IMAGE','-e','EC2_PROBE_TOKEN','--entrypoint','/opt/code/localstack/.venv/bin/python',image,'-'],input=(root/'scripts/ec2-storage-scenario.py').read_text(),env=env,capture_output=True,text=True,timeout=140)
        report={'exit':p.returncode,'records':json.loads(p.stdout) if p.returncode==0 else [],'stderr':p.stderr[-2000:]}
(root/'docs/research/rea/ec2-storage-localcloud.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'exit':report['exit'],'statuses':{r['case']:r['status'] for r in report['records']},'stderr':report['stderr']}))

raise SystemExit(0 if report.get("exit")==0 and not any(r["case"]=="probe-interrupted" for r in report.get("records",[])) else 1)
