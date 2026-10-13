from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json,os,tempfile,unittest
from rubric_generator.cli import build_parser
from rubric_generator.dataset import discover_tasks
from rubric_generator.opencode_client import OpenCodeClient
from rubric_generator.supervisor import supervise,save,read,archive_unvalidated,lock

class AutonomousTests(unittest.TestCase):
    def setup_project(self,root,count=2):
        dataset=root/'dataset'
        for n in range(count):
            p=dataset/f'task{n}'/'问题描述.txt';p.parent.mkdir(parents=True);p.write_text('input',encoding='utf-8')
        args=SimpleNamespace(dataset=dataset,run_id='test',temperature=0,execution_contract=True,concurrency=3,timeout=30,max_attempts=4,max_stalled_attempts=2,retry_delay=0)
        return args,discover_tasks(dataset)
    def test_temperature_override_preserves_other_configuration(self):
        old=json.dumps({'agent':{'rubric-author':{'steps':60}},'theme':'x'})
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'OPENCODE_CONFIG_CONTENT':old}),patch('rubric_generator.opencode_client._resolve_opencode_executable',return_value='opencode'):
            c=OpenCodeClient(Path(tmp),model_by_agent={'rubric-author':'p/m','rubric-reviewer':'p/m'},temperature=0)
            value=json.loads(c.process_env['OPENCODE_CONFIG_CONTENT'])
            self.assertEqual(value['agent']['rubric-author'],{'steps':60,'temperature':0})
            self.assertEqual(value['agent']['rubric-reviewer']['temperature'],0)
            self.assertEqual(os.environ['OPENCODE_CONFIG_CONTENT'],old)
    def test_invalid_temperature(self):
        with tempfile.TemporaryDirectory() as tmp,patch('rubric_generator.opencode_client._resolve_opencode_executable',return_value='opencode'):
            for value in [float('nan'),-1,3]:
                with self.assertRaises(ValueError):OpenCodeClient(Path(tmp),temperature=value)
    def test_native_retry_only_pending_and_restart_reuses_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args,tasks=self.setup_project(root);done=set();calls=[]
            def native(cmd,*unused):
                ids=[cmd[i+1] for i,x in enumerate(cmd) if x=='--task-id'];calls.append(ids)
                done.add(tasks[0].task_id)
                if len(calls)>1:done.add(tasks[1].task_id)
                return 1 if len(calls)==1 else 0
            def success(t,*unused):return t.task_id in done
            for t in tasks:t.rubric_file.write_text('native',encoding='utf-8') if False else None
            def finalize(t,*unused):
                result=success(t)
                if result:t.rubric_file.write_text('native',encoding='utf-8')
                return result
            with patch('rubric_generator.supervisor.completed',side_effect=finalize),patch('rubric_generator.supervisor.worker',side_effect=native):
                self.assertEqual(supervise(root,args),0)
                self.assertEqual(calls[1],[tasks[1].task_id])
                self.assertEqual(supervise(root,args),0)
                self.assertEqual(len(calls),2)
    def test_stalled_retries_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args,_=self.setup_project(root)
            with patch('rubric_generator.supervisor.worker',return_value=1) as w:
                self.assertEqual(supervise(root,args),1);self.assertEqual(w.call_count,2)
            self.assertEqual(read(root/'.rubric-generator/runs/test/supervisor/status.json')['status'],'RETRY_EXHAUSTED')
    def test_input_blocker_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args,tasks=self.setup_project(root)
            def native(*unused):
                save(root/'.rubric-generator/runs/test/tasks'/tasks[0].task_id/'status.json',{'status':'INPUT_BLOCKED','error':'missing material'});return 1
            with patch('rubric_generator.supervisor.worker',side_effect=native) as w:
                self.assertEqual(supervise(root,args),1);self.assertEqual(w.call_count,1)
    def test_semantic_retry_refreshes_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args,tasks=self.setup_project(root,1);calls=[]
            def native(cmd,*unused):
                calls.append(cmd);save(root/'.rubric-generator/runs/test/tasks'/tasks[0].task_id/'status.json',{'status':'SEMANTIC_AUDIT_FAILED'});return 1
            with patch('rubric_generator.supervisor.worker',side_effect=native):supervise(root,args)
            self.assertNotIn('--refresh-reviews',calls[0]);self.assertIn('--refresh-reviews',calls[1])
    def test_changed_temperature_cannot_reuse_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args,_=self.setup_project(root)
            with patch('rubric_generator.supervisor.worker',return_value=1):supervise(root,args)
            args.temperature=0.1
            with patch('rubric_generator.supervisor.worker') as w:
                self.assertEqual(supervise(root,args),1);w.assert_not_called()
    def test_unvalidated_raw_quarantined_valid_phase_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);task=SimpleNamespace(task_id='t');a=run/'tasks/t/artifacts';a.mkdir(parents=True)
            for stem in ['bad','bad.schema-retry-01','good']:(a/f'{stem}.response.txt').write_text('{}')
            (a/'good.validated.json').write_text('{}')
            archive_unvalidated(run,task,2)
            self.assertTrue((a/'good.response.txt').exists());self.assertFalse((a/'bad.response.txt').exists())
            self.assertTrue((a/'recovery-history/attempt-002/bad.schema-retry-01.response.txt').exists())
    def test_existing_rubric_preserved_for_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args,tasks=self.setup_project(root);tasks[0].rubric_file.write_text('old')
            self.assertEqual(supervise(root,args),1);self.assertEqual(tasks[0].rubric_file.read_text(),'old')
    def test_duplicate_supervisor_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'lock'
            with p.open('a+b') as first,p.open('a+b') as second:
                lock(first)
                with self.assertRaises(OSError):lock(second)
    def test_real_child_chinese_output_is_utf8(self):
        import sys
        from rubric_generator.supervisor import worker
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            code=worker([sys.executable,'-u','-c',"print('中文工具续写检查：准备自动恢复')"],root/'output.log',30,root/'worker.json')
            self.assertEqual(code,0)
            text=(root/'output.log').read_text(encoding='utf-8')
            self.assertIn('中文工具续写检查：准备自动恢复',text)
            self.assertNotIn('\ufffd',text)
    def test_endpoint_migration_does_not_retry_same_address(self):
        import subprocess
        from rubric_generator.opencode_client import AgentResponse,OpenCodeError
        with tempfile.TemporaryDirectory() as tmp,patch('rubric_generator.opencode_client._resolve_opencode_executable',return_value='opencode'):
            client=OpenCodeClient(Path(tmp),retries=3)
            with patch('rubric_generator.opencode_client.subprocess.run',return_value=subprocess.CompletedProcess([],0,'{}','')) as call,patch('rubric_generator.opencode_client.parse_event_stream',return_value=AgentResponse('',(),'',terminal_error='API 地址已迁移')):
                with self.assertRaises(OpenCodeError):client.run_agent('rubric-author','prompt','test')
                self.assertEqual(call.call_count,1)
    def test_cli_legacy_defaults_and_exact_task_filter(self):
        p=build_parser();a=p.parse_args(['generate','--dataset','.']);self.assertIsNone(a.temperature);self.assertIsNone(a.run_id)
        b=p.parse_args(['resume','--dataset','.','--run-id','x','--task-id','t']);self.assertEqual(b.task_id,['t'])

if __name__=='__main__':unittest.main()
