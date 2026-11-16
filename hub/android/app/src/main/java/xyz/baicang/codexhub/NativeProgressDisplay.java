package xyz.baicang.codexhub;

/** Labels describe confirmed contact and received reports, never inferred model activity. */
final class NativeProgressDisplay {
    private NativeProgressDisplay() { }
    static String heading(boolean online, boolean contact, boolean quiet) {
        if(!online)return "连接暂不可用，进展待核对";
        if(!contact)return "执行状态待核对";
        return quiet?"进展待更新":"执行连接正常";
    }
    static String notice(boolean online, boolean contact, boolean quiet) {
        if(!online)return "当前显示本机记录，恢复连接后再核对执行状态。";
        if(!contact)return "任务联系已过期，等待核对。已收到的进展会保留。";
        return quiet?"执行电脑仍在线，暂未收到新进度":"";
    }
    static boolean freshContact(boolean online, boolean contact, boolean quiet) {
        return online&&contact&&!quiet;
    }
}
