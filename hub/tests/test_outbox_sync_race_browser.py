"""A delayed queue snapshot must not rewind a POST that already started."""
import json
import os
import hashlib
import re
from pathlib import Path

import pytest
from test_mobile_composer_browser import live_hub  # noqa: F401
from test_workspace_shell_browser import login_page
from workspace_fixture import seed_workspace

playwright = pytest.importorskip('playwright.sync_api')
OUTPUT=Path(__file__).resolve().parents[2]/'output/hub-desktop-polish-20261029'


@pytest.mark.parametrize('release_when',['during_post','after_failure'])
def test_delayed_queued_snapshot_cannot_rewind_an_attempted_upload(live_hub,repo,release_when):
    data=seed_workspace(repo,room_messages=1)
    with playwright.sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':390,'height':700})
        page=context.new_page();events=[]
        # Optional local forensic replay of the frozen pre-fix JS. Default
        # regression always exercises the actual current application source.
        baseline_script=os.environ.get('HUB_OUTBOX_RACE_BASELINE_SCRIPT')
        if baseline_script:
            page.route('**/static/workspace-shell.js?*',lambda route:route.fulfill(
                content_type='application/javascript',body=Path(baseline_script).read_text(encoding='utf-8')))
        page.add_init_script('''(() => {
          let store;
          Object.defineProperty(window,'CodexWorkspaceStore',{configurable:true,
            get:()=>store,set:value=>{store={...value,
              read:async(...args)=>{
                const result=await value.read(...args);
                if(window.__holdQueuedSnapshot && result.job?.state==='queued'){
                  window.__holdQueuedSnapshot=false;window.__queuedSnapshotCaptured=true;
                  await new Promise(resolve=>window.__releaseQueuedSnapshot=resolve);
                  window.__queuedSnapshotReleased=true;
                }
                return result;
              },
              list:async(...args)=>{
                const result=await value.list(...args);
                if(window.__queuedSnapshotReleased) window.__queueListAfterRelease=true;
                return result;
              }
            };}});
        })();''')
        try:
            login_page(page,live_hub)
            page.wait_for_function("document.querySelector('[data-ws-shell]').dataset.wsReady==='true'")
            page.locator('[data-ws-attachment-input]').set_input_files([
                {'name':'先读说明.txt','mimeType':'text/plain','buffer':b'first reference'},
                {'name':'再看数据.txt','mimeType':'text/plain','buffer':b'second reference'},
            ])
            key=page.locator('[data-ws-dispatch] [name="request_key"]').input_value()
            count=len(repo.list_tasks(data['project']['id']))
            context.set_offline(True)
            page.locator('[data-ws-send]').click()
            playwright.expect(page.locator('[data-ws-queue-cancel]')).to_be_visible()
            page.evaluate('''() => {
              window.__holdQueuedSnapshot=true;
              const s=document.querySelector('[data-ws-shell]').dataset;
              const channel=new BroadcastChannel('codexHub.workspace.outbox.v1');
              channel.postMessage({scope:`${s.user}.${s.project}.${s.conversation}`});
              channel.close();
            }''')
            page.wait_for_function('window.__queuedSnapshotCaptured===true')

            def observe(request):
                if '/workspace-dispatch' in request.url:
                    event={'method':request.method,'path':request.url.removeprefix(live_hub)}
                    if request.method=='POST':
                        body=request.post_data_buffer
                        match=re.search(br'name="request_key"\r\n\r\n([^\r\n]+)',body or b'')
                        event['request_key']=match.group(1).decode('ascii') if match else None
                        event['body_available']=body is not None
                        event['body_size']=len(body) if body is not None else None
                        event['content_type']=request.headers.get('content-type','').split(';')[0]
                        event['contains_original_key']=key.encode('ascii') in body if body is not None else None
                    events.append(event)
            page.on('request',observe)
            def release_old_snapshot():
                # Drain the released read and the following IDB listing before
                # observing the UI. This also works when the fixed code discards
                # the stale read without calling showOtherQueues at all.
                page.evaluate('''async () => {
                  window.__releaseQueuedSnapshot();
                  await new Promise(resolve=>setTimeout(resolve,0));
                  await CodexWorkspaceStore.list('__fixture_barrier__');
                  await new Promise(resolve=>setTimeout(resolve,0));
                }''')
            def abort_after_stale_sync(route):
                # The real POST has started. Let a queued/attempted=false read
                # finish now, then fail the transport before the server gets it.
                page.evaluate('window.__uploadRacePostStarted=true')
                if release_when=='during_post':release_old_snapshot()
                route.abort()
            page.route('**/workspace-dispatch',abort_after_stale_sync,times=1)
            context.set_offline(False)
            page.wait_for_function('window.__uploadRacePostStarted===true')
            playwright.expect(page.locator('[data-ws-pending]')).to_be_visible()
            # Wait for the durable failure transition, not a transient label.
            page.wait_for_function('''async () => {
              const s=document.querySelector('[data-ws-shell]').dataset;
              const {job}=await CodexWorkspaceStore.read(`${s.user}.${s.project}.${s.conversation}`);
              return Boolean(job?.attempted && job.retryAt && !job.owner);
            }''')
            if release_when=='after_failure':release_old_snapshot()
            state=page.evaluate('''async () => {
              const s=document.querySelector('[data-ws-shell]').dataset;
              const {job}=await CodexWorkspaceStore.read(`${s.user}.${s.project}.${s.conversation}`);
              return {state:job.state,attempted:job.attempted,key:job.key,
                names:job.payload.files.map(item=>item.name),
                pendingText:document.querySelector('[data-ws-pending-message]').textContent,
                cancelVisible:!document.querySelector('[data-ws-queue-cancel]').hidden};
            }''')
            OUTPUT.mkdir(parents=True,exist_ok=True)
            trace_stage='before' if baseline_script else 'after'
            (OUTPUT/f'outbox-queued-race-{trace_stage}-{release_when}.json').write_text(json.dumps({'events':events,'state':state},ensure_ascii=False,indent=2),encoding='utf-8')
            assert state['state']=='uncertain' and state['attempted'] is True
            assert not state['cancelVisible']
            assert state['key']==key and state['names']==['先读说明.txt','再看数据.txt']
            assert any(event['method']=='GET' and event['path'].endswith('/'+key) for event in events)
            assert all(event.get('request_key')==key for event in events if event['method']=='POST')
            assert len(repo.list_tasks(data['project']['id']))==count
            page.reload()
            page.wait_for_url('**/conversations/*')
            tasks=repo.list_tasks(data['project']['id']);assert len(tasks)==count+1
            task=max(tasks,key=lambda item:item['id'])
            delivered=repo.list_task_attachments(data['project']['id'],task['id'])
            assert [item['original_filename'] for item in delivered]==state['names']
            assert [item['sha256'] for item in delivered]==[hashlib.sha256(value).hexdigest() for value in (b'first reference',b'second reference')]
            assert sum(event['method']=='POST' for event in events)==2
            # Chromium does not always expose the body of a multipart replay
            # containing restored Blobs. Verify its authoritative server receipt.
            receipt=page.request.get(f'{live_hub}/projects/workspace-team/workspace-dispatch/{key}')
            assert receipt.status==200 and receipt.json()['found'] is True
            assert receipt.json()['task_ids']==[task['id']]
            dispatched=repo.connection.execute('''SELECT dt.task_id FROM hub_agent_dispatches d
                JOIN hub_dispatch_tasks dt ON dt.dispatch_id=d.id WHERE d.project_id=? AND d.request_key=?''',
                (data['project']['id'],'web:'+key)).fetchall()
            assert [row['task_id'] for row in dispatched]==[task['id']]
            (OUTPUT/f'outbox-queued-race-{trace_stage}-{release_when}.json').write_text(json.dumps({
                'events':events,'state':state,'original_key_receipt_found':True,
                'delivered_task_ids':[task['id']],'delivered_sha256':[item['sha256'] for item in delivered]},ensure_ascii=False,indent=2),encoding='utf-8')
            assert page.locator('[data-ws-input]').input_value()==''
        finally:browser.close()
