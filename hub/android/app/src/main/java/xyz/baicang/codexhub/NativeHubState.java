package xyz.baicang.codexhub;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;
import java.util.ArrayDeque;

/** Pure native navigation and callback identity. No URL or HTML is a screen. */
final class NativeHubState {
    enum Screen { CHAT, PROJECTS, AGENTS, TASKS, TOOLBOX, AGENT, TASK, HISTORY, DEVICES, LIBRARY, DOWNLOADS, SETTINGS, MODELS }
    Screen screen = Screen.CHAT;
    String endpoint = "", userId = "", sessionKey = "", project = "";
    long conversation = 0, detailId = 0, authEpoch = 0, navigation = 0;
    private final ArrayDeque<Route> history = new ArrayDeque<>();

    void identify(String endpoint, String userId, String sessionKey) {
        if (!this.endpoint.equals(endpoint) || !this.userId.equals(userId) || !this.sessionKey.equals(sessionKey)) {
            authEpoch++; navigation++; project = ""; conversation = 0; detailId = 0; screen = Screen.CHAT;
            history.clear();
        }
        this.endpoint = endpoint; this.userId = userId; this.sessionKey = sessionKey;
    }
    void signOut() { identify(endpoint, "", ""); authEpoch++; }
    boolean open(Screen screen) { return go(new Route(screen,project,conversation,0)); }
    boolean detail(Screen screen,long id) { return go(new Route(screen,project,conversation,id)); }
    void room(String project, long conversation) {
        go(new Route(Screen.CHAT,project,Math.max(0,conversation),0));
    }
    private Route route() { return new Route(screen,project,conversation,detailId); }
    String routeKey() { return route().encode(); }
    private boolean go(Route next) {
        if(routeKey().equals(next.encode()))return false;
        history.addLast(route());while(history.size()>24)history.removeFirst();
        apply(next);return true;
    }
    private void apply(Route value) {screen=value.screen;project=value.project;conversation=value.conversation;detailId=value.detail;navigation++;}
    boolean back() {if(history.isEmpty())return false;apply(history.removeLast());return true;}
    void resolveConversation(String project,long previous,long confirmed) {
        if(confirmed<=0)return;ArrayDeque<Route> resolved=new ArrayDeque<>();
        for(Route value:history)resolved.addLast(value.project.equals(project)&&value.conversation==previous
                ?new Route(value.screen,value.project,confirmed,value.detail):value);
        history.clear();history.addAll(resolved);
        if(this.project.equals(project)&&conversation==previous){conversation=confirmed;navigation++;}
    }
    String saveNavigation() {
        StringBuilder saved=new StringBuilder();for(Route value:history)saved.append(value.encode()).append('\n');
        return saved.append(routeKey()).toString();
    }
    void restoreNavigation(String saved) {
        if(saved==null||saved.length()>16384)return;
        try {
            String[] entries=saved.split("\n");if(entries.length==0||entries.length>25)return;
            ArrayDeque<Route> restored=new ArrayDeque<>();for(String entry:entries)restored.addLast(Route.decode(entry));
            Route current=restored.removeLast();history.clear();history.addAll(restored);apply(current);
        }catch(RuntimeException ignored) { }
    }
    private static final class Route {
        final Screen screen;final String project;final long conversation,detail;
        Route(Screen screen,String project,long conversation,long detail){this.screen=screen;this.project=project;this.conversation=conversation;this.detail=detail;}
        String encode(){try{return screen.name()+"|"+java.net.URLEncoder.encode(project,"UTF-8")+"|"+conversation+"|"+detail;}catch(Exception impossible){throw new IllegalStateException(impossible);}}
        static Route decode(String value){
            try{String[] parts=value.split("\\|",-1);if(parts.length!=4)throw new IllegalArgumentException();long room=Long.parseLong(parts[2]),id=Long.parseLong(parts[3]);if(room<0||id<0)throw new IllegalArgumentException();return new Route(Screen.valueOf(parts[0]),java.net.URLDecoder.decode(parts[1],"UTF-8"),room,id);}
            catch(Exception invalid){throw new IllegalArgumentException(invalid);}
        }
    }
    Ticket ticket() { return new Ticket(authEpoch, navigation, endpoint, userId, sessionKey, project, conversation); }
    boolean accepts(Ticket ticket) {
        return ticket != null && ticket.authEpoch == authEpoch && ticket.endpoint.equals(endpoint)
                && ticket.user.equals(userId) && ticket.session.equals(sessionKey);
    }
    boolean displays(Ticket ticket) {
        return accepts(ticket) && ticket.navigation == navigation && ticket.project.equals(project)
                && ticket.conversation == conversation;
    }
    String userScope() { return userScope(endpoint, userId); }
    String cacheScope() { return digest(endpoint + "\n" + userId + "\n" + sessionKey); }
    String roomScope() { return roomScope(endpoint, userId, project, conversation); }
    static String userScope(String endpoint, String user) { return digest(endpoint + "\n" + user); }
    static String roomScope(String endpoint, String user, String project, long conversation) {
        return digest(endpoint + "\n" + user + "\n" + project + "\n" + Math.max(0, conversation));
    }
    static String requestKey() { return UUID.randomUUID().toString(); }
    static boolean validRequestKey(String value) {
        try { return value != null && UUID.fromString(value).toString().equals(value); }
        catch (IllegalArgumentException invalid) { return false; }
    }
    static String digest(String value) {
        try {
            StringBuilder result = new StringBuilder();
            for (byte item : MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)))
                result.append(String.format("%02x", item & 255));
            return result.toString();
        } catch (Exception impossible) { throw new IllegalStateException(impossible); }
    }
    static final class Ticket {
        final long authEpoch, navigation, conversation;
        final String endpoint, user, session, project;
        Ticket(long authEpoch, long navigation, String endpoint, String user, String session, String project, long conversation) {
            this.authEpoch=authEpoch; this.navigation=navigation; this.endpoint=endpoint; this.user=user;
            this.session=session; this.project=project; this.conversation=conversation;
        }
        String userScope() { return NativeHubState.userScope(endpoint, user); }
        String cacheScope() { return digest(endpoint + "\n" + user + "\n" + session); }
        String roomScope() { return NativeHubState.roomScope(endpoint, user, project, conversation); }
    }
}
