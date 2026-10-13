"""Bounded, resumable dataset orchestration around the existing native pipeline."""
from __future__ import annotations
import hashlib,json,math,os,re,shutil,subprocess,sys,time,threading
from datetime import datetime
from pathlib import Path
from .audit import audit_file
from .config import load_project_settings
from .dataset import discover_tasks,is_reference_rubric
from .rubric_spec import audit_rubric_spec,audit_execution_contract,render_rubric

SUCCESS={'COMPLETED','AUTO_FINALIZED'}
SEMANTIC={'SEMANTIC_AUDIT_FAILED','AUDIT_FAILED'}
PERMANENT=('permission requested','user dismissed','cannot reuse','不能复用旧检查点','invalid api key','unauthorized','authentication failed','model not found','endpoint_migrated','地址已迁移')

def save(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,path)

def read(path):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}

def binding(project,tasks,args):
    files={str(p.relative_to(args.dataset.resolve())):hashlib.sha256(p.read_bytes()).hexdigest()
           for t in tasks for p in t.task_dir.rglob('*') if p.is_file() and not is_reference_rubric(p)}
    return {'dataset':str(args.dataset.resolve()),'files':files,'models':load_project_settings(project).agent_models,
            'temperature':args.temperature,'execution_contract':args.execution_contract}

def completed(task,run,contract):
    status=read(run/'tasks'/task.task_id/'status.json')
    if status.get('status') not in SUCCESS or not task.rubric_file.is_file():return False
    spec=read(Path(status.get('rubric_spec','')))
    try:
        return (not audit_rubric_spec(spec) and not audit_execution_contract(spec,required=contract)
                and audit_file(task.rubric_file).passed
                and task.rubric_file.read_text(encoding='utf-8')==render_rubric(spec))
    except (ValueError,TypeError,KeyError):return False

def signature(run):
    return {str(p.relative_to(run)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (run/'tasks').rglob('*.validated.json')}

def archive_unvalidated(run,task,attempt):
    artifacts=run/'tasks'/task.task_id/'artifacts'
    for p in list(artifacts.glob('*.response.txt')):
        stem=p.name[:-len('.response.txt')]
        base=re.split(r'\.(?:schema|format)-retry-',stem)[0]
        if (artifacts/f'{base}.validated.json').is_file():continue
        history=artifacts/'recovery-history'/f'attempt-{attempt:03d}'
        history.mkdir(parents=True,exist_ok=True)
        for candidate in [p,artifacts/f'{stem}.events.jsonl']:
            if candidate.exists():shutil.move(str(candidate),str(history/candidate.name))

def stop_worker(child):
    if child.poll() is not None:return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:child.terminate()
    child.wait(timeout=30)

def worker(command,log,timeout,worker_file):
    with log.open('a',encoding='utf-8') as output:
        output.write('\n启动生成进程 '+datetime.now().isoformat()+'\n');output.flush()
        child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                               env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'})
        save(worker_file,{'pid':child.pid,'command':command,'started_at':datetime.now().isoformat()})
        def relay():
            for line in child.stdout:
                output.write(line);output.flush();print(line,end='',flush=True)
        reader=threading.Thread(target=relay,daemon=True);reader.start()
        last=log.stat().st_mtime;changed=time.monotonic();heartbeat=changed
        try:
            while child.poll() is None:
                time.sleep(5)
                if time.monotonic()-heartbeat>=60:
                    print(f'[{datetime.now().strftime("%H:%M:%S")}] 生成进程仍在运行，正在等待模型处理；请勿重复启动。',flush=True)
                    heartbeat=time.monotonic()
                stamp=log.stat().st_mtime
                if stamp!=last:last=stamp;changed=time.monotonic()
                if time.monotonic()-changed>max(7200,timeout*4):
                    stop_worker(child);return -1
            reader.join(timeout=10)
            return child.returncode
        finally:
            stop_worker(child)
            save(worker_file,{'pid':child.pid,'exited':True,'returncode':child.returncode})

def lock(handle):
    handle.write(b'0');handle.flush();handle.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)

def source_hashes(project):
    return {str(f.relative_to(project)):hashlib.sha256(f.read_bytes()).hexdigest()
            for folder in ['rubric_generator','.opencode/agents'] for f in (project/folder).rglob('*') if f.is_file() and f.suffix in {'.py','.md'}}

def supervise(project,args):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}',args.run_id):raise ValueError('运行编号格式不正确')
    if args.temperature is not None and (not math.isfinite(args.temperature) or not 0<=args.temperature<=2):raise ValueError('温度必须为0到2之间的有限数值')
    if min(args.max_attempts,args.max_stalled_attempts,args.timeout)<1 or args.retry_delay<0:raise ValueError('重试次数、超时或等待参数不正确')
    tasks=discover_tasks(args.dataset)
    if not tasks:raise ValueError('数据集中未找到问题描述.txt')
    run=project/'.rubric-generator/runs'/args.run_id
    journal=run/'supervisor';journal.mkdir(parents=True,exist_ok=True)
    with (journal/'lock').open('a+b') as handle:
        try:lock(handle)
        except OSError:
            print('同一运行已有调度程序，请勿重复启动。',flush=True);return 1
        return _supervise_locked(project,args,tasks,run,journal)

