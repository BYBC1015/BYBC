"""Execute native navigation/account fences on a JVM, without any web or UI host."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from codex_bridge.subprocess_utils import hidden_subprocess_kwargs


ROOT = Path(__file__).resolve().parents[2]
JAVA_ROOT = ROOT / 'codex-hub/android/app/src/main/java/xyz/baicang/codexhub'


@pytest.fixture(scope='module')
def native_state_harness(tmp_path_factory):
    target = tmp_path_factory.mktemp('native-android-state')
    suffix = '.exe' if os.name == 'nt' else ''
    jdk = ROOT / '.local-tools/jdk-17.0.19+10/bin'
    javac = str(jdk / ('javac' + suffix)) if jdk.exists() else shutil.which('javac')
    java = str(jdk / ('java' + suffix)) if jdk.exists() else shutil.which('java')
    if not javac or not java:
        pytest.skip('JDK unavailable for executable native state checks')
    harness = target / 'NativeStateCheck.java'
    harness.write_text(r'''
package xyz.baicang.codexhub;
import java.util.*;
public final class NativeStateCheck {
    static void yes(boolean value,String label) {if(!value)throw new AssertionError(label);}
    static NativeHubState state() {
        NativeHubState s=new NativeHubState();s.identify("https://example.invalid/hub/","1","session-one");
        s.room("project-a",12);return s;
    }
    public static void main(String[] args) {
        NativeHubState s=state(); NativeHubState.Ticket old=s.ticket();
        switch(args[0]) {
            case "same_identity":
                long epoch=s.authEpoch;long nav=s.navigation;
                s.identify(s.endpoint,s.userId,s.sessionKey);
                yes(s.authEpoch==epoch && s.navigation==nav,"refresh identity must preserve current route");
                yes(s.accepts(old)&&s.displays(old),"current callback remains applicable");break;
            case "account":
                String accountScope=s.userScope();s.identify(s.endpoint,"2",s.sessionKey);
                yes(!s.accepts(old)&&!s.displays(old),"account A callback must not modify B");
                yes(s.project.equals("")&&s.conversation==0,"account switch clears active room");
                yes(!accountScope.equals(s.userScope()),"private user scope differs");break;
            case "session":
                String draft=s.roomScope(),cache=s.cacheScope(),user=s.userScope();
                s.identify(s.endpoint,s.userId,"session-two");
                yes(!s.accepts(old)&&!s.displays(old),"reauth must fence old callbacks");
                yes(!cache.equals(s.cacheScope()),"remote cache must not cross logins");
                yes(user.equals(s.userScope()),"immutable send receipt can be checked after reauth");
                s.room("project-a",12);yes(draft.equals(s.roomScope()),"same owner can recover their unsent draft");break;
            case "endpoint":
                s.identify("https://another.invalid/hub/",s.userId,s.sessionKey);
                yes(!s.accepts(old)&&!s.displays(old),"old server callback must not enter new server");
                yes(!old.userScope().equals(s.userScope()),"endpoint isolates user data");break;
            case "room":
                s.room("project-a",13);yes(s.accepts(old),"completed receipt can still be recorded for original account");
                yes(!s.displays(old),"old conversation response cannot repaint new room");
                yes(!old.roomScope().equals(s.roomScope()),"room draft isolation");
                s.room("project-a",12);yes(!s.displays(old),"returning to old route must not resurrect stale generation");break;
            case "project":
                s.room("project-b",12);yes(!s.displays(old),"same numeric conversation in another project is distinct");
                yes(!old.roomScope().equals(s.roomScope()),"project scopes are distinct");break;
            case "tabs":
                for(NativeHubState.Screen screen:NativeHubState.Screen.values()) {
                    NativeHubState.Ticket previous=s.ticket();long tabNav=s.navigation;
                    boolean changed=s.open(screen);yes(s.screen==screen,"all pages navigate without network");
                    yes(changed?s.navigation>tabNav:s.navigation==tabNav,"repeat selection preserves render generation");
                    yes(changed?!s.displays(previous):s.displays(previous),"only changed route invalidates callback");
                }break;
            case "back_stack":
                s.open(NativeHubState.Screen.TASKS);s.open(NativeHubState.Screen.HISTORY);
                s.detail(NativeHubState.Screen.TASK,41);s.open(NativeHubState.Screen.SETTINGS);s.open(NativeHubState.Screen.MODELS);
                yes(s.back()&&s.screen==NativeHubState.Screen.SETTINGS,"models returns to settings");
                yes(s.back()&&s.screen==NativeHubState.Screen.TASK&&s.detailId==41,"settings returns to exact task");
                yes(s.back()&&s.screen==NativeHubState.Screen.HISTORY,"task returns to actual source");
                yes(s.back()&&s.screen==NativeHubState.Screen.TASKS,"history returns to board");break;
            case "repeat_navigation":
                s.open(NativeHubState.Screen.SETTINGS);NativeHubState.Ticket setting=s.ticket();
                for(int i=0;i<100;i++)yes(!s.open(NativeHubState.Screen.SETTINGS),"repeat click does not rebuild page");
                yes(s.displays(setting),"repeated click does not invalidate current read");
                yes(s.back()&&s.screen==NativeHubState.Screen.CHAT&&s.conversation==12,"one back escapes repeated taps");break;
            case "recreation":
                s.detail(NativeHubState.Screen.TASK,42);s.open(NativeHubState.Screen.SETTINGS);s.open(NativeHubState.Screen.MODELS);
                String saved=s.saveNavigation();NativeHubState recreated=new NativeHubState();recreated.identify(s.endpoint,s.userId,s.sessionKey);recreated.restoreNavigation(saved);
                yes(recreated.screen==NativeHubState.Screen.MODELS&&recreated.project.equals("project-a"),"recreation preserves native route");
                yes(recreated.back()&&recreated.screen==NativeHubState.Screen.SETTINGS,"recreation preserves back stack");
                yes(recreated.back()&&recreated.detailId==42&&recreated.screen==NativeHubState.Screen.TASK,"recreation retains exact task");
                String valid=recreated.saveNavigation();for(String invalid:new String[]{"", "WEBVIEW|x|0|0", "CHAT|x|-1|0", "MODELS|x|0|0\nUNKNOWN"}){recreated.restoreNavigation(invalid);yes(valid.equals(recreated.saveNavigation()),"bad navigation cannot create fallback route");}
                recreated.signOut();yes(!recreated.back(),"logout removes prior account navigation");break;
            case "history_bound":
                for(int i=0;i<200;i++)s.detail(NativeHubState.Screen.TASK,i+1);
                int count=0;while(s.back())count++;
                yes(count==24,"history is bounded without looping");break;
            case "pending_room":
                s.room("project-a",0);s.open(NativeHubState.Screen.SETTINGS);s.open(NativeHubState.Screen.MODELS);
                NativeHubState.Ticket pending=s.ticket();s.resolveConversation("project-a",0,99);
                yes(s.screen==NativeHubState.Screen.MODELS&&s.conversation==99,"late receipt does not change page");
                yes(!s.displays(pending),"receipt fences older zero-room callbacks");
                s.back();s.back();yes(s.screen==NativeHubState.Screen.CHAT&&s.conversation==99,"back reaches confirmed room not abandoned new draft");break;
            case "quiet_progress":
                yes(NativeProgressDisplay.heading(true,true,true).equals("进展待更新"),"quiet heartbeat is not fresh work");
                yes(NativeProgressDisplay.notice(true,true,true).equals("执行电脑仍在线，暂未收到新进度"),"quiet explanation is explicit");
                yes(!NativeProgressDisplay.freshContact(true,true,true),"quiet card has no live progress highlight");
                yes(NativeProgressDisplay.heading(true,true,false).equals("执行连接正常"),"fresh contact does not infer model thinking");
                yes(NativeProgressDisplay.notice(true,true,false).isEmpty(),"real update clears quiet explanation");break;
            case "quiet_truth_table":
                for(boolean online:new boolean[]{false,true})for(boolean contact:new boolean[]{false,true})for(boolean quiet:new boolean[]{false,true}){
                    String heading=NativeProgressDisplay.heading(online,contact,quiet),notice=NativeProgressDisplay.notice(online,contact,quiet);
                    yes(notice.contains("仍在线")== (online&&contact&&quiet),"cached quiet status never claims online after disconnect");
                    yes(!heading.contains("思考")&&!heading.contains("已停止")&&!notice.contains("已停止"),"progress is never a fabricated terminal state or model activity");
                    yes(NativeProgressDisplay.freshContact(online,contact,quiet)==(online&&contact&&!quiet),"highlight requires confirmed contact and new progress state");
                    if(!online)yes(heading.contains("待核对"),"offline takes precedence over cached contact");
                    else if(!contact)yes(heading.equals("执行状态待核对"),"expired task contact takes precedence over quiet");
                }break;
            case "logout":
                s.signOut();yes(!s.accepts(old)&&!s.displays(old),"logout immediately rejects callback");
                yes(s.userId.isEmpty()&&s.sessionKey.isEmpty()&&s.project.isEmpty(),"logout clears identity");
                NativeHubState.Ticket loggedOut=s.ticket();s.signOut();yes(!s.accepts(loggedOut),"repeat logout remains fenced");break;
            case "scope":
                Set<String> scopes=new HashSet<>();
                for(String endpoint:new String[]{"https://a.invalid/hub/","https://b.invalid/hub/"})
                 for(String scopeUser:new String[]{"1","12"})for(String project:new String[]{"3","23"})
                  for(long room:new long[]{0,1,12})yes(scopes.add(NativeHubState.roomScope(endpoint,scopeUser,project,room)),"unambiguous scope");
                yes(s.cacheScope().matches("[a-f0-9]{64}"),"scope is digest not cookies");
                yes(old.roomScope().equals(s.roomScope()),"ticket preserves original immutable scope");break;
            case "keys":
                Set<String> keys=new HashSet<>();
                for(int i=0;i<1000;i++){String key=NativeHubState.requestKey();yes(NativeHubState.validRequestKey(key)&&keys.add(key),"fresh explicit send keys unique");}
                String immutable=NativeHubState.requestKey();s.open(NativeHubState.Screen.TOOLBOX);s.room("project-b",0);
                yes(NativeHubState.validRequestKey(immutable),"navigation must not replace stored job key");
                for(String invalid:new String[]{null,"","short","../../file","1-1-1-1-1","<script>","f0000000-0000-0000-0000-00000000000G"})
                    yes(!NativeHubState.validRequestKey(invalid),"invalid request key rejected");break;
            case "cookie_rotation":
                String rotated=NativeHubCookies.update("codex_hub_session=old-session; codex_hub_csrf=old-csrf",
                    Arrays.asList("codex_hub_session=new-session; Max-Age=3600; HttpOnly; Path=/; SameSite=Lax",
                                  "codex_hub_csrf=new-csrf; Max-Age=3600; HttpOnly; Path=/; SameSite=Lax"));
                yes(rotated.contains("codex_hub_session=new-session")&&rotated.contains("codex_hub_csrf=new-csrf"),"native login adopts both rotated cookies");
                yes(!rotated.contains("old-")&&!rotated.contains("HttpOnly"),"request header contains cookie values only");break;
            case "cookie_delete":
                String deleted=NativeHubCookies.update("codex_hub_session=previous; codex_hub_csrf=previous",
                    Arrays.asList("codex_hub_session=; Max-Age=0; Path=/", "codex_hub_csrf=; Max-Age=0; Path=/"));
                yes(deleted.isEmpty(),"logout deletion removes both native cookies");break;
            case "cookie_scope":
                String scoped=NativeHubCookies.update("unrelated=never-send; codex_hub_session=fixture-session",
                    Arrays.asList("tracking=other-cookie; Path=/", "codex_hub_csrf=fixture-csrf; Max-Age=3600; Path=/"));
                yes(!scoped.contains("unrelated")&&!scoped.contains("tracking"),"unrelated migration/server cookies excluded");
                yes(scoped.contains("fixture-session")&&scoped.contains("fixture-csrf"),"Hub auth retained");break;
            default:throw new AssertionError("unknown case");
        }
        yes(!s.accepts(null)&&!s.displays(null),"missing ticket cannot be applied");
        System.out.println("PASS "+args[0]);
    }
}
''', encoding='utf-8')
    compiled = subprocess.run([javac, '-encoding', 'UTF-8', '-d', str(target),
                               str(JAVA_ROOT / 'NativeHubState.java'), str(JAVA_ROOT / 'NativeHubCookies.java'), str(JAVA_ROOT / 'NativeProgressDisplay.java'), str(harness)],
                              capture_output=True, text=True, timeout=60, **hidden_subprocess_kwargs())
    assert compiled.returncode == 0, compiled.stderr
    return java, target


@pytest.mark.parametrize('case', ['same_identity', 'account', 'session', 'endpoint', 'room',
                                  'project', 'tabs', 'back_stack', 'repeat_navigation', 'recreation', 'history_bound', 'pending_room', 'quiet_progress', 'quiet_truth_table', 'logout', 'scope', 'keys',
                                  'cookie_rotation', 'cookie_delete', 'cookie_scope'])
def test_native_state_behaviour(native_state_harness, case):
    java, target = native_state_harness
    result = subprocess.run([java, '-cp', str(target), 'xyz.baicang.codexhub.NativeStateCheck', case],
                            capture_output=True, text=True, timeout=15, **hidden_subprocess_kwargs())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'PASS ' + case
