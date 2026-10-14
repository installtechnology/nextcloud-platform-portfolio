"""Pinned observer recognition only; never a proof of quiescence or backup."""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.operational.host_probe import approved_monitor, ALLOY_IMAGE, ALLOY_COMMAND, ALLOY_CONFIG_HASHES
from scripts.backup.operational.host_probe import RealHostProbe

ROOT=Path(__file__).resolve().parents[4]


class MonitoringPeerTests(unittest.TestCase):
    def setUp(self):
        self.target=SimpleNamespace(project='synthetic-platform',deployment_root='/opt/synthetic-platform')
        self.image={'Id':'sha256:'+'a'*64,'RepoDigests':['grafana/alloy@'+ALLOY_IMAGE.split('@')[1]]}
        self.peer={'Id':'b'*64,'Image':self.image['Id'],
            'Config':{'Image':ALLOY_IMAGE,'Entrypoint':['/bin/alloy'],'Cmd':copy.deepcopy(ALLOY_COMMAND),
              'Labels':{'com.docker.compose.project':self.target.project,'com.docker.compose.service':'alloy','com.docker.compose.oneoff':'False'},
              'Env':['PATH=/bin:/usr/bin','COMPOSE_PROJECT_NAME='+self.target.project,'ALLOY_DEPLOY_MODE=docker']},
            'HostConfig':{'ReadonlyRootfs':True,'Privileged':False,'PortBindings':{},'PidMode':'','IpcMode':'private',
              'NetworkMode':self.target.project+'_backend','CapAdd':None,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true']},
            'NetworkSettings':{'Networks':{self.target.project+'_backend':{},self.target.project+'_observability':{}}},
            'Mounts':[{'Type':'volume','Name':self.target.project+'_alloy_data','Source':'/var/lib/docker/volumes/'+self.target.project+'_alloy_data/_data','Destination':'/var/lib/alloy','RW':True},
              {'Type':'bind','Source':'/var/lib/docker/containers','Destination':'/var/lib/docker/containers','RW':False},
              *[{'Type':'bind','Source':self.target.deployment_root+'/monitoring/alloy/'+name,'Destination':'/etc/alloy/'+name,'RW':False}
                for name in ALLOY_CONFIG_HASHES]]}
        self.config=(ROOT/'monitoring/alloy/config.alloy').read_bytes()
        template=(ROOT/'monitoring/alloy/blackbox.yml.j2').read_text()
        self.blackbox=template.replace("{{ nextcloud_stack_trusted_domains.split(' ')[0] | to_json }}",'"192.0.2.1"').encode()
    def read(self,path):return self.config if path.name=='config.alloy' else self.blackbox
    def validate(self):return approved_monitor(self.peer,self.target,self.image,self.read)
    def reject(self,code):
        with self.assertRaisesRegex(CaptureFailure,'^'+code+'$'):self.validate()
    def test_exact_pinned_observer_accepted(self):self.assertEqual(self.validate(),'b'*64)
    def test_pins_match_published_recipe(self):
        import yaml
        compose=yaml.safe_load((ROOT/'docker/compose.yml').read_text())
        self.assertEqual(compose['services']['alloy']['image'],ALLOY_IMAGE)
        self.assertEqual(compose['services']['alloy']['command'],ALLOY_COMMAND)
    def test_unknown_service_rejected(self):
        self.peer['Config']['Labels']['com.docker.compose.service']='unknown';self.reject('EXTERNAL_DATABASE_CONTAINER_REJECTED')
    def test_wrong_project_rejected(self):
        self.peer['Config']['Labels']['com.docker.compose.project']='other';self.reject('EXTERNAL_DATABASE_CONTAINER_REJECTED')
    def test_oneoff_rejected(self):
        self.peer['Config']['Labels']['com.docker.compose.oneoff']='True';self.reject('EXTERNAL_DATABASE_CONTAINER_REJECTED')
    def test_image_changes_rejected(self):
        for key,value in (('Image','other-image'),):
            self.peer['Config'][key]=value;self.reject('MONITOR_IMAGE_REJECTED')
    def test_image_id_changed_rejected(self):
        self.peer['Image']='sha256:'+'f'*64;self.reject('MONITOR_IMAGE_REJECTED')
    def test_digest_missing_rejected(self):
        self.image['RepoDigests']=[];self.reject('MONITOR_IMAGE_REJECTED')
    def test_command_changed_rejected(self):
        self.peer['Config']['Cmd']=['run','/unreviewed'];self.reject('MONITOR_COMMAND_REJECTED')
    def test_extra_network_rejected(self):
        self.peer['NetworkSettings']['Networks']['extra']={};self.reject('MONITOR_NETWORK_REJECTED')
    def test_each_unsafe_host_option_rejected(self):
        original=copy.deepcopy(self.peer['HostConfig'])
        for key,value in (('Privileged',True),('Privileged',None),('ReadonlyRootfs',False),('PidMode','host'),('PidMode',None),('IpcMode','host'),('NetworkMode',None),
                          ('PortBindings',{'5432/tcp':[{}]}),('CapAdd',['SYS_ADMIN']),('CapDrop',[]),('SecurityOpt',[])):
            with self.subTest(key=key):
                self.peer['HostConfig']=dict(original,**{key:value});self.reject('MONITOR_SECURITY_REJECTED')
    def test_credentials_in_env_rejected(self):
        self.peer['Config']['Env'].append('POSTGRES_PASSWORD=synthetic');self.reject('MONITOR_CREDENTIALS_REJECTED')
    def test_deploy_mode_missing_rejected(self):
        self.peer['Config']['Env'].remove('ALLOY_DEPLOY_MODE=docker');self.reject('MONITOR_CREDENTIALS_REJECTED')
    def test_deploy_mode_changed_rejected(self):
        self.peer['Config']['Env'][-1]='ALLOY_DEPLOY_MODE=other';self.reject('MONITOR_CREDENTIALS_REJECTED')
    def test_deploy_mode_duplicate_rejected(self):
        self.peer['Config']['Env'].append('ALLOY_DEPLOY_MODE=docker');self.reject('MONITOR_CREDENTIALS_REJECTED')
    def test_unknown_environment_rejected(self):
        self.peer['Config']['Env'].append('UNKNOWN=synthetic');self.reject('MONITOR_CREDENTIALS_REJECTED')
    def test_secret_or_socket_mount_rejected(self):
        for dest in ('/run/secrets/postgres_password','/var/run/docker.sock'):
            with self.subTest(dest=dest):
                self.peer['Mounts'].append({'Destination':dest,'RW':False});self.reject('MONITOR_MOUNTS_REJECTED');self.peer['Mounts'].pop()
    def test_writable_config_rejected(self):
        self.peer['Mounts'][2]['RW']=True;self.reject('MONITOR_MOUNTS_REJECTED')
    def test_different_mount_source_rejected(self):
        self.peer['Mounts'][2]['Source']='/other/config.alloy';self.reject('MONITOR_MOUNTS_REJECTED')
    def test_unreviewed_probe_sql_or_post_rejected(self):
        self.blackbox+=b'  dangerous:\n    prober: http\n    http:\n      method: POST\n';self.reject('MONITOR_CONFIG_REJECTED')
    def test_altered_main_config_rejected(self):
        self.config+=b'\nunknown.component "write" {}\n';self.reject('MONITOR_CONFIG_REJECTED')
    def test_invalid_host_header_rejected(self):
        self.blackbox=self.blackbox.replace(b'"192.0.2.1"',b'"bad\\nheader"');self.reject('MONITOR_CONFIG_REJECTED')
    def test_quiescence_still_requires_hba_and_zero_sessions(self):
        from scripts.backup.operational.live_capture import LiveCaptureBackend
        backend=object.__new__(LiveCaptureBackend)
        backend.database_fence=SimpleNamespace(assert_fenced=Mock(side_effect=CaptureFailure('POSTGRES_FENCE_LOST')))
        with self.assertRaisesRegex(CaptureFailure,'^POSTGRES_FENCE_LOST$'):backend.assert_quiescent({})
    def test_hba_rules_reject_monitor_and_all_tcp_clients(self):
        from scripts.backup.operational.database_fence import RULES
        self.assertIn(b'host all all 0.0.0.0/0 reject\n',RULES)
        self.assertIn(b'host all all ::0/0 reject\n',RULES)

    def network_probe(self, peers):
        probe=object.__new__(RealHostProbe);probe.target=self.target
        probe.api=Mock();probe.api.inspect_network.return_value={'Containers':{p:{} for p in peers}}
        probe.api.inspect_container.return_value=self.peer;probe.api.inspect_image.return_value=self.image
        found={'nextcloud':{'Id':'c'*64},'postgres':{'Id':'d'*64,'HostConfig':{'PortBindings':{}},
            'NetworkSettings':{'Networks':{'backend':{'NetworkID':'synthetic'}}}},'redis':{'Id':'e'*64}}
        return probe,found
    def test_no_extra_peer_remains_accepted(self):
        probe,found=self.network_probe(['c'*64,'d'*64,'e'*64]);probe.assert_database_participants(found)
        probe.api.inspect_container.assert_not_called()
    def test_network_integration_rejects_unknown_peer(self):
        probe,found=self.network_probe(['b'*64]);self.peer['Config']['Labels']['com.docker.compose.service']='unknown'
        with self.assertRaisesRegex(CaptureFailure,'^EXTERNAL_DATABASE_CONTAINER_REJECTED$'):probe.assert_database_participants(found)
    def test_network_integration_accepts_only_reviewed_observer(self):
        probe,found=self.network_probe(['b'*64])
        with patch('scripts.backup.operational.host_probe.approved_monitor',side_effect=lambda i,t,d:approved_monitor(i,t,d,self.read)):
            probe.assert_database_participants(found)
    def test_duplicate_observers_rejected(self):
        probe,found=self.network_probe(['b'*64,'f'*64])
        def inspect(peer):
            info=copy.deepcopy(self.peer);info['Id']=peer;return info
        probe.api.inspect_container.side_effect=inspect
        with patch('scripts.backup.operational.host_probe.approved_monitor',side_effect=lambda i,t,d:approved_monitor(i,t,d,self.read)):
            with self.assertRaisesRegex(CaptureFailure,'^DUPLICATE_MONITORING_PEER$'):probe.assert_database_participants(found)
    def test_changed_peer_identity_rejected(self):
        probe,found=self.network_probe(['f'*64])
        with self.assertRaisesRegex(CaptureFailure,'^MONITOR_IDENTITY_CHANGED$'):probe.assert_database_participants(found)
    def test_guard_runs_in_baseline_before_any_maintenance(self):
        probe,found=self.network_probe([]);probe.read_state=lambda:{};probe.containers=lambda:found
        probe.assert_database_participants=Mock(side_effect=CaptureFailure('MONITOR_SECURITY_REJECTED'))
        with self.assertRaisesRegex(CaptureFailure,'^MONITOR_SECURITY_REJECTED$'):probe.baseline()


if __name__=='__main__':unittest.main()
