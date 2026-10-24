"""Bounded EBS copy/attribute and discovery extensions; no raw block devices."""
import copy
import re
from .engine import Error
from .ec2_ebs_defaults import PARAMETERS as DEFAULT_PARAMETERS, Defaults

PARAMETERS = {
 **DEFAULT_PARAMETERS,
 'DescribeVolumeAttribute': {'VolumeId','Attribute'},
 'ModifyVolumeAttribute': {'VolumeId','AutoEnableIO'},
 'DescribeSnapshotAttribute': {'SnapshotId','Attribute'},
 'ModifySnapshotAttribute': {'SnapshotId','Attribute','OperationType','UserIds','GroupNames','CreateVolumePermission'},
 'ResetSnapshotAttribute': {'SnapshotId','Attribute'},
 'CopySnapshot': {'SourceRegion','SourceSnapshotId','Description','Encrypted','TagSpecifications','ClientToken','DestinationRegion','PresignedUrl'},
 'CreateSnapshots': {'InstanceSpecification','Description','TagSpecifications','CopyTagsFromSource'},
 'DescribeRegions': {'RegionNames','AllRegions','Filters'},
 'DescribeAccountAttributes': {'AttributeNames'},
}


class StorageExtension:
 def __init__(self, infrastructure):self.infra=infrastructure
 def perform(self,data,resources,operation,args):
  from .ec2_infrastructure import ACCOUNT,REGION,identifier,now,boolean,tag_specs
  infra=self.infra
  if operation in DEFAULT_PARAMETERS:return Defaults(infra).perform(data,resources,operation,args)
  if operation=='DescribeRegions':
   names=args.get('RegionNames',['us-east-1'])
   if any(n!='us-east-1' for n in names):raise Error('InvalidParameterValue','Only us-east-1 is supported')
   return {'Regions':infra.filter([{'RegionName':REGION,'Endpoint':'ec2.'+REGION+'.amazonaws.com','OptInStatus':'opt-in-not-required'}],args.get('Filters',[]))}
  if operation=='DescribeAccountAttributes':
   default=next((v['VpcId'] for v in resources['vpcs'].values() if v['IsDefault']),'none')
   values={'supported-platforms':['VPC'],'default-vpc':[default],'vpc-max-security-groups-per-interface':['5'],'max-instances':['20'],'max-elastic-ips':['4096'],'vpc-max-elastic-ips':['4096']}
   names=args.get('AttributeNames',list(values))
   if not isinstance(names,list) or any(n not in values for n in names):raise Error('InvalidParameterValue','Unsupported account attribute')
   return {'AccountAttributes':[{'AttributeName':n,'AttributeValues':[{'AttributeValue':v} for v in values[n]]} for n in names]}
  if operation in ('DescribeVolumeAttribute','ModifyVolumeAttribute'):
   volume=infra.get(resources,'Volume',args.get('VolumeId'))
   if operation=='ModifyVolumeAttribute':
    value=args.get('AutoEnableIO')
    if not isinstance(value,dict) or set(value)!={'Value'}:raise Error('InvalidParameterValue','Expected AutoEnableIO.Value')
    volume['_auto_enable_io']=boolean(value['Value']);return {}
   attribute=args.get('Attribute')
   if attribute=='autoEnableIO':return {'VolumeId':volume['VolumeId'],'AutoEnableIO':{'Value':volume.get('_auto_enable_io',False)}}
   if attribute=='productCodes':return {'VolumeId':volume['VolumeId'],'ProductCodes':[]}
   raise Error('InvalidParameterValue','Unsupported volume attribute')
  if operation=='CreateSnapshots':
   specification=args.get('InstanceSpecification')
   if not isinstance(specification,dict) or set(specification)-{'InstanceId','ExcludeBootVolume'} or 'InstanceId' not in specification:raise Error('UnsupportedOperation','Specify InstanceId and optional ExcludeBootVolume',501)
   instance=infra.instance(data,specification['InstanceId'])
   if instance['State']['Name']=='terminated':raise Error('IncorrectInstanceState','Instance is terminated')
   exclude=boolean(specification.get('ExcludeBootVolume',False))
   if args.get('CopyTagsFromSource') not in (None,'volume'):raise Error('InvalidParameterValue','CopyTagsFromSource must be volume')
   copy_tags=args.get('CopyTagsFromSource')=='volume'
   tags=tag_specs(args.get('TagSpecifications'),'snapshot')
   made=[]
   try:
    with infra.ec2.runtime.freeze_snapshot_writers([instance]):
     for mapping in instance.get('BlockDeviceMappings',[]):
      if exclude and mapping['DeviceName'] in ('/dev/sda1','/dev/xvda','/dev/xvda1'):continue
      volume=infra.get(resources,'Volume',mapping['Ebs']['VolumeId'])
      request={'VolumeId':volume['VolumeId'],'Description':args.get('Description','')}
      merged={t['Key']:t['Value'] for t in volume.get('Tags',[]) if copy_tags}
      merged.update({t['Key']:t['Value'] for t in tags})
      request['TagSpecifications']=[{'ResourceType':'snapshot','Tags':[{'Key':k,'Value':v} for k,v in merged.items()]}]
      result=infra.storage(data,resources,'CreateSnapshot',request);made.append(result)
    return {'Snapshots':made}
   except Exception as failure:
    for snapshot in made:
     try:infra.ec2.runtime.delete_volume(snapshot['SnapshotId'])
     except Error as cleanup_error:failure.add_note('Batch snapshot cleanup failed: '+str(cleanup_error))
     finally:resources['snapshots'].pop(snapshot['SnapshotId'],None)
    raise
  snapshot=infra.get(resources,'Snapshot',args.get('SourceSnapshotId') if operation=='CopySnapshot' else args.get('SnapshotId'))
  if operation=='CopySnapshot':
   # Botocore injects signing metadata. Never fetch, persist or include the
   # time-dependent presigned URL in an idempotency fingerprint.
   if args.get('DestinationRegion',REGION)!=REGION:raise Error('UnsupportedOperation','Cross-region copy is not implemented',501)
   args={k:v for k,v in args.items() if k not in ('PresignedUrl','DestinationRegion')}
   if args.get('SourceRegion')!=REGION:raise Error('UnsupportedOperation','Cross-region copy is not implemented',501)
   replay=infra.replay(resources['snapshots'],args)
   if replay:return {'SnapshotId':replay['SnapshotId']}
   encrypted=boolean(args.get('Encrypted',snapshot['Encrypted']))
   if snapshot['Encrypted'] and not encrypted:raise Error('InvalidParameterValue','Cannot decrypt an encrypted snapshot')
   if infra.ec2.images and encrypted:raise Error('UnsupportedOperation','Docker file copies are not encrypted block snapshots',501)
   item={'SnapshotId':identifier('snap'),'VolumeId':snapshot['VolumeId'],'VolumeSize':snapshot['VolumeSize'],'State':'completed','StartTime':now(),'Progress':'100%','OwnerId':ACCOUNT,'Description':args.get('Description',''),'Encrypted':encrypted,'Tags':tag_specs(args.get('TagSpecifications'),'snapshot')}
   infra.ec2.runtime.create_volume(item['SnapshotId'],snapshot['SnapshotId']);item.update(infra.token_fields(args));resources['snapshots'][item['SnapshotId']]=item
   return {'SnapshotId':item['SnapshotId']}
  attribute=args.get('Attribute','createVolumePermission')
  if attribute not in ('createVolumePermission','productCodes'):raise Error('InvalidParameterValue','Unsupported snapshot attribute')
  if operation=='DescribeSnapshotAttribute':return {'SnapshotId':snapshot['SnapshotId'],**({'CreateVolumePermissions':copy.deepcopy(snapshot.get('_permissions',[]))} if attribute=='createVolumePermission' else {'ProductCodes':[]})}
  if attribute!='createVolumePermission':raise Error('UnsupportedOperation','Product code changes are not supported',501)
  if operation=='ResetSnapshotAttribute':snapshot['_permissions']=[];return {}
  structured=args.get('CreateVolumePermission')
  if structured is not None:
   if set(args)&{'OperationType','UserIds','GroupNames'} or not isinstance(structured,dict) or set(structured)-{'Add','Remove'} or not structured:raise Error('InvalidParameterCombination','Use one permissions syntax')
   changes=structured
  else:
   action={'add':'Add','remove':'Remove'}.get(args.get('OperationType'))
   if not action:raise Error('InvalidParameterValue','Specify add/remove OperationType')
   changes={action:[{'UserId':u} for u in args.get('UserIds',[])]+[{'Group':g} for g in args.get('GroupNames',[])]}
  current=snapshot.setdefault('_permissions',[])
  for action,items in changes.items():
   if not isinstance(items,list) or not 1<=len(items)<=100:raise Error('InvalidParameterValue','Expected 1 through 100 permissions')
   for value in items:
    if not isinstance(value,dict) or not (set(value)=={'UserId'} and isinstance(value['UserId'],str) and re.fullmatch(r'[0-9]{12}',value['UserId']) or value=={'Group':'all'}):raise Error('InvalidParameterValue','Expected account UserId or all Group')
    if action=='Add' and value not in current:current.append(copy.deepcopy(value))
    if action=='Remove' and value in current:current.remove(value)
  if len(current)>100:raise Error('ResourceLimitExceeded','Local snapshot permission limit is 100')
  return {}
