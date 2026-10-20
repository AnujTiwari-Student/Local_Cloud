"""Compare bounded API projections; never infer complete response equivalence."""
import json,pathlib
root=pathlib.Path(__file__).resolve().parents[1]/'docs/research/rea'
r={x['case']:x for x in json.loads((root/'ec2-network-reference.json').read_text())['records'] if not x['case'].startswith('cleanup-')}
c={x['case']:x for x in json.loads((root/'ec2-network-localcloud.json').read_text())['records'] if not x['case'].startswith('cleanup-')}

def projection(row):
    result=row.get('result',{})
    if row['case'] in ('eni','eni-default-group','eni-after-assign','eni-after-attach','eni-final'):
        item=result.get('NetworkInterface') or result['NetworkInterfaces'][0]
        view={k:item.get(k) for k in ('Description','Status','SourceDestCheck','InterfaceType')}
        view['Groups']=sorted(g['GroupName'] for g in item.get('Groups',[]))
        if row['case']!='eni-default-group':view['PrivateIpAddresses']=[{k:a.get(k) for k in ('PrivateIpAddress','Primary')} for a in item['PrivateIpAddresses']]
        if item.get('Attachment'):view['Attachment']={k:item['Attachment'].get(k) for k in ('DeviceIndex','Status','DeleteOnTermination')}
        return view
    if row['case']=='eni-attribute':return {'SourceDestCheck':result['SourceDestCheck']}
    if row['case']=='eni-filter':return {'interfaces':sorted(i['PrivateIpAddress'] for i in result['NetworkInterfaces'])}
    if row['case']=='eip-after-associate':
        item=result['Addresses'][0];return {'Domain':item['Domain'],'PrivateIpAddress':item['PrivateIpAddress'],'has_instance':bool(item.get('InstanceId'))}
    if row['case']=='eni-after-eip':
        item=result['NetworkInterfaces'][0];return {'interface_association':bool(item.get('Association')),'private_address_associations':[a['PrivateIpAddress'] for a in item['PrivateIpAddresses'] if a.get('Association')]}
    return None

rows=[]
for case in sorted(r.keys()&c.keys()):
    ref,candidate=projection(r[case]),projection(c[case])
    rows.append({'case':case,'reference_status':r[case]['status'],'localcloud_status':c[case]['status'],'status_equal':r[case]['status']==c[case]['status'],**({'reference_projection':ref,'localcloud_projection':candidate,'projection_equal':ref==candidate} if ref is not None else {})})
report={'scope':'Selected non-cleanup status and explicitly bounded semantic projections, not whole-response equality','cases':len(rows),'matching_statuses':sum(x['status_equal'] for x in rows),'semantic_projections':sum('projection_equal' in x for x in rows),'matching_semantic_projections':sum(x.get('projection_equal',False) for x in rows),'rows':rows,'known_differences':['Reject duplicate private IPs','Reject primary IP removal','Reject deletion of attached ENI','Keep requested secondary-IP EIP target and owning instance rather than reference primary-IP/empty-instance response','Preserve primary IP after rejected removal','LocalCloud publishes only on localhost; reference SSH was wildcard-published']}
(root/'ec2-network-comparison.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='rows'}))
