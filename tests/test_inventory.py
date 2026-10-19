import json
import tempfile
import struct
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import agent_inventory as ai


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.inventory=ai.Inventory(self.home)

    def npm(self,folder,agent,pkg,version='1.0.0'):
        prefix=self.home/folder
        meta=prefix/'node_modules'/pkg/'package.json'
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({'name':pkg,'version':version,'bin':{agent:'cli.js'}}))
        path=prefix/(agent+'.cmd')
        path.write_text(f'node "%~dp0node_modules\\{pkg.replace("/",chr(92))}\\cli.js" %*')
        return self.inventory._entry(agent,path,source='npm',version=version,launch=['node',str(meta.parent/'cli.js')],
                                      manifest=str(meta),prefix=str(prefix),package=pkg)

    def test_path_instances_not_global_package_name_are_identity(self):
        e1=self.npm('one','codex','@openai/codex')
        e2=self.npm('two','codex','@openai/codex','2.0.0')
        self.assertNotEqual(e1['id'],e2['id'])
        self.inventory.instances={e['id']:e for e in (e1,e2)}
        self.assertEqual(len(self.inventory.view()['instances']),2)
        self.assertFalse(any(e['canUpdate'] for e in self.inventory.view()['instances']))

    def test_desktop_version_is_application_package_not_electron_runtime(self):
        package=json.dumps({'name':'desktop-app','version':'1.2.3-rc.2'}).encode()
        header=json.dumps({'files':{'package.json':{'size':len(package),'offset':'0'}}}).encode()
        archive=self.home/'app.asar'
        archive.write_bytes(struct.pack('<IIII',4,len(header)+8,len(header)+4,len(header))+header+package)
        (self.home/'version').write_text('44.0.0')
        self.assertEqual(ai.asar_app_version(archive),'1.2.3-rc.2')

    def test_exit_success_without_expected_version_is_failed(self):
        e=self.inventory._entry('claude',self.home/'claude.exe',source='claude-native',version='1.0.0')
        job={'state':'running','messages':[]}
        with patch.object(ai,'version_of',side_effect=['1.0.0','1.0.0']),patch.object(ai,'run',return_value=(0,'updated','')),patch.object(self.inventory,'detect'):
            self.inventory._update(e,{'latestVersion':'2.0.0'},job)
        self.assertEqual(job['state'],'failed')
        self.assertIn('版本未达到',job['messages'][-1])

    def test_update_verifies_same_entry_and_exact_version(self):
        e=self.inventory._entry('claude',self.home/'claude.exe',source='claude-native',version='1.0.0')
        job={'state':'running','messages':[]}
        with patch.object(ai,'version_of',side_effect=['1.0.0','2.0.0']) as probe,patch.object(ai,'run',return_value=(0,'','')) as run,patch.object(self.inventory,'detect'):
            self.inventory._update(e,{'latestVersion':'2.0.0'},job)
        self.assertEqual(job['state'],'succeeded')
        self.assertEqual(probe.call_args_list[0],probe.call_args_list[1])
        self.assertEqual(run.call_args.args[0],[str(self.home/'claude.exe'),'update'])

    def test_real_isolated_updater_process_and_startup_probe(self):
        version=self.home/'version.txt';version.write_text('1.0.0')
        updater=self.home/'updater.py'
        updater.write_text("import pathlib,sys\np=pathlib.Path(__file__).with_name('version.txt')\nif sys.argv[1]=='update':p.write_text('2.0.0')\nelse:print(p.read_text())\n")
        e=self.inventory._entry('claude',updater,source='claude-native',version='1.0.0',launch=[sys.executable,str(updater)])
        job={'state':'running','messages':[]}
        with patch.object(self.inventory,'detect'):
            self.inventory._update(e,{'latestVersion':'2.0.0'},job)
        self.assertEqual(job['state'],'succeeded')
        self.assertEqual(job['actualVersion'],'2.0.0')

    def test_npm_targets_exact_prefix_and_checks_startup_not_manifest_only(self):
        e=self.npm('isolated','pi','@earendil-works/pi-coding-agent')
        node=self.home/'runtime/node.exe';npm=node.parent/'node_modules/npm/bin/npm-cli.js'
        npm.parent.mkdir(parents=True);npm.write_text('')
        job={'state':'running','messages':[]}
        def installed(*args,**kwargs):
            data=json.loads(Path(e['manifest']).read_text());data['version']='2.0.0';Path(e['manifest']).write_text(json.dumps(data))
            return 0,'',''
        with patch.object(ai.shutil,'which',return_value=str(node)),patch.object(ai,'run',side_effect=installed) as run,patch.object(ai,'version_of',return_value='2.0.0'),patch.object(self.inventory,'detect'):
            self.inventory._update(e,{'latestVersion':'2.0.0'},job)
        args=run.call_args.args[0]
        self.assertIn(e['prefix'],args)
        self.assertIn('@earendil-works/pi-coding-agent@2.0.0',args)
        self.assertEqual(job['state'],'succeeded')

    def test_source_unknown_check_failure_locked_update_and_manual_are_distinct(self):
        e=self.inventory._entry('codex',self.home/'codex',source='unknown')
        self.inventory.instances[e['id']]=e
        self.assertEqual(self.inventory.check(e['id'])['updateState'],'source-unknown')
        with self.assertRaises(ValueError):self.inventory.start_update(e['id'])
        e['source']='msix'
        self.assertEqual(self.inventory.check(e['id'])['updateState'],'manual')
        e['source']='npm';e['package']='@openai/codex'
        with patch.object(ai.urllib.request,'urlopen',side_effect=OSError):
            self.assertEqual(self.inventory.check(e['id'])['updateState'],'check-failed')
        e['source']='claude-native';e['version']='1.0.0'
        job={'state':'running','messages':[]}
        with patch.object(ai,'version_of',return_value='1.0.0'),patch.object(ai,'run',return_value=(1,'','EPERM')),patch.object(self.inventory,'detect'):
            self.inventory._update(e,{'latestVersion':'2.0.0'},job)
        self.assertEqual(job['state'],'failed')
        self.assertIn('占用',job['messages'][-1])


if __name__=='__main__':unittest.main()
