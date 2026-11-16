package xyz.baicang.codexhub;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.Dialog;
import android.content.ClipData;
import android.content.ContentValues;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.content.res.ColorStateList;
import android.animation.ValueAnimator;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.Switch;
import android.widget.Spinner;
import android.widget.ArrayAdapter;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Installed Android screens and native navigation. Network responses are data, never pages. */
public final class NativeHubActivity extends Activity {
    private static final int BG=0xff15191d, PANEL=0xff20272c, CARD=0xff293239, LINE=0xff3b484f;
    private static final int TEXT=0xffe8eceb, MUTED=0xff9eafb7, GOLD=0xffd3c2a7, GREEN=0xff9fc6b5;
    private static final int PICK_FILES=201, SAVE_FILE=202;
    private final Handler main=new Handler(Looper.getMainLooper());
    private final ExecutorService network=Executors.newFixedThreadPool(4), files=Executors.newSingleThreadExecutor();
    private final NativeHubState state=new NativeHubState();
    private final Set<String> reads=new HashSet<>(), sending=new HashSet<>(), checking=new HashSet<>();
    private final Set<String> mutations=new HashSet<>();
    private NativeHubStore store;
    private SharedAttachmentImporter importer;
    private LinearLayout root, header, navigation, content, feed, fileTray,chatContext;
    private FrameLayout page;
    private ScrollView messagesScroll;
    private ScrollView pageScroll;
    private final java.util.Map<String,Integer> scrollPositions=new java.util.HashMap<>();
    private String renderedRoute="";
    private boolean reducedMotion=false;
    private TextView connectionLabel, recipientLabel, titleLabel;
    private EditText composer;
    private JSONArray selectedFiles=new JSONArray();
    private long selectedAgent=0;
    private int receiptCursor=0;
    private boolean destroyed=false,foreground=false,restoring=false,keyboardOpen=false,online=false,loginBusy=false;
    private String cookie="",csrf="",username="",connectionText="正在核对连接…",renderedRevision="",fixtureBase="",composerScope="";
    private NativeHubState.Ticket pickerTicket;
    private HubFileDownload.Result pendingSave;
    private AlertDialog modelDialog;
    private final ArrayList<Uri> incomingShares=new ArrayList<>();
    private final Runnable poll=new Runnable(){public void run(){
        if(!foreground||destroyed)return;
        if(!state.userId.isEmpty()) {refreshCurrent(false);checkReceipts();}
        main.postDelayed(this,8000);
    }};

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        store=new NativeHubStore(this);importer=new SharedAttachmentImporter(this);
        reducedMotion=store.read("appearance").optBoolean("reduced_motion",false);
        String testBase=getIntent().getStringExtra("native_fixture_base");
        if(BuildConfig.DEBUG&&NativeHubApi.fixtureBase(testBase))fixtureBase=testBase;
        state.endpoint=fixtureBase.isEmpty()?HubEndpoint.current(this):fixtureBase;
        JSONObject auth=store.read("identity:"+NativeHubState.digest(state.endpoint));
        cookie=auth.optString("cookies","");
        if(cookie.isEmpty()&&fixtureBase.isEmpty()&&!auth.optBoolean("migration_disabled")) {
            try {cookie=NativeHubCookies.update(CookieManager.getInstance().getCookie(state.endpoint),java.util.Collections.emptyList());}
            catch(RuntimeException ignored){cookie="";}
        }
        if(!cookie.isEmpty()&&auth.optString("cookie_digest").equals(NativeHubState.digest(cookie))&&auth.optLong("user_id")>0) {
            state.identify(state.endpoint,auth.optString("user_id"),auth.optString("session_key"));
            username=auth.optString("username");csrf=auth.optString("csrf_token");restoreLocation();
        }
        createFrame();
        stopService(new Intent(this,HubNotificationService.class));
        if(state.userId.isEmpty())renderLogin();else {renderShell();warmCache();}
        collectIncoming(getIntent());refreshSession();
    }

    @Override protected void onResume(){super.onResume();foreground=true;HubNotificationService.setAppInForeground(true);main.removeCallbacks(poll);main.postDelayed(poll,1200);}
    @Override protected void onPause(){foreground=false;HubNotificationService.setAppInForeground(false);main.removeCallbacks(poll);saveComposer();rememberPage();if(!state.userId.isEmpty())store.rememberLocation(state);super.onPause();}
    @Override protected void onDestroy(){destroyed=true;if(modelDialog!=null)modelDialog.dismiss();main.removeCallbacksAndMessages(null);network.shutdownNow();files.shutdownNow();store.close();super.onDestroy();}
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);collectIncoming(intent);}

    private void createFrame() {
        root=column();root.setBackgroundColor(BG);setContentView(root);
        getWindow().setStatusBarColor(BG);getWindow().setNavigationBarColor(BG);
        getWindow().getDecorView().setSystemUiVisibility(0);
        if(Build.VERSION.SDK_INT>=30)getWindow().setDecorFitsSystemWindows(false);
        root.setOnApplyWindowInsetsListener((view,insets)->{
            if(Build.VERSION.SDK_INT>=30) {
                android.graphics.Insets bars=insets.getInsets(WindowInsets.Type.systemBars()|WindowInsets.Type.displayCutout());
                android.graphics.Insets ime=insets.getInsets(WindowInsets.Type.ime());keyboardOpen=ime.bottom>bars.bottom;
                root.setPadding(bars.left,bars.top,bars.right,Math.max(bars.bottom,ime.bottom));
                if(navigation!=null)navigation.setVisibility(keyboardOpen?View.GONE:View.VISIBLE);
                boolean compact=keyboardOpen&&getResources().getConfiguration().orientation==android.content.res.Configuration.ORIENTATION_LANDSCAPE;
                if(header!=null)header.setVisibility(compact?View.GONE:View.VISIBLE);
                if(chatContext!=null)chatContext.setVisibility(compact?View.GONE:View.VISIBLE);
                if(composer!=null){composer.setMinLines(compact?1:2);composer.setMaxLines(compact?2:4);}
                return new WindowInsets.Builder(insets).setInsets(WindowInsets.Type.systemBars()|WindowInsets.Type.displayCutout()|WindowInsets.Type.ime(),android.graphics.Insets.NONE).build();
            }
            root.setPadding(insets.getSystemWindowInsetLeft(),insets.getSystemWindowInsetTop(),insets.getSystemWindowInsetRight(),insets.getSystemWindowInsetBottom());
            return insets.consumeSystemWindowInsets();
        });
    }
    private void restoreLocation(){JSONObject saved=store.read("location:"+state.userScope());state.project=saved.optString("project");state.conversation=saved.optLong("conversation");state.restoreNavigation(saved.optString("navigation"));}
    private JSONObject cached(String key){JSONObject value=store.cache(state.cacheScope(),key);return value==null?new JSONObject():value;}
    private String workspaceKey(){return "workspace:"+state.project+":"+state.conversation;}
    private JSONObject workspace(){return cached(workspaceKey());}
    private String projectPath(){return "api/mobile/projects/"+Uri.encode(state.project);}
    private String cookies(){return cookie;}

    private void rememberIdentity() {
        store.put("identity:"+NativeHubState.digest(state.endpoint),obj("user_id",Long.parseLong(state.userId),"username",username,
                "session_key",state.sessionKey,"csrf_token",csrf,"cookies",cookie,"cookie_digest",NativeHubState.digest(cookie)));
    }
    private void adoptSession(NativeHubApi.Reply reply) throws Exception {
        JSONObject user=reply.body.getJSONObject("user");String key=reply.body.getString("session_key");
        if(reply.body.optInt("api_version")!=1||user.optLong("id")<=0||key.isEmpty())throw new Exception("客户端接口版本暂不兼容");
        cookie=NativeHubCookies.update(cookie,reply.cookies);
        state.identify(state.endpoint,user.optString("id"),key);username=user.optString("username");csrf=reply.body.getString("csrf_token");
        rememberIdentity();if(state.project.isEmpty()&&state.screen==NativeHubState.Screen.CHAT)restoreLocation();online=true;connectionText="已连接 · 本地界面";
        renderShell();warmCache();refreshProjects(true);refreshCurrent(true);
        String pendingKey="pending-link:"+NativeHubState.digest(state.endpoint),link=store.read(pendingKey).optString("url");if(!link.isEmpty()){store.remove(pendingKey);routeIntent(link);}
        if(fixtureBase.isEmpty())try{Intent service=new Intent(this,HubNotificationService.class);if(Build.VERSION.SDK_INT>=26)startForegroundService(service);else startService(service);}catch(RuntimeException ignored){}
    }
    private void refreshSession() {
        NativeHubState.Ticket ticket=state.ticket();final String currentCookie=cookies();
        network.execute(()->{try {
            NativeHubApi.Reply reply=NativeHubApi.json(ticket.endpoint,"api/mobile/session","GET",null,currentCookie,"");
            main.post(()->{if(destroyed||!state.accepts(ticket))return;try{adoptSession(reply);}catch(Exception invalid){status(invalid.getMessage(),false);}});
        }catch(Exception error){main.post(()->{if(destroyed||!state.accepts(ticket))return;if(error instanceof NativeHubApi.ApiError&&((NativeHubApi.ApiError)error).status==401)expireLogin();else status(error.getMessage(),false);});}});
    }
    private void expireLogin(){if(modelDialog!=null)modelDialog.dismiss();if(state.userId.isEmpty()){status("登录后连接你的团队",false);return;}saveComposer();store.put("identity:"+NativeHubState.digest(state.endpoint),obj("migration_disabled",true));state.signOut();csrf="";cookie="";online=false;loginBusy=false;connectionText="登录已过期，草稿和附件仍保留";stopService(new Intent(this,HubNotificationService.class));renderLogin();}
    private void login(EditText name,EditText password,Button submit) {
        if(loginBusy)return;String user=name.getText().toString().trim(),pass=password.getText().toString();
        if(user.isEmpty()||pass.isEmpty()){toast("填写账号和密码即可连接");return;}
        loginBusy=true;submit.setEnabled(false);state.authEpoch++;NativeHubState.Ticket ticket=state.ticket();
        network.execute(()->{try {
            NativeHubApi.Reply reply=NativeHubApi.json(ticket.endpoint,"api/mobile/login","POST",obj("username",user,"password",pass),"","");
            main.post(()->{if(destroyed||!state.accepts(ticket))return;loginBusy=false;password.setText("");try{adoptSession(reply);}catch(Exception error){submit.setEnabled(true);status(error.getMessage(),false);}});
        }catch(Exception error){main.post(()->{if(destroyed||!state.accepts(ticket))return;loginBusy=false;submit.setEnabled(true);status(error.getMessage(),false);});}});
    }
    private void logout() {
        if(modelDialog!=null)modelDialog.dismiss();saveComposer();NativeHubState.Ticket ticket=state.ticket();String oldCookie=cookie,oldCsrf=csrf;
        state.signOut();cookie="";csrf="";username="";online=false;store.remove("identity:"+NativeHubState.digest(state.endpoint));
        stopService(new Intent(this,HubNotificationService.class));((android.app.NotificationManager)getSystemService(NOTIFICATION_SERVICE)).cancelAll();
        // Prevent a later launch from reimporting the old WebView session after native logout.
        store.put("identity:"+NativeHubState.digest(state.endpoint),obj("cookies","","migration_disabled",true));
        renderLogin();network.execute(()->{try{NativeHubApi.json(ticket.endpoint,"api/mobile/logout","POST",obj(),oldCookie,oldCsrf);}catch(Exception ignored){ }});
    }

    private void renderLogin() {
        composer=null;root.removeAllViews();header=null;navigation=null;page=null;
        ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);LinearLayout body=column();body.setGravity(Gravity.CENTER_VERTICAL);body.setPadding(dp(28),dp(28),dp(28),dp(28));scroll.addView(body);
        ImageView owl=new ImageView(this);owl.setImageResource(xyz.baicang.codexhub.R.drawable.codex_owl_logo);body.addView(owl,new LinearLayout.LayoutParams(dp(60),dp(60)));
        body.addView(text("你的团队，随身同行",26,TEXT,true));body.addView(text("BYBC Hub · 原生工作台",14,MUTED,false));space(body,28);
        EditText name=input("账号",false),password=input("密码",false);password.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD);
        body.addView(name);space(body,12);body.addView(password);space(body,16);
        Button submit=button("登录",()->{});submit.setOnClickListener(v->login(name,password,submit));submit.setBackground(tone(GOLD,18));submit.setTextColor(BG);body.addView(submit);
        connectionLabel=text(connectionText,13,MUTED,false);body.addView(connectionLabel);space(body,16);
        body.addView(button("连接地址",this::serverSettings));root.addView(scroll,new LinearLayout.LayoutParams(-1,-1));
    }

    private void renderShell() {
        if(state.userId.isEmpty()){renderLogin();return;}
        saveComposer();composer=null;feed=null;messagesScroll=null;root.removeAllViews();
        header=column();header.setPadding(dp(18),dp(9),dp(12),dp(9));
        LinearLayout heading=row();ImageView owl=new ImageView(this);owl.setImageResource(xyz.baicang.codexhub.R.drawable.codex_owl_logo);heading.addView(owl,new LinearLayout.LayoutParams(dp(34),dp(34)));
        LinearLayout names=column();titleLabel=text("BYBC Hub",20,TEXT,true);names.addView(titleLabel);connectionLabel=text(connectionText,11,MUTED,false);names.addView(connectionLabel);LinearLayout.LayoutParams nameLayout=new LinearLayout.LayoutParams(0,-2,1);nameLayout.leftMargin=dp(10);heading.addView(names,nameLayout);
        heading.addView(iconButton("↻","刷新当前内容",()->refreshCurrent(true)));heading.addView(iconButton("⚙","设置",()->navigate(NativeHubState.Screen.SETTINGS)));header.addView(heading);root.addView(header);
        page=new FrameLayout(this);root.addView(page,new LinearLayout.LayoutParams(-1,0,1));
        navigation=row();navigation.setPadding(dp(7),dp(5),dp(7),dp(7));navigation.setBackgroundColor(PANEL);
        String[] labels={"对话","项目","智能体","任务","工具箱"};NativeHubState.Screen[] screens={NativeHubState.Screen.CHAT,NativeHubState.Screen.PROJECTS,NativeHubState.Screen.AGENTS,NativeHubState.Screen.TASKS,NativeHubState.Screen.TOOLBOX};
        for(int i=0;i<labels.length;i++){final NativeHubState.Screen screen=screens[i];Button item=button(labels[i],()->navigate(screen));item.setTextSize(12);item.setPadding(0,0,0,0);item.setTextColor(state.screen==screen?GOLD:MUTED);item.setBackground(tone(state.screen==screen?CARD:PANEL,16));navigation.addView(item,new LinearLayout.LayoutParams(0,dp(52),1));}
        root.addView(navigation);navigation.setVisibility(keyboardOpen?View.GONE:View.VISIBLE);renderPage();
    }
    private void navigate(NativeHubState.Screen screen){saveComposer();rememberPage();if(!state.open(screen))return;store.rememberLocation(state);renderShell();animatePage();refreshCurrent(true);}
    private void openRoom(String slug,long room){saveComposer();rememberPage();String before=state.routeKey();state.room(slug,room);store.rememberLocation(state);if(before.equals(state.routeKey()))return;renderShell();animatePage();refreshCurrent(true);}
    private void rememberPage(){if(pageScroll!=null&&!renderedRoute.isEmpty())scrollPositions.put(renderedRoute,pageScroll.getScrollY());}
    private void animatePage(){if(page==null)return;page.animate().cancel();page.setAlpha(1f);page.setTranslationY(0f);if(reducedMotion||(Build.VERSION.SDK_INT>=26&&!ValueAnimator.areAnimatorsEnabled()))return;page.setAlpha(.65f);page.setTranslationY(dp(7));page.animate().alpha(1f).translationY(0f).setDuration(160).start();}
    /** No selection means latest; explicit conversation 0 remains a real new draft. */
    private void selectProject(String slug){
        JSONObject location=store.read("room:"+state.userScope()+":"+slug);
        if(location.has("conversation")){openRoom(slug,location.optLong("conversation"));return;}
        String scope=NativeHubState.roomScope(state.endpoint,state.userId,slug,0);JSONObject draft=store.draft(scope);
        if(!draft.optString("text").isEmpty()||array(draft,"files").length()>0||pendingNewRoom(slug)){openRoom(slug,0);return;}
        saveComposer();state.room(slug,0);renderShell();
        fetch("initial:"+slug,"api/mobile/projects/"+Uri.encode(slug)+"/workspace",data->{
            saveComposer();JSONObject current=store.draft(state.roomScope());
            if(!current.optString("text").isEmpty()||array(current,"files").length()>0||pendingNewRoom(slug)){
                store.rememberLocation(state);refreshCurrent(true);return;
            }
            JSONObject conversation=data.optJSONObject("conversation");long latest=conversation==null?0:conversation.optLong("id");
            state.room(slug,latest);store.rememberLocation(state);store.cache(state.cacheScope(),workspaceKey(),data);composer=null;renderShell();
        });
    }
    private boolean pendingNewRoom(String slug){JSONArray jobs=store.jobs(state.userScope());for(int i=0;i<jobs.length();i++){JSONObject job=jobs.optJSONObject(i);if(job!=null&&job.optString("project").equals(slug)&&job.optLong("conversation_id")==0)return true;}return false;}
    private void renderPage() {
        rememberPage();pageScroll=null;renderedRoute=state.routeKey();page.removeAllViews();content=column();content.setPadding(dp(16),dp(10),dp(16),dp(20));
        if(state.screen==NativeHubState.Screen.CHAT){renderChat();return;}
        ScrollView scroll=new ScrollView(this);pageScroll=scroll;scroll.setFillViewport(true);scroll.addView(content);page.addView(scroll,new FrameLayout.LayoutParams(-1,-1));
        switch(state.screen){
            case PROJECTS:renderProjects();break;case AGENTS:renderAgents();break;case TASKS:renderTasks();break;
            case TOOLBOX:renderToolbox();break;case AGENT:renderAgent();break;case TASK:renderTask();break;case HISTORY:renderHistory();break;
            case DEVICES:renderDevices();break;case LIBRARY:renderLibrary();break;case DOWNLOADS:renderDownloads();break;
            case SETTINGS:renderSettings();break;case MODELS:renderModels();break;default:break;
        }
        int savedY=scrollPositions.containsKey(renderedRoute)?scrollPositions.get(renderedRoute):0;scroll.post(()->{if(scroll==pageScroll)scroll.scrollTo(0,savedY);});
    }

    private void renderChat() {
        LinearLayout layout=column();page.addView(layout,new FrameLayout.LayoutParams(-1,-1));
        LinearLayout context=row();chatContext=context;context.setPadding(dp(16),0,dp(12),dp(8));
        String projectName=workspace().optJSONObject("project")==null?state.project:workspace().optJSONObject("project").optString("name",state.project);
        Button room=button(state.project.isEmpty()?"选择一个项目":projectName+"  ▾",this::roomPicker);
        room.setGravity(Gravity.LEFT|Gravity.CENTER_VERTICAL);context.addView(room,new LinearLayout.LayoutParams(0,dp(46),1));
        context.addView(iconButton("＋","新建对话",()->{if(!state.project.isEmpty())openRoom(state.project,0);else navigate(NativeHubState.Screen.PROJECTS);}));layout.addView(context);
        messagesScroll=new ScrollView(this);messagesScroll.setFillViewport(true);feed=column();feed.setPadding(dp(17),dp(10),dp(17),dp(14));messagesScroll.addView(feed);layout.addView(messagesScroll,new LinearLayout.LayoutParams(-1,0,1));
        LinearLayout compose=column();compose.setPadding(dp(12),dp(8),dp(12),dp(10));compose.setBackgroundColor(BG);
        LinearLayout box=column();box.setPadding(dp(13),dp(5),dp(13),dp(8));box.setBackground(outline(CARD,22));
        composerScope=state.roomScope();JSONObject draft=store.draft(composerScope);selectedFiles=array(draft,"files");selectedAgent=draft.optLong("agent",workspace().optLong("default_agent_id"));
        recipientLabel=text("交给 "+agentName(selectedAgent)+"  ▾",12,GOLD,true);recipientLabel.setMinHeight(dp(38));recipientLabel.setGravity(Gravity.CENTER_VERTICAL);recipientLabel.setOnClickListener(v->recipientPicker());box.addView(recipientLabel);
        android.widget.HorizontalScrollView attachmentScroll=new android.widget.HorizontalScrollView(this);attachmentScroll.setHorizontalScrollBarEnabled(false);
        fileTray=row();attachmentScroll.addView(fileTray);box.addView(attachmentScroll,new LinearLayout.LayoutParams(-1,-2));renderSelectedFiles();
        composer=input("写下想法，或交给团队一个目标…",true);composer.setBackgroundColor(Color.TRANSPARENT);composer.setPadding(0,dp(4),0,dp(7));composer.setMinLines(2);composer.setMaxLines(4);
        restoring=true;composer.setText(draft.optString("text"));restoring=false;box.addView(composer,new LinearLayout.LayoutParams(-1,-2));
        composer.addTextChangedListener(new TextWatcher(){public void beforeTextChanged(CharSequence s,int st,int c,int a){}public void onTextChanged(CharSequence s,int st,int b,int c){if(!restoring)saveComposer();}public void afterTextChanged(Editable e){}});
        LinearLayout actions=row();actions.addView(iconButton("＋","添加图片或文件",this::pickFiles));
        TextView hint=text("图片 · 文件 · 本地草稿",11,MUTED,false);actions.addView(hint,new LinearLayout.LayoutParams(0,-2,1));
        Button send=iconButton("↑","发送消息",this::sendDraft);send.setBackground(tone(GOLD,22));send.setTextColor(BG);actions.addView(send);box.addView(actions);compose.addView(box);layout.addView(compose);
        renderChatMessages(true);
        offerIncoming();
    }
    private void renderChatMessages(boolean initial) {
        if(feed==null||messagesScroll==null||state.screen!=NativeHubState.Screen.CHAT)return;
        JSONObject data=workspace();String revision=data.optString("revision");
        int oldScroll=messagesScroll.getScrollY();boolean nearBottom=initial||messagesScroll.getChildAt(0).getHeight()-oldScroll-messagesScroll.getHeight()<dp(120);
        feed.removeAllViews();JSONArray messages=array(data,"messages");if(messages.length()==0)messages=array(data,"entries");
        if(data.optLong("older_entries_before")>0)feed.addView(button("查看更早的消息",()->loadOlderMessages(data.optLong("older_entries_before"))));
        if(messages.length()==0){
            space(feed,28);feed.addView(text(state.project.isEmpty()?"先选择你的项目":"让团队开始行动",25,TEXT,true));
            feed.addView(text(state.project.isEmpty()?"项目、对话与团队都在本机页面切换。":"你可以自然地交流，也可以添加图片和文件。总控会根据任务安排合适的智能体。",14,MUTED,false));
            space(feed,18);if(state.project.isEmpty())feed.addView(button("打开项目",()->navigate(NativeHubState.Screen.PROJECTS)));
        }
        for(int i=0;i<messages.length();i++){JSONObject message=messages.optJSONObject(i);if(message!=null)messageCard(feed,message);}
        renderProgress(feed,array(data,"progress"),true);
        JSONArray jobs=store.jobs(state.userScope());for(int i=0;i<jobs.length();i++){JSONObject job=jobs.optJSONObject(i);if(job!=null&&job.optString("project").equals(state.project)&&job.optLong("conversation_id")==state.conversation)jobCard(feed,job);}
        renderedRevision=revision;messagesScroll.post(()->{if(messagesScroll==null)return;if(nearBottom)messagesScroll.fullScroll(View.FOCUS_DOWN);else messagesScroll.scrollTo(0,oldScroll);});
    }
    private void renderProgress(LinearLayout parent,JSONArray progress,boolean openDetails){
        for(int i=0;i<progress.length();i++){JSONObject item=progress.optJSONObject(i);if(item==null)continue;
            boolean contact=item.optBoolean("execution_contact_valid")&&item.optString("execution_state").equals("running"),quiet=item.optBoolean("progress_quiet");
            LinearLayout active=card(parent);active.addView(text(item.optString("agent_name","团队")+" · "+NativeProgressDisplay.heading(online,contact,quiet),12,NativeProgressDisplay.freshContact(online,contact,quiet)?GREEN:GOLD,true));
            String notice=NativeProgressDisplay.notice(online,contact,quiet);if(!notice.isEmpty())active.addView(text(notice,13,GOLD,false));
            String progressText=item.optString("assistant_text");if(progressText.isEmpty())progressText=item.optString("status_text");if(!progressText.isEmpty())active.addView(text(progressText,14,TEXT,false));
            boolean reported=item.optBoolean("has_public_report",item.optLong("sequence")>0);
            active.addView(text(reported?"距最近报告约 "+item.optString("report_age_label"):"距本轮开始约 "+item.optString("run_age_label"),11,MUTED,false));
            if(openDetails&&item.optLong("task_id")>0)active.addView(button("查看任务与停止操作",()->openTask(item.optLong("task_id"))));
        }
    }
    private void messageCard(LinearLayout parent,JSONObject message) {
        boolean mine=message.optString("role").equals("user");LinearLayout block=column();block.setPadding(dp(14),dp(12),dp(14),dp(13));
        block.setBackground(tone(mine?0xff35423f:PANEL,20));LinearLayout.LayoutParams params=new LinearLayout.LayoutParams(-1,-2);params.bottomMargin=dp(14);params.leftMargin=mine?dp(26):0;params.rightMargin=mine?0:dp(12);parent.addView(block,params);
        block.addView(text(message.optString("author",mine?"你":"团队")+"  ·  "+time(message.optString("created_at")),12,mine?GREEN:GOLD,true));
        TextView body=text(message.optString("text"),16,TEXT,false);body.setTextIsSelectable(true);body.setLineSpacing(dp(4),1.05f);block.addView(body);
        JSONArray attachments=array(message,"attachments");for(int j=0;j<attachments.length();j++){JSONObject file=attachments.optJSONObject(j);if(file!=null)attachmentCard(block,file);}
        if(mine&&!message.optString("receipt_label").isEmpty())block.addView(text(message.optString("receipt_label"),11,MUTED,false));
        if(message.optLong("task_id")>0)block.addView(button("查看任务  ↗",()->openTask(message.optLong("task_id"))));
    }
    private void jobCard(LinearLayout parent,JSONObject job) {
        String key=job.optString("request_key");LinearLayout block=card(parent);
        block.addView(text(job.optString("description"),15,TEXT,false));
        String label=sending.contains(key)?"正在发送…":checking.contains(key)?"正在核对回执…":job.optString("state").equals("conflict")?"这条发送需要检查":"已保存在本机 · 尚未确认送达";
        block.addView(text(label,12,GOLD,false));int count=array(job,"files").length();if(count>0)block.addView(text("含 "+count+" 个附件",12,MUTED,false));
        if(!job.optString("error").isEmpty())block.addView(text(job.optString("error"),12,MUTED,false));
        if(!sending.contains(key)&&!checking.contains(key)) {
            LinearLayout actions=row();actions.addView(button("核对状态",()->checkReceipt(job,true)),new LinearLayout.LayoutParams(0,dp(44),1));
            if(!job.optString("state").equals("conflict"))actions.addView(button("重试发送",()->transmit(job)),new LinearLayout.LayoutParams(0,dp(44),1));block.addView(actions);
        }
    }
    private void saveComposer(){if(composer!=null&&!composerScope.isEmpty()&&!restoring)store.draft(composerScope,composer.getText().toString(),selectedFiles,selectedAgent);}
    private void renderSelectedFiles() {
        if(fileTray==null)return;fileTray.removeAllViews();for(int i=0;i<selectedFiles.length();i++){final int index=i;JSONObject item=selectedFiles.optJSONObject(i);if(item==null)continue;
            LinearLayout row=row();row.setPadding(dp(8),0,0,0);row.setBackground(tone(BG,12));TextView name=text("▧  "+item.optString("name","附件"),12,TEXT,false);name.setMaxLines(2);row.addView(name,new LinearLayout.LayoutParams(0,-2,1));
            row.addView(iconButton("×","移除附件",()->{JSONArray next=new JSONArray();for(int j=0;j<selectedFiles.length();j++)if(j!=index)next.put(selectedFiles.opt(j));selectedFiles=next;saveComposer();renderSelectedFiles();}));LinearLayout.LayoutParams params=new LinearLayout.LayoutParams(dp(208),dp(48));params.rightMargin=dp(7);fileTray.addView(row,params);
        }
    }
    private void roomPicker() {
        if(state.project.isEmpty()){navigate(NativeHubState.Screen.PROJECTS);return;}
        JSONArray rooms=array(workspace(),"conversations");String[] titles=new String[rooms.length()+1];titles[0]="＋ 新建对话";
        for(int i=0;i<rooms.length();i++)titles[i+1]=rooms.optJSONObject(i).optString("title","工作对话");
        new AlertDialog.Builder(this).setTitle("切换对话").setItems(titles,(d,index)->openRoom(state.project,index==0?0:rooms.optJSONObject(index-1).optLong("id"))).setNegativeButton("选择其他项目",(d,w)->navigate(NativeHubState.Screen.PROJECTS)).show();
    }
    private String agentName(long id){JSONArray agents=array(workspace(),"agents");for(int i=0;i<agents.length();i++){JSONObject a=agents.optJSONObject(i);if(a!=null&&a.optLong("id")==id)return a.optString("name","总控");}return "总控";}
    private void recipientPicker() {
        JSONArray agents=array(workspace(),"agents");ArrayList<JSONObject> selectable=new ArrayList<>();
        for(int i=0;i<agents.length();i++){JSONObject a=agents.optJSONObject(i);if(a!=null&&a.optBoolean("can_dispatch"))selectable.add(a);}
        if(selectable.isEmpty()){toast("连接后可选择当前项目的智能体");return;}
        String[] names=new String[selectable.size()];for(int i=0;i<names.length;i++)names[i]=selectable.get(i).optString("name");
        new AlertDialog.Builder(this).setTitle("这次交给谁").setItems(names,(d,index)->{selectedAgent=selectable.get(index).optLong("id");recipientLabel.setText("交给 "+agentName(selectedAgent)+"  ▾");saveComposer();}).show();
    }

    private void renderProjects() {
        section("项目","每个项目有自己的团队、对话和成果。");
        JSONArray projects=array(cached("projects"),"projects");
        if(projects.length()==0)empty("还没有本机项目记录","连接后会显示你在 Hub 中的项目。");
        for(int i=0;i<projects.length();i++){JSONObject project=projects.optJSONObject(i);if(project==null)continue;
            LinearLayout block=card(content);block.addView(text(project.optString("name"),20,TEXT,true));block.addView(text(project.optString("description",""),13,MUTED,false));
            String slug=project.optString("slug");block.addView(button("打开项目  ↗",()->selectProject(slug)));
        }
    }
    private void renderAgents() {
        section("你的团队","智能体有自己的职责，每次任务由总控按需要分配。");
        JSONArray agents=array(workspace(),"agents");
        if(agents.length()==0)empty("选择项目后查看团队","没有缓存时不会用过期网页代替这个页面。");
        int[] colors={0xffb5afd2,0xffa1c2b5,0xffd6c7aa,0xffc59f99,0xff99b6ca,0xffb4bd8f};
        for(int i=0;i<agents.length();i++){JSONObject agent=agents.optJSONObject(i);if(agent==null)continue;
            LinearLayout block=card(content),line=row();TextView avatar=text("• •",24,BG,true);avatar.setGravity(Gravity.CENTER);avatar.setBackground(tone(colors[i%colors.length],26));line.addView(avatar,new LinearLayout.LayoutParams(dp(52),dp(52)));
            LinearLayout info=column();info.setPadding(dp(12),0,0,0);info.addView(text(agent.optString("name"),18,TEXT,true));info.addView(text(agent.optString("connection_label","状态待核对"),12,MUTED,false));line.addView(info,new LinearLayout.LayoutParams(0,-2,1));block.addView(line);
            block.addView(text(agent.optString("job",""),14,MUTED,false));block.addView(button("了解职责",()->{saveComposer();rememberPage();if(state.detail(NativeHubState.Screen.AGENT,agent.optLong("id"))){store.rememberLocation(state);renderShell();animatePage();}}));
        }
    }
    private void renderAgent() {
        JSONObject selected=null;JSONArray agents=array(workspace(),"agents");for(int i=0;i<agents.length();i++){JSONObject candidate=agents.optJSONObject(i);if(candidate!=null&&candidate.optLong("id")==state.detailId)selected=candidate;}
        if(selected==null){empty("暂无这位智能体的本机记录","返回团队页面刷新后再试。");return;}
        JSONObject agent=selected;section(agent.optString("name"),agent.optString("connection_label","状态待核对"));
        LinearLayout block=card(content);block.addView(text("职责",12,GOLD,true));block.addView(text(agent.optString("job"),18,TEXT,true));block.addView(text(agent.optString("instructions"),15,MUTED,false));
        if(!agent.optString("connection_hint").isEmpty())block.addView(text(agent.optString("connection_hint"),12,MUTED,false));
        Button assign=button("交给他",()->{JSONObject draft=store.draft(state.roomScope());store.draft(state.roomScope(),draft.optString("text"),array(draft,"files"),agent.optLong("id"));navigate(NativeHubState.Screen.CHAT);});assign.setEnabled(agent.optBoolean("can_dispatch"));content.addView(assign);
    }
    private void renderTasks() {
        section("任务与交付","真实进度、当前负责人和交付结果都在这里。");
        JSONArray tasks=array(workspace(),"tasks");if(tasks.length()==0)empty("目前没有活跃任务","已完成的项目仍可在历史任务中查看。");
        for(int i=0;i<tasks.length();i++){JSONObject task=tasks.optJSONObject(i);if(task!=null)taskCard(content,task);}
        content.addView(button("查看历史任务",()->navigate(NativeHubState.Screen.HISTORY)));
    }
    private void renderHistory(){
        section("历史任务","已完成或已归档的记录。");JSONArray rows=array(cached("history:"+state.project),"tasks");
        for(int j=0;j<rows.length();j++){JSONObject task=rows.optJSONObject(j);if(task!=null)taskCard(content,task);}if(rows.length()==0)empty("暂无历史任务","");
    }
    private void taskCard(LinearLayout parent,JSONObject task) {
        LinearLayout block=card(parent);block.addView(text(task.optString("status_label",task.optString("board_label",""))+"  ·  "+task.optString("assigned_agent_name","总控"),12,GOLD,true));
        block.addView(text(task.optString("title"),18,TEXT,true));String summary=task.optString("result_summary");if(!summary.isEmpty())block.addView(text(summary,14,MUTED,false));
        block.addView(button("查看详情  ↗",()->openTask(task.optLong("id"))));
    }
    private void openTask(long id){if(id<=0)return;saveComposer();rememberPage();if(!state.detail(NativeHubState.Screen.TASK,id))return;store.rememberLocation(state);renderShell();animatePage();refreshCurrent(true);}
    private void renderTask() {
        JSONObject data=cached("task:"+state.project+":"+state.detailId),task=data.optJSONObject("task");
        if(task==null){section("任务详情","本地页面");empty("正在读取这项任务","网络暂不可用时，你仍可返回其他本机页面。");return;}
        section(task.optString("title"),task.optString("status_label",task.optString("status")));
        String execution=task.isNull("execution_label")?"":task.optString("execution_label");
        LinearLayout block=card(content);block.addView(text(task.optString("assigned_agent_name","总控")+(execution.isEmpty()?"":"  ·  "+execution),12,GOLD,true));
        block.addView(text(task.optString("description"),15,TEXT,false));if(!task.optString("result_summary").isEmpty())block.addView(text(task.optString("result_summary"),15,GREEN,false));
        String cancelKey="cancel:"+state.userScope()+":"+state.project+":"+state.detailId;JSONObject pendingCancel=store.read(cancelKey);
        if(!task.optBoolean("cancel_requested")&&java.util.Arrays.asList("done","review","cancelled").contains(task.optString("status"))){store.remove(cancelKey);pendingCancel=obj();}
        if(task.optBoolean("cancel_requested"))block.addView(text(task.optString("cancel_message","停止请求已送达，等待执行电脑确认"),13,GOLD,false));
        else if(!pendingCancel.optString("state").isEmpty())block.addView(text(pendingCancel.optString("message","停止状态待核对，请刷新任务详情"),13,GOLD,false));
        if(task.optBoolean("can_cancel")&&!task.optBoolean("cancel_requested")){
            Button stop=button("请求停止任务",()->new AlertDialog.Builder(this).setTitle("请求停止这项任务？").setMessage("请求发送后会等待执行电脑确认；连接中断时可以继续查看任务状态。").setPositiveButton("请求停止",(d,w)->cancelTask(task.optLong("id"))).setNegativeButton("继续任务",null).show());
            stop.setEnabled(!mutations.contains(projectPath()+"/tasks/"+task.optLong("id")+"/cancel"));block.addView(stop);
        }
        renderProgress(content,array(data,"progress"),false);
        JSONArray messages=array(data,"messages");for(int i=0;i<messages.length();i++){JSONObject item=messages.optJSONObject(i);if(item!=null)messageCard(content,item);}
    }
    private void cancelTask(long id){
        if(id<=0)return;NativeHubState.Ticket ticket=state.ticket();String path=projectPath()+"/tasks/"+id+"/cancel";
        if(!mutations.add(path))return;String auth=cookie,token=csrf,key="cancel:"+ticket.userScope()+":"+ticket.project+":"+id;
        store.put(key,obj("state","pending","message","正在发送停止请求，执行状态尚未确认"));renderPage();
        network.execute(()->{try{NativeHubApi.Reply reply=NativeHubApi.json(ticket.endpoint,path,"POST",obj(),auth,token);main.post(()->{
            mutations.remove(path);if(destroyed||!state.accepts(ticket))return;
            if(reply.body.optBoolean("stopped"))store.remove(key);else store.put(key,obj("state","requested","message",reply.body.optString("message","停止请求已送达，等待执行电脑确认")));
            JSONObject returned=reply.body.optJSONObject("task");if(returned!=null){String cacheKey="task:"+ticket.project+":"+id;JSONObject latest=copy(cached(cacheKey));try{latest.put("task",returned);}catch(JSONException ignored){}store.cache(ticket.cacheScope(),cacheKey,latest);}
            if(state.displays(ticket)){renderPage();refreshCurrent(true);}
        });}catch(Exception error){main.post(()->{mutations.remove(path);if(destroyed||!state.accepts(ticket))return;if(error instanceof NativeHubApi.ApiError&&((NativeHubApi.ApiError)error).status>=400&&((NativeHubApi.ApiError)error).status<500)store.remove(key);else store.put(key,obj("state","uncertain","message","停止状态待核对；请刷新详情，尚未确认执行已停止"));
            if(error instanceof NativeHubApi.ApiError&&((NativeHubApi.ApiError)error).status==401)expireLogin();else if(state.displays(ticket)){renderPage();toast(error instanceof NativeHubApi.ApiError?error.getMessage():"连接中断，停止状态待核对");}
        });}});
    }
    private void renderToolbox() {
        section("工具箱","项目资料、在线设备与反馈，都有独立的本地入口。");
        content.addView(button("设置与模型服务",()->navigate(NativeHubState.Screen.SETTINGS)));
        JSONArray tools=array(cached("toolbox"),"tools");
        if(tools.length()==0)tools=new JSONArray().put(obj("title","资料库","description","图片、文档和团队交付","action","library"))
                .put(obj("title","设备","description","查看 BYBC 执行设备的实时状态","action","devices"))
                .put(obj("title","反馈与建议","description","记录遇到的问题或新的想法","action","feedback"))
                .put(obj("title","客户端下载","description","查看各平台版本与安装包","action","downloads"));
        for(int i=0;i<tools.length();i++){JSONObject tool=tools.optJSONObject(i);if(tool==null)continue;LinearLayout block=card(content);
            block.addView(text(tool.optString("title"),18,TEXT,true));block.addView(text(tool.optString("description"),13,MUTED,false));
            String action=tool.optString("action");boolean supported=java.util.Arrays.asList("library","devices","feedback","downloads").contains(action);
            Button open=button(supported?"打开  ↗":"此功能稍后提供",()->toolAction(action));open.setEnabled(supported);block.addView(open);
        }
    }
    private void toolAction(String action) {
        if(action.equals("feedback")){feedback();return;}
        if((action.equals("library")||action.equals("devices"))&&state.project.isEmpty()){toast("先选择一个项目");navigate(NativeHubState.Screen.PROJECTS);return;}
        if(action.equals("library"))navigate(NativeHubState.Screen.LIBRARY);else if(action.equals("devices"))navigate(NativeHubState.Screen.DEVICES);else if(action.equals("downloads"))navigate(NativeHubState.Screen.DOWNLOADS);
    }
    private void renderDevices() {
        section("执行设备","手机负责交流和查看，工作由在线电脑执行。");JSONArray devices=array(cached("devices:"+state.project),"devices");if(devices.length()==0)devices=array(workspace(),"devices");
        if(devices.length()==0)empty("暂无本机设备记录","联网后将核对设备状态。");
        for(int i=0;i<devices.length();i++){JSONObject item=devices.optJSONObject(i);if(item==null)continue;LinearLayout card=card(content);
            card.addView(text(item.optString("device_label",item.optString("device_id")),19,TEXT,true));
            card.addView(text(online?item.optString("presence_label","状态待核对"):"本机记录 · 连接状态待核对",14,online&&item.optBoolean("is_online")?GREEN:MUTED,false));
            card.addView(text("最近连接 "+time(item.optString("last_seen_at")),12,MUTED,false));
        }
    }
    private void renderLibrary() {
        section("项目资料","点击图片在本机预览，文件可直接保存到下载目录。");
        JSONObject data=cached("library:"+state.project);JSONArray attachments=array(data,"attachments");
        if(attachments.length()==0)empty("暂无本机资料记录","第一次连接后会保存最近的资料清单。");
        for(int i=0;i<attachments.length();i++){JSONObject item=attachments.optJSONObject(i);if(item!=null)attachmentCard(content,item);}
        if(data.optLong("older_before")>0)content.addView(button("加载更多资料",()->fetch("library-more:"+state.project,projectPath()+"/library?before="+data.optLong("older_before"),older->{
            JSONArray merged=new JSONArray();for(int i=0;i<attachments.length();i++)merged.put(attachments.opt(i));JSONArray extra=array(older,"attachments");for(int i=0;i<extra.length();i++)merged.put(extra.opt(i));
            store.cache(state.cacheScope(),"library:"+state.project,obj("attachments",merged,"older_before",older.optLong("older_before")));renderPage();
        })));
    }
    private void renderDownloads() {
        section("客户端下载","安装包由 Hub 提供，下载在当前应用完成。");JSONArray releases=array(cached("releases"),"releases");
        if(releases.length()==0)empty("暂无本机版本记录","联网刷新后即可查看最新版本。");
        for(int i=0;i<releases.length();i++){JSONObject release=releases.optJSONObject(i);if(release==null)continue;LinearLayout block=card(content);
            block.addView(text(release.optString("platform_label")+"  "+release.optString("version"),18,TEXT,true));
            block.addView(text(release.optString("filename"),12,MUTED,false));block.addView(button("下载安装包",()->download(release.optString("download_url"),release.optString("filename"),false)));
        }
    }
    private void attachmentCard(LinearLayout parent,JSONObject file) {
        LinearLayout block=card(parent);String preview=file.optString("preview_url"),url=file.optString("download_url");
        if(!preview.isEmpty()&&file.optString("mime_type").startsWith("image/")) {
            ImageView image=new ImageView(this);image.setAdjustViewBounds(true);image.setScaleType(ImageView.ScaleType.FIT_CENTER);image.setMinimumHeight(dp(90));image.setMaxHeight(dp(220));image.setBackground(tone(BG,12));
            image.setContentDescription(file.optString("name","图片"));block.addView(image,new LinearLayout.LayoutParams(-1,dp(150)));loadPreview(image,preview);image.setOnClickListener(v->download(url,file.optString("name"),true));
        }
        block.addView(text(file.optString("name","文件"),14,TEXT,true));block.addView(text(size(file.optLong("byte_size")),11,MUTED,false));
        block.addView(button("保存文件  ↓",()->download(url,file.optString("name"),false)));
    }

    private interface Loaded {void accept(JSONObject value);}
    private void fetch(String key,String path,Loaded loaded) {
        if(state.userId.isEmpty())return;NativeHubState.Ticket ticket=state.ticket();String marker=ticket.cacheScope()+":"+path;
        if(reads.contains(marker))return;reads.add(marker);String auth=cookie,token=csrf;
        network.execute(()->{try {
            NativeHubApi.Reply reply=NativeHubApi.json(ticket.endpoint,path,"GET",null,auth,token);
            main.post(()->{reads.remove(marker);if(destroyed||!state.accepts(ticket))return;
                String identity=reply.body.optString("session_key");if(!identity.isEmpty()&&!identity.equals(state.sessionKey)){expireLogin();return;}
                cookie=NativeHubCookies.update(cookie,reply.cookies);if(!reply.body.optString("csrf_token").isEmpty()){csrf=reply.body.optString("csrf_token");rememberIdentity();}
                if(key.startsWith("models:")){JSONArray providers=array(reply.body,"services");for(int i=0;i<providers.length();i++){JSONObject provider=providers.optJSONObject(i);if(provider!=null){provider.remove("api_key");provider.remove("apiKey");provider.remove("password");provider.remove("token");}}}
                if(key.startsWith("workspace:")){
                    JSONObject prior=store.cache(ticket.cacheScope(),key);
                    if(prior!=null&&prior.optBoolean("history_expanded"))try{
                        reply.body.put("messages",mergeMessages(array(prior,"messages"),array(reply.body,"messages"))).put("history_expanded",true).put("older_entries_before",prior.optLong("older_entries_before"));
                    }catch(JSONException ignored){}
                }
                store.cache(ticket.cacheScope(),key,reply.body);status("已同步 "+timeNow()+" · 本地页面",true);
                if(foreground&&state.displays(ticket))loaded.accept(reply.body);
            });
        }catch(Exception error){main.post(()->{reads.remove(marker);if(destroyed||!state.accepts(ticket))return;if(error instanceof NativeHubApi.ApiError&&((NativeHubApi.ApiError)error).status==401)expireLogin();else status("离线或连接未完成 · 本机内容与草稿保留",false);});}});
    }
    private void refreshProjects(boolean choose) {
        fetch("projects","api/mobile/projects",data->{
            JSONArray projects=array(data,"projects");
            if(choose&&state.project.isEmpty()&&state.screen==NativeHubState.Screen.CHAT&&projects.length()>0){selectProject(projects.optJSONObject(0).optString("slug"));}
            else if(state.screen==NativeHubState.Screen.PROJECTS)renderPage();
        });
    }
    private void refreshCurrent(boolean manual) {
        if(state.userId.isEmpty()){if(manual)refreshSession();return;}
        switch(state.screen) {
            case PROJECTS:refreshProjects(false);return;
            case TOOLBOX:fetch("toolbox","api/mobile/toolbox",data->renderPage());return;
            case DOWNLOADS:fetch("releases","api/releases",data->renderPage());return;
            case DEVICES:if(!state.project.isEmpty())fetch("devices:"+state.project,projectPath()+"/devices",data->renderPage());return;
            case LIBRARY:if(!state.project.isEmpty()&&manual)fetch("library:"+state.project,projectPath()+"/library",data->renderPage());return;
            case TASK:if(!state.project.isEmpty())fetch("task:"+state.project+":"+state.detailId,projectPath()+"/tasks/"+state.detailId,data->renderPage());return;
            case HISTORY:if(!state.project.isEmpty())fetch("history:"+state.project,projectPath()+"/tasks?history=1",data->renderPage());return;
            case SETTINGS:return;
            case MODELS:refreshModels(manual);return;
            default:break;
        }
        if(state.project.isEmpty()){if(manual)refreshProjects(true);return;}
        String key=workspaceKey();fetch(key,projectPath()+"/workspace?conversation_id="+state.conversation,data->{
            if(state.screen==NativeHubState.Screen.CHAT){
                if(manual||!renderedRevision.equals(data.optString("revision")))renderChatMessages(false);
                if(selectedAgent==0&&data.optLong("default_agent_id")>0){selectedAgent=data.optLong("default_agent_id");if(recipientLabel!=null)recipientLabel.setText("交给 "+agentName(selectedAgent)+"  ▾");saveComposer();}
            }else renderPage();
        });
    }
    private void loadOlderMessages(long before) {
        NativeHubState.Ticket ticket=state.ticket();String key=workspaceKey();JSONObject current=workspace();
        fetch("older:"+key+":"+before,projectPath()+"/workspace?conversation_id="+state.conversation+"&before="+before,older->{
            if(!state.displays(ticket))return;JSONArray messages=new JSONArray();JSONArray prior=array(older,"messages"),recent=array(current,"messages");
            for(int i=0;i<prior.length();i++)messages.put(prior.opt(i));for(int i=0;i<recent.length();i++)messages.put(recent.opt(i));
            JSONObject merged=copy(workspace());try{merged.put("messages",mergeMessages(prior,array(merged,"messages"))).put("history_expanded",true).put("older_entries_before",older.optLong("older_entries_before"));}catch(JSONException ignored){}
            int oldHeight=feed==null?0:feed.getHeight(),oldY=messagesScroll==null?0:messagesScroll.getScrollY();ScrollView reading=messagesScroll;
            store.cache(ticket.cacheScope(),key,merged);renderChatMessages(false);
            if(reading!=null)reading.post(()->{if(state.displays(ticket)&&reading==messagesScroll)reading.scrollTo(0,oldY+Math.max(0,feed.getHeight()-oldHeight));});
        });
    }

    private void warmCache(){
        NativeHubState.Ticket ticket=state.ticket();store.warm(ticket.cacheScope(),()->main.post(()->{
            if(destroyed||!state.accepts(ticket)||!state.displays(ticket))return;
            if(state.screen==NativeHubState.Screen.CHAT){
                renderChatMessages(false);
                if(recipientLabel!=null)recipientLabel.setText("交给 "+agentName(selectedAgent)+"  ▾");
                JSONObject project=workspace().optJSONObject("project");
                if(project!=null&&chatContext!=null&&chatContext.getChildAt(0) instanceof Button)((Button)chatContext.getChildAt(0)).setText(project.optString("name",state.project)+"  ▾");
            }else renderPage();
        }));
    }
    private static JSONArray mergeMessages(JSONArray first,JSONArray second){
        java.util.TreeMap<Long,JSONObject> rows=new java.util.TreeMap<>();
        for(JSONArray group:new JSONArray[]{first,second})for(int i=0;i<group.length();i++){JSONObject item=group.optJSONObject(i);if(item!=null)rows.put(item.optLong("id"),item);}
        JSONArray result=new JSONArray();for(JSONObject row:rows.values())result.put(row);return result;
    }

    private void sendDraft(){
        if(composer==null||state.userId.isEmpty()||state.project.isEmpty()){toast("先连接并选择项目");return;}
        String text=composer.getText().toString().trim();if(text.isEmpty()&&selectedFiles.length()==0)return;
        JSONArray pending=store.jobs(state.userScope());
        if(state.conversation==0)for(int i=0;i<pending.length();i++){JSONObject old=pending.optJSONObject(i);if(old!=null&&old.optString("project").equals(state.project)&&old.optLong("conversation_id")==0){toast("首条消息正在核对，你可以先继续写草稿");checkReceipt(old,true);return;}}
        JSONObject job=obj("request_key",NativeHubState.requestKey(),"project",state.project,"conversation_id",state.conversation,
                "description",text,"agent_id",selectedAgent,"files",copyArray(selectedFiles),"created_at",System.currentTimeMillis(),"state","saved");
        if(!store.job(state.userScope(),job)){toast("本机暂时无法保存，文字与附件已保留");return;}
        restoring=true;composer.setText("");selectedFiles=new JSONArray();restoring=false;saveComposer();renderSelectedFiles();renderChatMessages(false);transmit(job);
    }
    private void transmit(JSONObject job){
        String key=job.optString("request_key");if(!NativeHubState.validRequestKey(key)||sending.contains(key)||checking.contains(key)||state.userId.isEmpty())return;
        if(!store.job(state.userScope(),job)){toast("本机保存尚未完成，请稍后重试");return;}
        NativeHubState.Ticket ticket=state.ticket();String auth=cookie,token=csrf;JSONObject immutable=copy(job);
        sending.add(key);renderChatMessages(false);
        network.execute(()->{try{
            List<NativeHubApi.Upload> uploads=new ArrayList<>();JSONArray attachments=array(immutable,"files");
            for(int i=0;i<attachments.length();i++){JSONObject item=attachments.getJSONObject(i);Uri uri=Uri.parse(item.getString("uri"));uploads.add(new NativeHubApi.Upload(SharedAttachmentProvider.file(this,uri),item.optString("name"),item.optString("type")));}
            NativeHubApi.Reply reply=NativeHubApi.send(ticket.endpoint,"api/mobile/projects/"+Uri.encode(immutable.optString("project"))+"/messages",immutable,uploads,auth,token);
            main.post(()->{sending.remove(key);if(destroyed||!state.accepts(ticket))return;confirmJob(ticket,immutable,reply.body);});
        }catch(Exception error){main.post(()->{
            sending.remove(key);if(destroyed||!state.accepts(ticket))return;
            JSONObject failed=copy(immutable);try{failed.put("state",error instanceof NativeHubApi.ApiError&&((NativeHubApi.ApiError)error).status==409?"conflict":"uncertain").put("error",error instanceof NativeHubApi.ApiError?error.getMessage():"连接中断，正在保留原消息并核对回执");}catch(JSONException ignored){}
            store.job(ticket.userScope(),failed);renderChatMessages(false);
            if(error instanceof NativeHubApi.ApiError&&((NativeHubApi.ApiError)error).status==401)expireLogin();else checkReceipt(failed,false);
        });}});
    }
    private void checkReceipts(){
        JSONArray jobs=store.jobs(state.userScope());int count=jobs.length();if(count==0){receiptCursor=0;return;}
        int visited=0,scheduled=0;while(visited<count&&scheduled<4){
            JSONObject job=jobs.optJSONObject(receiptCursor%count);receiptCursor=(receiptCursor+1)%count;visited++;
            if(job==null||job.optString("state").equals("conflict")||sending.contains(job.optString("request_key"))||checking.contains(job.optString("request_key")))continue;
            checkReceipt(job,false);scheduled++;
        }
    }
    private void checkReceipt(JSONObject job,boolean explicit){
        String key=job.optString("request_key");if(!NativeHubState.validRequestKey(key)||sending.contains(key)||checking.contains(key)||state.userId.isEmpty())return;
        NativeHubState.Ticket ticket=state.ticket();String auth=cookie,token=csrf;checking.add(key);if(explicit)renderChatMessages(false);
        network.execute(()->{try{
            NativeHubApi.Reply reply=NativeHubApi.json(ticket.endpoint,"api/mobile/projects/"+Uri.encode(job.optString("project"))+"/receipts/"+key,"GET",null,auth,token);
            main.post(()->{checking.remove(key);if(destroyed||!state.accepts(ticket))return;
                if(reply.body.optBoolean("found"))confirmJob(ticket,job,reply.body);else{renderChatMessages(false);if(explicit)toast("尚无送达回执，原消息已保存，可点重试发送");}
            });
        }catch(Exception error){main.post(()->{checking.remove(key);if(destroyed||!state.accepts(ticket))return;if(error instanceof NativeHubApi.ApiError&&((NativeHubApi.ApiError)error).status==401)expireLogin();else if(explicit){toast("暂时无法核对，原消息仍在本机");renderChatMessages(false);}});}});
    }
    private void confirmJob(NativeHubState.Ticket ticket,JSONObject job,JSONObject receipt){
        if(!state.accepts(ticket))return;long room=receipt.optLong("conversation_id");if(room<=0)return;
        String key=job.optString("request_key");if(!receipt.optString("request_key",key).equals(key))return;
        store.removeJob(ticket.userScope(),key);
        JSONArray sentFiles=array(job,"files");files.execute(()->{for(int i=0;i<sentFiles.length();i++){JSONObject file=sentFiles.optJSONObject(i);if(file!=null&&!store.referenced(file.optString("uri")))importer.remove(Uri.parse(file.optString("uri")));}});
        if(state.project.equals(job.optString("project"))&&state.conversation==job.optLong("conversation_id")){
            if(state.conversation==0&&(state.displays(ticket)||state.screen!=NativeHubState.Screen.CHAT)){
                boolean stillReading=state.displays(ticket)&&state.screen==NativeHubState.Screen.CHAT;
                saveComposer();String oldScope=state.roomScope();JSONObject follow=store.draft(oldScope);state.resolveConversation(state.project,state.conversation,room);store.rememberLocation(state);
                store.draft(state.roomScope(),follow.optString("text"),array(follow,"files"),follow.optLong("agent",selectedAgent));
                store.draft(oldScope,"",new JSONArray(),follow.optLong("agent",selectedAgent));
                if(stillReading){composer=null;renderShell();}
            }
            refreshCurrent(true);renderChatMessages(false);
        }
        status("消息已送达 · 团队正在接手",true);
    }
    private void pickFiles(){
        if(state.userId.isEmpty()||state.project.isEmpty()){toast("先选择项目，再添加附件");return;}
        saveComposer();pickerTicket=state.ticket();Intent pick=new Intent(Intent.ACTION_OPEN_DOCUMENT);pick.setType("*/*");pick.addCategory(Intent.CATEGORY_OPENABLE);
        pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE,true);pick.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION|Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);startActivityForResult(pick,PICK_FILES);
    }
    @Override protected void onActivityResult(int request,int result,Intent data){
        super.onActivityResult(request,result,data);
        if(result!=RESULT_OK||data==null)return;
        if(request==PICK_FILES&&pickerTicket!=null){
            ArrayList<Uri> uris=new ArrayList<>();if(data.getClipData()!=null)for(int i=0;i<data.getClipData().getItemCount();i++)uris.add(data.getClipData().getItemAt(i).getUri());else if(data.getData()!=null)uris.add(data.getData());
            NativeHubState.Ticket ticket=pickerTicket;pickerTicket=null;importSelected(uris,ticket,false);
        }else if(request==SAVE_FILE&&pendingSave!=null&&data.getData()!=null){HubFileDownload.Result download=pendingSave;pendingSave=null;writeDocument(download,data.getData());}
    }
    private void importSelected(List<Uri> sources,NativeHubState.Ticket target,boolean inbox){
        files.execute(()->{
            JSONArray copied=new JSONArray();String error="";
            for(Uri source:sources)try{
                if(copied.length()>=SharedFilePolicy.MAX_FILES)break;Uri local=importer.copy(source,null);
                copied.put(obj("uri",local.toString(),"name",local.getLastPathSegment(),"type",local.getQueryParameter("type")));
            }catch(Exception failure){error=failure.getMessage();}
            final String failureText=error;
            main.post(()->{
                if(inbox){JSONArray pending=array(store.read("share-inbox"),"files");for(int i=0;i<copied.length();i++)pending.put(copied.opt(i));store.put("share-inbox",obj("files",pending));}
                else if(target!=null){
                    JSONObject draft=store.draft(target.roomScope());JSONArray all=array(draft,"files");for(int i=0;i<copied.length();i++){JSONObject next=copied.optJSONObject(i);if(all.length()<SharedFilePolicy.MAX_FILES)all.put(next);else if(next!=null){JSONArray pending=array(store.read("share-inbox"),"files");pending.put(next);store.put("share-inbox",obj("files",pending));}}
                    store.draft(target.roomScope(),draft.optString("text"),all,draft.optLong("agent"));
                }
                if(destroyed)return;
                if(!failureText.isEmpty())toast(failureText);
                if(inbox)offerIncoming();else if(state.accepts(target)&&state.roomScope().equals(target.roomScope())&&composer!=null){
                    selectedFiles=array(store.draft(target.roomScope()),"files");renderSelectedFiles();toast("附件已保存在本机");
                }
            });
        });
    }
    private void collectIncoming(Intent intent){
        if(intent==null)return;ArrayList<Uri> sources=new ArrayList<>();String action=intent.getAction();
        if(Intent.ACTION_SEND.equals(action)){Uri source=intent.getParcelableExtra(Intent.EXTRA_STREAM);if(source!=null)sources.add(source);}
        else if(Intent.ACTION_SEND_MULTIPLE.equals(action)){ArrayList<Uri> group=intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM);if(group!=null)sources.addAll(group);}
        else if(Intent.ACTION_VIEW.equals(action)&&intent.getData()!=null){
            if("content".equals(intent.getData().getScheme()))sources.add(intent.getData());else routeIntent(intent.getData().toString());
        }
        String link=intent.getStringExtra("hub_url");if(link==null)link=intent.getStringExtra("url");if(link!=null)routeIntent(link);
        if(!sources.isEmpty())importSelected(sources,null,true);
        intent.removeExtra(Intent.EXTRA_STREAM);intent.setAction(null);
    }
    private void routeIntent(String url){
        if(!NativeHubApi.internal(state.endpoint,url))return;
        String relative=URI.create(url).getPath().substring(URI.create(state.endpoint).getPath().length());String[] parts=relative.split("/");
        if(relative.matches("settings/?")||relative.matches("settings/models/?")){
            if(state.userId.isEmpty())store.put("pending-link:"+NativeHubState.digest(state.endpoint),obj("url",url));
            else navigate(relative.contains("models")?NativeHubState.Screen.MODELS:NativeHubState.Screen.SETTINGS);
            return;
        }
        if(parts.length>=2&&parts[0].equals("projects")){
            if(state.userId.isEmpty()){store.put("pending-link:"+NativeHubState.digest(state.endpoint),obj("url",url));return;}
            String slug=Uri.decode(parts[1]);long prior=store.read("room:"+state.userScope()+":"+slug).optLong("conversation");
            if(parts.length>=4&&parts[2].equals("conversations"))openRoom(slug,parseLong(parts[3]));else selectProject(slug);
            if(parts.length>=4&&parts[2].equals("tasks"))openTask(parseLong(parts[3]));
        }
    }
    private boolean offeringShare=false;
    private void offerIncoming(){
        if(destroyed||offeringShare||state.userId.isEmpty()||state.project.isEmpty())return;
        JSONArray inbox=array(store.read("share-inbox"),"files");if(inbox.length()==0)return;offeringShare=true;NativeHubState.Ticket target=state.ticket();
        new AlertDialog.Builder(this).setTitle("添加分享来的附件").setMessage(inbox.length()+" 个文件已保存在本机。添加到当前对话的草稿？")
                .setPositiveButton("添加到草稿",(d,w)->{if(!state.displays(target))return;saveComposer();JSONObject draft=store.draft(target.roomScope());JSONArray all=array(draft,"files"),remaining=new JSONArray();for(int i=0;i<inbox.length();i++)if(all.length()<SharedFilePolicy.MAX_FILES)all.put(inbox.opt(i));else remaining.put(inbox.opt(i));store.draft(target.roomScope(),draft.optString("text"),all,draft.optLong("agent"));store.put("share-inbox",obj("files",remaining));selectedFiles=all;renderSelectedFiles();})
                .setNegativeButton("稍后",null).setOnDismissListener(d->offeringShare=false).show();
    }
    private void loadPreview(ImageView image,String url){
        NativeHubState.Ticket ticket=state.ticket();String auth=cookie;
        File directory=new File(getCacheDir(),"native-images/"+ticket.cacheScope());File saved=new File(directory,NativeHubState.digest(url)+".image");
        network.execute(()->{try{
            if(!saved.isFile()){HubFileDownload.Result result=NativeHubApi.file(ticket.endpoint,url,auth,directory,16L*1024*1024);if(!result.type.startsWith("image/")){result.file.delete();return;}if(!result.file.renameTo(saved)){result.file.delete();return;}}
            Bitmap bitmap=decode(saved,1200);main.post(()->{if(bitmap!=null&&!destroyed&&state.displays(ticket)&&image.isAttachedToWindow())image.setImageBitmap(bitmap);});
        }catch(Exception ignored){}});
    }
    private static Bitmap decode(File file,int max){
        BitmapFactory.Options options=new BitmapFactory.Options();options.inJustDecodeBounds=true;BitmapFactory.decodeFile(file.getAbsolutePath(),options);
        if(options.outWidth<=0||options.outHeight<=0)return null;options.inSampleSize=1;
        while(Math.max(options.outWidth,options.outHeight)/options.inSampleSize>max)options.inSampleSize*=2;
        options.inJustDecodeBounds=false;return BitmapFactory.decodeFile(file.getAbsolutePath(),options);
    }
    private void download(String url,String name,boolean preview){
        NativeHubState.Ticket ticket=state.ticket();String auth=cookie;toast(preview?"正在准备预览…":"正在下载…");
        network.execute(()->{try{
            HubFileDownload.Result result=NativeHubApi.file(ticket.endpoint,url,auth,new File(getCacheDir(),"native-downloads/"+ticket.cacheScope()),preview?64L*1024*1024:HubFileDownload.MAX_BYTES);
            Bitmap bitmap=preview&&result.type.startsWith("image/")?decode(result.file,2400):null;
            main.post(()->{if(destroyed||!state.accepts(ticket)){result.file.delete();return;}
                if(bitmap!=null){ImageView picture=new ImageView(this);picture.setImageBitmap(bitmap);picture.setAdjustViewBounds(true);picture.setScaleType(ImageView.ScaleType.FIT_CENTER);ScrollView scroll=new ScrollView(this);scroll.addView(picture);
                    new AlertDialog.Builder(this).setTitle(name).setView(scroll).setPositiveButton("保存文件",(d,w)->saveDownload(result,ticket)).setNegativeButton("关闭",(d,w)->result.file.delete()).show();
                }else saveDownload(result,ticket);
            });
        }catch(Exception error){main.post(()->{if(!destroyed&&state.accepts(ticket))toast(error.getMessage());});}});
    }
    private void saveDownload(HubFileDownload.Result result,NativeHubState.Ticket ticket){
        if(!state.accepts(ticket))return;
        if(Build.VERSION.SDK_INT>=29){
            files.execute(()->{Uri uri=null;try{
                ContentValues values=new ContentValues();values.put(MediaStore.Downloads.DISPLAY_NAME,result.name);values.put(MediaStore.Downloads.MIME_TYPE,result.type);values.put(MediaStore.Downloads.RELATIVE_PATH,Environment.DIRECTORY_DOWNLOADS+"/BYBC Hub");values.put(MediaStore.Downloads.IS_PENDING,1);
                uri=getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI,values);if(uri==null)throw new Exception("无法保存下载");
                copyFile(result.file,uri);ContentValues done=new ContentValues();done.put(MediaStore.Downloads.IS_PENDING,0);getContentResolver().update(uri,done,null,null);
                result.file.delete();main.post(()->{if(!destroyed&&state.accepts(ticket))toast("已保存到下载目录 / BYBC Hub");});
            }catch(Exception error){if(uri!=null)getContentResolver().delete(uri,null,null);main.post(()->{if(!destroyed&&state.accepts(ticket))toast("保存未完成，可重试下载");});}});
        }else{pendingSave=result;Intent create=new Intent(Intent.ACTION_CREATE_DOCUMENT);create.addCategory(Intent.CATEGORY_OPENABLE);create.setType(result.type);create.putExtra(Intent.EXTRA_TITLE,result.name);startActivityForResult(create,SAVE_FILE);}
    }
    private void writeDocument(HubFileDownload.Result result,Uri uri){files.execute(()->{try{copyFile(result.file,uri);result.file.delete();main.post(()->{if(!destroyed)toast("文件已保存");});}catch(Exception error){main.post(()->{if(!destroyed)toast("文件保存未完成");});}});}
    private void copyFile(File file,Uri uri)throws Exception{try(InputStream input=new FileInputStream(file);OutputStream output=getContentResolver().openOutputStream(uri)){if(output==null)throw new Exception("文件不可写");byte[] bytes=new byte[65536];int n;while((n=input.read(bytes))!=-1)output.write(bytes,0,n);}}
    private void feedback(){
        if(state.project.isEmpty()){toast("先选择需要反馈的项目");navigate(NativeHubState.Screen.PROJECTS);return;}
        NativeHubState.Ticket ticket=state.ticket();String scope="feedback:"+ticket.userScope()+":"+ticket.project;JSONObject saved=store.read(scope);
        LinearLayout body=column();body.setPadding(dp(20),0,dp(20),0);EditText title=input("问题或想法",false),details=input("发生了什么？希望如何改进？",true);title.setText(saved.optString("title"));details.setText(saved.optString("body"));body.addView(title);body.addView(details);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("反馈给工程师").setView(body).setPositiveButton("提交",null).setNegativeButton("保存草稿",(d,w)->store.put(scope,obj("title",title.getText(),"body",details.getText()))).create();
        final String[] feedbackKey={saved.optString("request_key",NativeHubState.requestKey())};
        dialog.setOnShowListener(d->dialog.getButton(-1).setOnClickListener(v->{
            String heading=title.getText().toString().trim(),note=details.getText().toString().trim();if(heading.isEmpty()||note.isEmpty()){toast("写下问题和补充说明");return;}
            if(!state.accepts(ticket))return;
            JSONObject payload=obj("request_key",feedbackKey[0],"kind","bug","title",heading,"body",note,"conversation_id",ticket.conversation>0?ticket.conversation:JSONObject.NULL,"platform","android");
            if(!store.putDurable(scope,payload)){toast("本机暂时无法保存，请稍后重试");return;}title.setEnabled(false);details.setEnabled(false);String auth=cookie,token=csrf;dialog.getButton(-1).setEnabled(false);
            network.execute(()->{try{NativeHubApi.json(ticket.endpoint,"api/mobile/projects/"+Uri.encode(ticket.project)+"/feedback","POST",payload,auth,token);main.post(()->{if(destroyed||!state.accepts(ticket))return;store.remove(scope);dialog.dismiss();toast("已记录，工程师会结合设备和日志处理");});}
                catch(Exception error){main.post(()->{if(destroyed||!state.accepts(ticket))return;dialog.getButton(-1).setEnabled(true);toast("暂未提交成功，反馈内容已保存");});}});
        }));showDialog(dialog);
    }
    private void renderSettings(){
        section("设置","账号、连接和体验偏好，直接保存在本机界面。");
        LinearLayout account=card(content);account.addView(text(username.isEmpty()?"当前账号":username,20,TEXT,true));
        account.addView(text("Android "+BuildConfig.VERSION_NAME,12,MUTED,false));
        LinearLayout connection=card(content);connection.addView(text("Hub 连接",18,TEXT,true));connection.addView(text(state.endpoint,13,MUTED,false));
        connection.addView(text(connectionText,13,online?GREEN:MUTED,false));
        connection.addView(button("重新核对连接",this::refreshSession));connection.addView(button("修改连接地址",this::serverSettings));
        LinearLayout experience=card(content);experience.addView(text("界面体验",18,TEXT,true));Switch motion=new Switch(this);motion.setText("减少动态效果");motion.setTextColor(TEXT);motion.setTextSize(16);motion.setMinHeight(dp(48));motion.setChecked(reducedMotion);
        motion.setOnCheckedChangeListener((view,enabled)->{reducedMotion=enabled;store.put("appearance",obj("reduced_motion",enabled));if(enabled&&page!=null){page.animate().cancel();page.setAlpha(1f);page.setTranslationY(0f);}});experience.addView(motion);
        experience.addView(text("页面切换与按下反馈在手机本地完成；同时遵循系统动画设置。",13,MUTED,false));
        content.addView(button("模型与 API 服务",()->navigate(NativeHubState.Screen.MODELS)));
        content.addView(button("收到的分享附件",()->{if(array(store.read("share-inbox"),"files").length()==0)toast("暂无待添加的附件");else offerIncoming();}));
        content.addView(button("退出登录",()->new AlertDialog.Builder(this).setTitle("退出当前账号？").setMessage("本机未发送的草稿和附件会保留，重新登录同一账号后可继续。").setPositiveButton("退出登录",(d,w)->logout()).setNegativeButton("留在这里",null).show()));
    }
    private String modelsPath(){return "api/projects/"+Uri.encode(state.project)+"/model-services";}
    private void refreshModels(boolean manual){if(!state.project.isEmpty())fetch("models:"+state.project,modelsPath(),data->renderPage());}
    private void renderModels(){
        section("模型与 API 服务","连接执行电脑上的本地模型，或兼容 OpenAI 的 API。");
        if(state.project.isEmpty()){empty("先选择项目","模型配置属于当前项目，保存后由所选电脑使用。");content.addView(button("选择项目",()->navigate(NativeHubState.Screen.PROJECTS)));return;}
        JSONObject data=cached("models:"+state.project);boolean canManage=data.optBoolean("can_manage");
        LinearLayout info=card(content);info.addView(text("当前项目 · "+state.project,16,TEXT,true));info.addView(text("本地地址由执行电脑访问。127.0.0.1 指该电脑；手机负责配置与查看。",13,MUTED,false));
        info.addView(text(online?"配置保存后，点测试连接核对服务与模型。":"当前显示本机记录，连接状态待核对。",13,MUTED,false));
        if(canManage)info.addView(button("给智能体分配模型",this::choosePolicyAgent));
        JSONArray services=array(data,"services");if(services.length()==0)empty("尚无模型服务记录",data.has("can_manage")?"可接入 Ollama、LM Studio 或兼容 API。":"联网后核对项目权限与已保存的服务。");
        for(int i=0;i<services.length();i++){
            JSONObject service=services.optJSONObject(i);if(service==null)continue;LinearLayout block=card(content);
            block.addView(text(service.optString("name","模型服务"),19,TEXT,true));block.addView(text(service.optString("kind")+" · "+service.optString("model","未选择模型"),13,GOLD,false));
            block.addView(text(service.optString("base_url"),12,MUTED,false));block.addView(text("执行电脑 · "+service.optString("device_id"),12,MUTED,false));
            block.addView(text(online?service.optString("status_message",service.optString("status","尚未测试")):"离线记录 · 服务状态待核对",13,MUTED,false));
            block.addView(text(service.optBoolean("has_api_key")?"API 密钥已设置":"未设置 API 密钥",12,MUTED,false));
            JSONArray models=array(service,"models");if(models.length()>0){ArrayList<String> names=new ArrayList<>();for(int j=0;j<Math.min(models.length(),12);j++){Object value=models.opt(j);names.add(value instanceof JSONObject?((JSONObject)value).optString("id"):String.valueOf(value));}block.addView(text("可用模型 · "+android.text.TextUtils.join("、",names),12,MUTED,false));}
            if(canManage){
                Button test=button("测试连接",()->modelMutation("POST","/"+service.optLong("id")+"/test",obj(),null));test.setEnabled(!mutations.contains(modelsPath()+"/"+service.optLong("id")+"/test"));block.addView(test);
                block.addView(button("编辑服务",()->editModel(service,data)));
                block.addView(button("移除服务",()->new AlertDialog.Builder(this).setTitle("移除模型服务？").setMessage(service.optString("name")+" 将不再出现在这个项目的配置中。").setPositiveButton("移除",(d,w)->modelMutation("DELETE","/"+service.optLong("id"),null,null)).setNegativeButton("取消",null).show()));
            }
        }
        if(canManage)content.addView(button("添加模型服务",()->editModel(new JSONObject(),data)));
        else if(data.has("can_manage"))content.addView(text("仅项目所有者可以修改模型服务。",13,MUTED,false));
        content.addView(button("刷新服务状态",()->refreshModels(true)));
    }
    private Spinner choice(String[] labels,int selected){Spinner view=new Spinner(this);ArrayAdapter<String> adapter=new ArrayAdapter<>(this,android.R.layout.simple_spinner_dropdown_item,labels);view.setAdapter(adapter);view.setSelection(Math.max(0,selected));view.setMinimumHeight(dp(48));return view;}
    private ScrollView dialogScroll(LinearLayout form){
        ScrollView scroll=new ScrollView(this){@Override protected void onMeasure(int width,int height){
            android.graphics.Rect available=new android.graphics.Rect();root.getWindowVisibleDisplayFrame(available);
            int limit=Math.max(dp(120),available.height()-dp(192));
            super.onMeasure(width,View.MeasureSpec.makeMeasureSpec(Math.min(View.MeasureSpec.getSize(height),limit),View.MeasureSpec.AT_MOST));
        }};scroll.addView(form);return scroll;
    }
    private void showDialog(AlertDialog dialog){
        dialog.getWindow().setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        if(Build.VERSION.SDK_INT>=30)dialog.getWindow().setDecorFitsSystemWindows(true);
        dialog.show();
    }
    private void choosePolicyAgent(){
        fetch("settings:"+state.project,"api/projects/"+Uri.encode(state.project)+"/settings",data->{
            JSONArray agents=array(data,"agents");if(agents.length()==0){toast("当前项目还没有可设置的智能体");return;}String[] labels=new String[agents.length()];
            for(int i=0;i<labels.length;i++)labels[i]=agents.optJSONObject(i).optString("name","智能体");
            new AlertDialog.Builder(this).setTitle("选择智能体").setItems(labels,(d,index)->{
                JSONObject agent=agents.optJSONObject(index);String path="api/projects/"+Uri.encode(state.project)+"/settings/agents/"+agent.optLong("id")+"/model-policy";
                fetch("policy:"+state.project+":"+agent.optLong("id"),path,policy->editModelPolicy(agent,policy,path));
            }).setNegativeButton("取消",null).show();
        });
    }
    private void editModelPolicy(JSONObject agent,JSONObject data,String path){
        NativeHubState.Ticket ticket=state.ticket();JSONArray models=array(data,"models");JSONObject policy=data.optJSONObject("policy");if(policy==null)policy=obj();
        String[] labels=new String[models.length()+1];labels[0]="由总控分配（默认）";int selected=0;
        for(int i=0;i<models.length();i++){JSONObject model=models.optJSONObject(i);labels[i+1]=model.optString("displayName",model.optString("model"));if(policy.optString("mode").equals("manual")&&model.optString("model").equals(policy.optString("model")))selected=i+1;}
        LinearLayout form=column();form.setPadding(dp(20),dp(8),dp(20),dp(16));form.addView(text("设置只影响之后的新任务，已开始的任务继续使用原模型。",13,MUTED,false));
        if(models.length()==0)form.addView(text("执行电脑尚未上报可用模型。请更新并连接电脑端，再核对服务。",13,GOLD,false));
        Spinner modelChoice=choice(labels,selected),effortChoice=choice(new String[]{"默认"},0);form.addView(modelChoice);form.addView(text("思考档位",13,GOLD,true));form.addView(effortChoice);
        ArrayList<String> efforts=new ArrayList<>();String currentEffort=policy.optString("reasoning_effort");
        modelChoice.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener(){public void onNothingSelected(android.widget.AdapterView<?> parent){}public void onItemSelected(android.widget.AdapterView<?> parent,View view,int position,long id){
            efforts.clear();ArrayList<String> names=new ArrayList<>();JSONArray rows=position>0?array(models.optJSONObject(position-1),"supportedReasoningEfforts"):new JSONArray();int selection=0;
            for(int i=0;i<rows.length();i++){JSONObject row=rows.optJSONObject(i);String effort=row.optString("reasoningEffort");efforts.add(effort);names.add(effort.equals("none")?"无额外思考档位":effort);if(effort.equals(currentEffort))selection=i;}
            if(names.isEmpty()){efforts.add("");names.add("默认");}effortChoice.setAdapter(new ArrayAdapter<>(NativeHubActivity.this,android.R.layout.simple_spinner_dropdown_item,names));effortChoice.setSelection(selection);
        }});
        ScrollView scroll=dialogScroll(form);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle(agent.optString("name","智能体")+" · 执行模型").setView(scroll).setPositiveButton("保存分配",null).setNegativeButton("取消",null).create();
        dialog.setOnShowListener(d->dialog.getButton(-1).setOnClickListener(v->{
            if(!state.displays(ticket)){dialog.dismiss();return;}int index=modelChoice.getSelectedItemPosition();JSONObject payload=obj("mode",index==0?"controller":"manual","model",index==0?"":models.optJSONObject(index-1).optString("model"),"reasoning_effort",index==0?"":efforts.get(effortChoice.getSelectedItemPosition()),"reason","用户在 Android 设置中指定");
            if(!mutations.add(path))return;dialog.getButton(-1).setEnabled(false);String auth=cookie,token=csrf;
            network.execute(()->{try{NativeHubApi.json(ticket.endpoint,path,"POST",payload,auth,token);main.post(()->{mutations.remove(path);if(destroyed||!state.accepts(ticket))return;dialog.dismiss();toast("模型分配已保存，将用于这位智能体的新任务");});}
                catch(Exception error){main.post(()->{mutations.remove(path);if(destroyed||!state.accepts(ticket))return;dialog.getButton(-1).setEnabled(true);toast(error instanceof NativeHubApi.ApiError?error.getMessage():"连接中断，分配结果待核对；重新打开此设置可查看");});}});
        }));showDialog(dialog);
    }
    private void editModel(JSONObject existing,JSONObject data){
        JSONArray devices=array(data,"devices");if(devices.length()==0){toast("先连接一台执行电脑，再添加模型服务");return;}
        NativeHubState.Ticket ticket=state.ticket();LinearLayout form=column();form.setPadding(dp(20),dp(8),dp(20),dp(16));ScrollView scroll=dialogScroll(form);
        EditText name=input("服务名称",false),base=input("服务地址",false),model=input("模型名称，例如 qwen3:8b",false),secret=input("API 密钥（留空保留已有密钥）",false);
        secret.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD);secret.setSaveEnabled(false);if(Build.VERSION.SDK_INT>=26)secret.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        name.setText(existing.optString("name"));base.setText(existing.optString("base_url","http://127.0.0.1:11434/v1"));model.setText(existing.optString("model"));
        String[] kinds={"ollama","lmstudio","openai_compatible"},kindNames={"Ollama（本地）","LM Studio（本地）","兼容 OpenAI 的 API"};int selectedKind=0;for(int i=0;i<kinds.length;i++)if(kinds[i].equals(existing.optString("kind")))selectedKind=i;
        Spinner kind=choice(kindNames,selectedKind);String[] deviceNames=new String[devices.length()];int deviceSelection=0;
        for(int i=0;i<devices.length();i++){JSONObject device=devices.optJSONObject(i);deviceNames[i]=device.optString("device_label",device.optString("device_id"))+(device.optBoolean("is_agent_online")?" · 在线":" · 离线");if(device.optString("device_id").equals(existing.optString("device_id")))deviceSelection=i;}
        Spinner device=choice(deviceNames,deviceSelection);Switch enabled=new Switch(this);enabled.setText("启用此服务");enabled.setTextColor(TEXT);enabled.setChecked(existing.optBoolean("enabled",true));
        Switch clear=new Switch(this);clear.setText("清除已保存的 API 密钥");clear.setTextColor(TEXT);
        Switch images=new Switch(this),toolSupport=new Switch(this);images.setText("服务支持图片输入");images.setTextColor(TEXT);images.setChecked(existing.optBoolean("supports_images"));toolSupport.setText("服务支持工具调用");toolSupport.setTextColor(TEXT);toolSupport.setChecked(existing.optBoolean("supports_tools"));
        for(Switch control:new Switch[]{enabled,clear,images,toolSupport})control.setMinHeight(dp(48));
        form.addView(text("服务类型",13,GOLD,true));form.addView(kind);form.addView(name);space(form,8);form.addView(base);form.addView(text("本地地址由下方执行电脑访问；远程 API 请填写 HTTPS 地址。",12,MUTED,false));form.addView(model);space(form,8);form.addView(secret);if(existing.optBoolean("has_api_key"))form.addView(clear);form.addView(text("执行电脑",13,GOLD,true));form.addView(device);form.addView(enabled);form.addView(images);form.addView(toolSupport);form.addView(text("仅在所选模型确实支持时开启对应能力。",12,MUTED,false));
        kind.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener(){public void onNothingSelected(android.widget.AdapterView<?> parent){}public void onItemSelected(android.widget.AdapterView<?> parent,View view,int position,long id){String current=base.getText().toString();if(current.equals("http://127.0.0.1:11434/v1")||current.equals("http://127.0.0.1:1234/v1")){base.setText(position==0?"http://127.0.0.1:11434/v1":position==1?"http://127.0.0.1:1234/v1":"https://api.example.com/v1");}}});
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle(existing.optLong("id")>0?"编辑模型服务":"添加模型服务").setView(scroll).setPositiveButton("保存",null).setNegativeButton("取消",null).create();
        modelDialog=dialog;dialog.setOnDismissListener(d->{secret.setText("");if(modelDialog==dialog)modelDialog=null;});dialog.setOnShowListener(d->dialog.getButton(-1).setOnClickListener(v->{
            if(!state.accepts(ticket)||!state.project.equals(ticket.project)){dialog.dismiss();return;}
            String label=name.getText().toString().trim(),url=base.getText().toString().trim(),modelName=model.getText().toString().trim();if(label.isEmpty()||url.isEmpty()||modelName.isEmpty()){toast("填写服务名称、地址和模型名称");return;}
            JSONObject payload=obj("name",label,"kind",kinds[kind.getSelectedItemPosition()],"base_url",url,"model",modelName,"device_id",devices.optJSONObject(device.getSelectedItemPosition()).optString("device_id"),"enabled",enabled.isChecked(),"supports_images",images.isChecked(),"supports_tools",toolSupport.isChecked());
            try{if(!secret.getText().toString().isEmpty())payload.put("api_key",secret.getText().toString());if(clear.isChecked())payload.put("clear_api_key",true);}catch(JSONException ignored){}
            dialog.getButton(-1).setEnabled(false);modelMutation(existing.optLong("id")>0?"PUT":"POST",existing.optLong("id")>0?"/"+existing.optLong("id"):"",payload,dialog);
        }));showDialog(dialog);
    }
    private void modelMutation(String method,String suffix,JSONObject payload,AlertDialog dialog){
        NativeHubState.Ticket ticket=state.ticket();String path=modelsPath()+suffix;if(!mutations.add(path))return;String auth=cookie,token=csrf;
        network.execute(()->{try{NativeHubApi.Reply reply=NativeHubApi.json(ticket.endpoint,path,method,payload,auth,token);main.post(()->{
            mutations.remove(path);if(destroyed||!state.accepts(ticket))return;if(dialog!=null)dialog.dismiss();toast(reply.body.optString("message",suffix.endsWith("/test")?"测试已交给执行电脑，请刷新查看结果":"模型配置已保存"));if(state.displays(ticket))refreshModels(true);
        });}catch(Exception error){main.post(()->{mutations.remove(path);if(destroyed||!state.accepts(ticket))return;if(dialog!=null)dialog.getButton(-1).setEnabled(true);if(error instanceof NativeHubApi.ApiError&&((NativeHubApi.ApiError)error).status==401)expireLogin();else toast(error instanceof NativeHubApi.ApiError?error.getMessage():"连接中断，保存结果待核对；请刷新服务列表后再操作");});}});
    }
    private void serverSettings(){
        EditText endpoint=input("https://…/hub/",false);endpoint.setText(state.endpoint);
        LinearLayout form=column();form.setPadding(dp(20),dp(8),dp(20),dp(16));form.addView(text("填写完整的 Hub HTTPS 地址。",13,MUTED,false));form.addView(endpoint);ScrollView scroll=dialogScroll(form);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("Hub 连接地址").setView(scroll).setPositiveButton("保存",null).setNegativeButton("取消",null).create();
        dialog.setOnShowListener(d->dialog.getButton(-1).setOnClickListener(v->{String value=HubEndpoint.normalize(endpoint.getText().toString());if(value==null){endpoint.setError("填写完整的 HTTPS 地址");return;}
            saveComposer();HubEndpoint.save(this,value);fixtureBase="";state.identify(value,"","");cookie="";csrf="";username="";dialog.dismiss();renderLogin();refreshSession();
        }));showDialog(dialog);
    }
    @Override public void onBackPressed(){
        if(keyboardOpen){((InputMethodManager)getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(root.getWindowToken(),0);return;}
        saveComposer();rememberPage();if(!state.userId.isEmpty()&&state.back()){store.rememberLocation(state);renderShell();animatePage();refreshCurrent(true);return;}super.onBackPressed();
    }
    private void status(String message,boolean connected){boolean changed=online!=connected;online=connected;connectionText=message==null?"连接暂未完成":message;if(connectionLabel!=null)connectionLabel.setText(connectionText);if(changed&&state.screen==NativeHubState.Screen.CHAT)renderChatMessages(false);else if(changed&&page!=null&&state.screen==NativeHubState.Screen.TASK)renderPage();}
    private void toast(String message){if(!destroyed)Toast.makeText(this,message==null?"暂时未完成，请重试":message,Toast.LENGTH_LONG).show();}
    private LinearLayout column(){LinearLayout view=new LinearLayout(this);view.setOrientation(LinearLayout.VERTICAL);return view;}
    private LinearLayout row(){LinearLayout view=new LinearLayout(this);view.setOrientation(LinearLayout.HORIZONTAL);view.setGravity(Gravity.CENTER_VERTICAL);return view;}
    private TextView text(String value,int size,int color,boolean bold){TextView view=new TextView(this);view.setText(value);view.setTextSize(size);view.setTextColor(color);view.setTypeface(Typeface.create("sans-serif",bold?Typeface.BOLD:Typeface.NORMAL));view.setPadding(0,dp(5),0,dp(5));view.setLineSpacing(dp(2),1.05f);return view;}
    private EditText input(String hint,boolean multiline){EditText view=new EditText(this);view.setTextColor(TEXT);view.setHintTextColor(MUTED);view.setTextSize(16);view.setTypeface(Typeface.create("sans-serif",Typeface.NORMAL));view.setHint(hint);view.setPadding(dp(14),dp(12),dp(14),dp(12));view.setBackground(outline(CARD,16));view.setInputType(InputType.TYPE_CLASS_TEXT|(multiline?InputType.TYPE_TEXT_FLAG_MULTI_LINE:0));view.setSingleLine(!multiline);return view;}
    private Button button(String title,Runnable action){Button view=new Button(this);view.setText(title);view.setAllCaps(false);view.setTextSize(14);view.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));view.setTextColor(GOLD);view.setBackground(new RippleDrawable(ColorStateList.valueOf(0x35d3c2a7),tone(PANEL,14),tone(Color.WHITE,14)));view.setMinHeight(dp(48));view.setMinimumWidth(0);view.setMinWidth(0);view.setPadding(dp(12),dp(6),dp(12),dp(6));final long[] clicked={-1000};view.setOnClickListener(v->{long now=SystemClock.elapsedRealtime();if(now-clicked[0]<350)return;clicked[0]=now;action.run();});return view;}
    private Button iconButton(String label,String description,Runnable action){Button view=button(label,action);view.setContentDescription(description);view.setTextSize(22);view.setLayoutParams(new LinearLayout.LayoutParams(dp(44),dp(44)));return view;}
    private LinearLayout card(LinearLayout parent){LinearLayout card=column();card.setBackground(outline(PANEL,20));card.setPadding(dp(15),dp(12),dp(15),dp(14));LinearLayout.LayoutParams layout=new LinearLayout.LayoutParams(-1,-2);layout.bottomMargin=dp(12);parent.addView(card,layout);return card;}
    private void section(String title,String description){content.addView(text(title,25,TEXT,true));content.addView(text(description,13,MUTED,false));space(content,16);}
    private void empty(String title,String description){LinearLayout box=card(content);box.addView(text(title,18,TEXT,true));box.addView(text(description,14,MUTED,false));}
    private void space(LinearLayout parent,int height){parent.addView(new View(this),new LinearLayout.LayoutParams(1,dp(height)));}
    private GradientDrawable tone(int color,int radius){GradientDrawable draw=new GradientDrawable();draw.setColor(color);draw.setCornerRadius(dp(radius));return draw;}
    private GradientDrawable outline(int color,int radius){GradientDrawable draw=tone(color,radius);draw.setStroke(dp(1),LINE);return draw;}
    private int dp(int value){return Math.round(value*getResources().getDisplayMetrics().density);}
    private static JSONObject obj(Object...values){JSONObject result=new JSONObject();try{for(int i=0;i+1<values.length;i+=2)result.put(String.valueOf(values[i]),values[i+1]);}catch(JSONException ignored){}return result;}
    private static JSONObject copy(JSONObject value){try{return new JSONObject(value.toString());}catch(JSONException error){return new JSONObject();}}
    private static JSONArray copyArray(JSONArray value){try{return new JSONArray(value.toString());}catch(JSONException error){return new JSONArray();}}
    private static JSONArray array(JSONObject value,String key){JSONArray result=value==null?null:value.optJSONArray(key);return result==null?new JSONArray():result;}
    private static long parseLong(String text){try{return Long.parseLong(text);}catch(Exception invalid){return 0;}}
    private static String time(String value){if(value==null||value.isEmpty()||value.equals("null"))return "";if(value.matches("[0-9]+"))return new java.text.SimpleDateFormat("MM-dd HH:mm",java.util.Locale.CHINA).format(new java.util.Date(parseLong(value)*1000));return value.replace('T',' ').substring(0,Math.min(16,value.length()));}
    private static String timeNow(){return new java.text.SimpleDateFormat("HH:mm",java.util.Locale.CHINA).format(new java.util.Date());}
    private static String size(long bytes){return bytes>1024*1024?String.format(java.util.Locale.CHINA,"%.1f MB",bytes/1048576.0):Math.max(1,bytes/1024)+" KB";}
}
