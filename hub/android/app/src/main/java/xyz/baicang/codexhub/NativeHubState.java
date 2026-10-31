package xyz.baicang.codexhub;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;

/** Pure native navigation and callback identity. No URL or HTML is a screen. */
final class NativeHubState {
    enum Screen { CHAT, PROJECTS, AGENTS, TASKS, TOOLBOX, AGENT, TASK, HISTORY, DEVICES, LIBRARY, DOWNLOADS }
    Screen screen = Screen.CHAT;
    String endpoint = "", userId = "", sessionKey = "", project = "";
    long conversation = 0, detailId = 0, authEpoch = 0, navigation = 0;

    void identify(String endpoint, String userId, String sessionKey) {
        if (!this.endpoint.equals(endpoint) || !this.userId.equals(userId) || !this.sessionKey.equals(sessionKey)) {
            authEpoch++; navigation++; project = ""; conversation = 0; detailId = 0; screen = Screen.CHAT;
        }
        this.endpoint = endpoint; this.userId = userId; this.sessionKey = sessionKey;
    }
    void signOut() { identify(endpoint, "", ""); authEpoch++; }
    void open(Screen screen) { this.screen = screen; navigation++; }
    void room(String project, long conversation) {
        this.project = project; this.conversation = Math.max(0, conversation); open(Screen.CHAT);
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
