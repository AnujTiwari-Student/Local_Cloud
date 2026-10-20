"""Bounded networking contract probe. Run only against an isolated test backend."""
import json,os
import boto3
from botocore.config import Config


def scenario():
    client=boto3.client('ec2',endpoint_url=os.environ.get('EC2_PROBE_ENDPOINT','http://127.0.0.1:4566'),region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test',aws_session_token=os.environ.get('EC2_PROBE_TOKEN'),config=Config(retries={'max_attempts':0},read_timeout=30))
    rows=[];cleanup=[]
    def call(label,operation,**params):
        try:
            result=getattr(client,operation)(**params)
            status=result.pop('ResponseMetadata')['HTTPStatusCode']
            rows.append({'case':label,'operation':operation,'parameters':params,'status':status,'result':result})
            return result
        except Exception as e:
            r=getattr(e,'response',{})
            rows.append({'case':label,'operation':operation,'parameters':params,'status':r.get('ResponseMetadata',{}).get('HTTPStatusCode'),'error':r.get('Error',{'type':type(e).__name__,'message':str(e)})})
            return {}
    try:
        vpc=call('vpc','create_vpc',CidrBlock='10.86.0.0/16')['Vpc']['VpcId'];cleanup.append(('delete_vpc',{'VpcId':vpc}))
        subnet=call('subnet','create_subnet',VpcId=vpc,CidrBlock='10.86.1.0/24',AvailabilityZone='us-east-1a')['Subnet']['SubnetId'];cleanup.append(('delete_subnet',{'SubnetId':subnet}))
        subnet2=call('subnet-second','create_subnet',VpcId=vpc,CidrBlock='10.86.2.0/24',AvailabilityZone='us-east-1a')['Subnet']['SubnetId'];cleanup.append(('delete_subnet',{'SubnetId':subnet2}))
        group=call('sg','create_security_group',VpcId=vpc,GroupName='network-probe',Description='Isolated REA probe')['GroupId'];cleanup.append(('delete_security_group',{'GroupId':group}))
        call('subnet-map-public','modify_subnet_attribute',SubnetId=subnet,MapPublicIpOnLaunch={'Value':True})
        call('subnet-after-modify','describe_subnets',SubnetIds=[subnet])
        instance=call('instance','run_instances',ImageId=os.environ.get('EC2_PROBE_IMAGE','ami-03cf127a'),MinCount=1,MaxCount=1,SubnetId=subnet,SecurityGroupIds=[group])['Instances'][0]['InstanceId'];cleanup.append(('terminate_instances',{'InstanceIds':[instance]}))
        eni=call('eni','create_network_interface',SubnetId=subnet2,PrivateIpAddress='10.86.2.10',Groups=[group],Description='secondary')['NetworkInterface']['NetworkInterfaceId'];cleanup.append(('delete_network_interface',{'NetworkInterfaceId':eni}))
        call('eni-default-group','create_network_interface',SubnetId=subnet2)
        # Register the extra interface for cleanup without suppressing the recorded response.
        extra=rows[-1].get('result',{}).get('NetworkInterface',{}).get('NetworkInterfaceId')
        if extra:cleanup.append(('delete_network_interface',{'NetworkInterfaceId':extra}))
        duplicate=call('eni-duplicate-ip','create_network_interface',SubnetId=subnet2,PrivateIpAddress='10.86.2.10').get('NetworkInterface',{}).get('NetworkInterfaceId')
        if duplicate:cleanup.append(('delete_network_interface',{'NetworkInterfaceId':duplicate}))
        call('eni-modify-description','modify_network_interface_attribute',NetworkInterfaceId=eni,Description={'Value':'updated'})
        call('eni-modify-source-check','modify_network_interface_attribute',NetworkInterfaceId=eni,SourceDestCheck={'Value':False})
        call('eni-attribute','describe_network_interface_attribute',NetworkInterfaceId=eni,Attribute='sourceDestCheck')
        call('eni-assign-secondary','assign_private_ip_addresses',NetworkInterfaceId=eni,PrivateIpAddresses=['10.86.2.11','10.86.2.12'])
        call('eni-after-assign','describe_network_interfaces',NetworkInterfaceIds=[eni])
        attachment=call('eni-attach','attach_network_interface',NetworkInterfaceId=eni,InstanceId=instance,DeviceIndex=1).get('AttachmentId')
        if attachment:cleanup.append(('detach_network_interface',{'AttachmentId':attachment}))
        call('eni-after-attach','describe_network_interfaces',NetworkInterfaceIds=[eni])
        call('eni-filter','describe_network_interfaces',Filters=[{'Name':'attachment.instance-id','Values':[instance]}])
        allocation=call('eip','allocate_address',Domain='vpc')['AllocationId'];cleanup.append(('release_address',{'AllocationId':allocation}))
        assoc=call('eip-to-eni-secondary','associate_address',AllocationId=allocation,NetworkInterfaceId=eni,PrivateIpAddress='10.86.2.11').get('AssociationId')
        if assoc:
            call('eip-after-associate','describe_addresses',AllocationIds=[allocation])
            call('eni-after-eip','describe_network_interfaces',NetworkInterfaceIds=[eni])
            call('eip-filter','describe_addresses',Filters=[{'Name':'network-interface-id','Values':[eni]}])
            call('eip-disassociate','disassociate_address',AssociationId=assoc)
        assoc=call('eip-to-instance','associate_address',AllocationId=allocation,InstanceId=instance).get('AssociationId')
        if assoc:
            call('eip-after-instance','describe_addresses',AllocationIds=[allocation])
            call('eip-disassociate-instance','disassociate_address',AssociationId=assoc)
        call('eni-unassign','unassign_private_ip_addresses',NetworkInterfaceId=eni,PrivateIpAddresses=['10.86.2.12'])
        call('eni-unassign-primary','unassign_private_ip_addresses',NetworkInterfaceId=eni,PrivateIpAddresses=['10.86.2.10'])
        call('eni-final','describe_network_interfaces',NetworkInterfaceIds=[eni])
        call('eni-delete-in-use','delete_network_interface',NetworkInterfaceId=eni)
        call('instance-final','describe_instances',InstanceIds=[instance])
        call('missing-interface','describe_network_interfaces',NetworkInterfaceIds=['eni-00000000000000000'])
    except Exception as error:
        rows.append({'case':'probe-interrupted','status':None,'error':{'type':type(error).__name__,'message':str(error)}})
    finally:
        for op,params in reversed(cleanup):call('cleanup-'+op,op,**params)
    return rows

if __name__=='__main__':print(json.dumps(scenario(),default=str))
