"""Local logging compatibility; synthetic APIs never claim Docker homologation."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock
import unittest
from scripts.backup.operational.host_probe import ReadOnlyState
from scripts.backup.capture.storage import CaptureFailure

OP='20261012T172827Z-3bda4bbf-deba-40cf-af87-b4f176789741'
IMAGE='nextcloud:34.0.4-apache@sha256:'+'a'*64

class StateHelperLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);script=root/'read-state.php';script.write_text('<?php echo "{}";')
        self.records={}
        self.store=SimpleNamespace(root=root,write=lambda k,v:self.records.update({k:copy.deepcopy(v)}))
        self.api=MagicMock()
        self.api.create_host_config.side_effect=lambda **kw:kw
        self.api.create_container.return_value={'Id':'b'*64}
        self.api.wait.return_value={'StatusCode':0}
        self.api.inspect_container.return_value={'State':{'Running':False,'ExitCode':0},'Config':{'Labels':{'nextcloud.recovery.helper':OP},'Image':IMAGE}}
        self.api.logs.return_value=b'{}'
        self.helper=ReadOnlyState(self.api,self.store,OP,IMAGE,script)
        self.info={'Mounts':[],'Config':{'Env':['SYNTHETIC_TEST=true']}}

    def test_single_file_logging_disables_incompatible_default_compression(self):
        def start(_):
            log=self.api.create_host_config.call_args.kwargs['log_config']
            if log['Config'].get('compress','true')!='false' and log['Config']['max-file']=='1':
                raise RuntimeError('compression cannot be enabled when max file count is 1')
        self.api.start.side_effect=start
        self.assertEqual(self.helper.read(self.info),{})
        self.assertEqual(self.api.create_host_config.call_args.kwargs['log_config'],{'Type':'local','Config':{'max-size':'64k','max-file':'1','compress':'false'}})
        self.assertEqual(self.records[self.helper.relative]['helpers'][0]['phase'],'removed')
        self.api.remove_container.assert_called_once_with('b'*64,v=False,force=False)

    def test_security_and_isolation_remain_enforced(self):
        self.helper.read(self.info)
        host=self.api.create_host_config.call_args.kwargs
        self.assertTrue(host['read_only'])
        self.assertEqual(host['network_mode'],'none')
        self.assertEqual(host['cap_drop'],['ALL'])
        self.assertEqual(host['security_opt'],['no-new-privileges:true'])
        self.assertTrue(all(v['mode']=='ro' for v in host['binds'].values()))
        create=self.api.create_container.call_args.kwargs
        self.assertTrue(create['network_disabled']);self.assertEqual(create['user'],'www-data')
        self.assertEqual(create['labels'],{'nextcloud.recovery.helper':OP})

    def test_start_failure_keeps_intent_and_never_forces_removal(self):
        self.api.start.side_effect=RuntimeError('synthetic failure')
        with self.assertRaises(CaptureFailure) as failure:self.helper.read(self.info)
        self.assertEqual(failure.exception.code,'STATE_HELPER_RECOVERY_REQUIRED')
        self.assertEqual(self.records[self.helper.relative]['helpers'][0]['phase'],'start_intent')
        self.api.remove_container.assert_not_called()

if __name__=='__main__':unittest.main()