def _supervise_locked(project,args,tasks,run,journal):
    expected=binding(project,tasks,args);saved=read(journal/'binding.json')
    if saved and saved!=expected:
        print('输入、模型、温度或审核规则已变化，请使用新的运行编号。',flush=True);return 1
    if not saved:
        if any(t.rubric_file.exists() for t in tasks):
            print('新运行目录已有Rubric；已保留这些文件，请使用空白题目副本。',flush=True);return 1
        save(journal/'binding.json',expected)
    previous=read(journal/'worker.json')
    if previous and not previous.get('exited'):
        # Never launch a competing writer after an unexpected supervisor crash.
        if os.name=='nt':
            query=f'Get-CimInstance Win32_Process -Filter "ProcessId = {int(previous["pid"])}" | Select-Object -ExpandProperty CommandLine'
            found=subprocess.run(['powershell','-NoProfile','-Command',query],capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW).stdout
            if 'rubric_generator.cli' in found and args.run_id in found:
                print('上次生成进程仍在运行；检查点已保留，请等待该进程退出后恢复。',flush=True);return 1
        elif Path(f'/proc/{previous["pid"]}/cmdline').exists():
            print('上次生成进程仍在运行，检查点已保留。',flush=True);return 1
    state=read(journal/'status.json');used=int(state.get('attempts',0));stalled=int(state.get('stalled_attempts',0))
    old_signature=signature(run)
    while True:
        if binding(project,tasks,args)!=expected:raise ValueError('运行中输入或模型配置发生变化，已停止以免混用结果')
        done=[t for t in tasks if completed(t,run,args.execution_contract)]
        pending=[t for t in tasks if t not in done]
        state.update({'run_id':args.run_id,'dataset':str(args.dataset.resolve()),'completed':len(done),'total':len(tasks),
                      'pending':[str(t.relative_dir) for t in pending],'attempts':used,'stalled_attempts':stalled,'updated_at':datetime.now().isoformat()})
        if not pending:
            state['status']='COMPLETED';save(journal/'status.json',state)
            save(journal/'completion.json',{'completed_at':datetime.now().isoformat(),'rubrics':[{'task':str(t.relative_dir),'path':str(t.rubric_file),'sha256':hashlib.sha256(t.rubric_file.read_bytes()).hexdigest()} for t in done]})
            print(f'数据集生成完成：{len(done)}/{len(tasks)}题。运行记录：{journal}',flush=True);return 0
        blocked=[]
        for t in pending:
            s=read(run/'tasks'/t.task_id/'status.json')
            if s.get('status')=='INPUT_BLOCKED' or any(x in str(s.get('error','')).casefold() for x in PERMANENT):blocked.append(str(t.relative_dir))
        if blocked or used>=args.max_attempts or stalled>=args.max_stalled_attempts:
            state.update({'status':'BLOCKED' if blocked else 'RETRY_EXHAUSTED','blocked':blocked})
            save(journal/'status.json',state);print(f'运行已暂停：已完成{len(done)}/{len(tasks)}题；'+('存在不可自动恢复的输入或配置问题：'+', '.join(blocked) if blocked else '已达到重试或无进展上限。')+f' 详细记录：{journal}',flush=True);return 1
        state['status']='RUNNING';save(journal/'status.json',state)
        print(f'自动运行第{used+1}/{args.max_attempts}轮：已完成{len(done)}/{len(tasks)}题',flush=True)
        groups={False:[],True:[]}
        for t in pending:groups[read(run/'tasks'/t.task_id/'status.json').get('status') in SEMANTIC].append(t)
        for refresh,group in groups.items():
            if not group:continue
            for t in group:archive_unvalidated(run,t,used+1)
            cmd=[sys.executable,'-X','utf8','-u','-m','rubric_generator.cli','--project-dir',str(project),'resume','--dataset',str(args.dataset.resolve()),'--run-id',args.run_id,'--concurrency',str(args.concurrency),'--timeout',str(args.timeout)]
            for t in group:cmd+=['--task-id',t.task_id]
            if args.temperature is not None:cmd+=['--temperature',str(args.temperature)]
            if args.execution_contract:cmd+=['--execution-contract']
            if refresh:cmd+=['--refresh-reviews']
            exit_code=worker(cmd,journal/'native.log',args.timeout,journal/'worker.json')
            save(journal/f'attempt-{used+1:03d}-{int(refresh)}.json',{'tasks':[str(t.relative_dir) for t in group],'exit_code':exit_code,'source_sha256':source_hashes(project)})
        used+=1
        current=signature(run)
        new_done=sum(completed(t,run,args.execution_contract) for t in tasks)
        stalled=stalled+1 if new_done==len(done) and current==old_signature else 0
        old_signature=current
        state.update({'attempts':used,'stalled_attempts':stalled});save(journal/'status.json',state)
        if new_done<len(tasks):
            print(f'准备自动恢复：已完成{new_done}/{len(tasks)}题；有效检查点保留，仅续跑未完成部分。',flush=True)
            time.sleep(min(args.retry_delay*2**max(0,stalled-1),300))
