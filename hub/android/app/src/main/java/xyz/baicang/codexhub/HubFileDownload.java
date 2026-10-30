package xyz.baicang.codexhub;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Streams authenticated Hub files without passing session cookies to a browser or another host. */
final class HubFileDownload {
    static final long MAX_BYTES = 1024L * 1024L * 1024L;
    interface Connections { HttpURLConnection open(URI uri) throws IOException; }
    static final class Result {
        final File file; final String name; final String type; final long bytes;
        Result(File file, String name, String type, long bytes) {
            this.file = file; this.name = name; this.type = type; this.bytes = bytes;
        }
    }

    static Result fetch(String base, String url, String cookie, String userAgent, File directory,
                        Connections connections) throws IOException {
        if (!HubNavigationPolicy.download(base, url)) throw new IOException("文件地址不属于当前 Hub");
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法创建下载缓存");
        URI target = URI.create(url);
        File temporary = null;
        boolean complete = false;
        try {
            for (int redirect = 0; redirect <= 5; redirect++) {
                if (Thread.currentThread().isInterrupted()) throw new IOException("下载已取消");
                if (!HubNavigationPolicy.download(base, target.toString())) {
                    throw new IOException("文件需要重新登录，或下载地址已失效");
                }
                HttpURLConnection connection = connections.open(target);
                connection.setConnectTimeout(15000);
                connection.setReadTimeout(30000);
                connection.setInstanceFollowRedirects(false);
                connection.setRequestProperty("Accept-Encoding", "identity");
                if (cookie != null && !cookie.isEmpty()) connection.setRequestProperty("Cookie", cookie);
                if (userAgent != null && !userAgent.isEmpty()) connection.setRequestProperty("User-Agent", userAgent);
                try {
                    int status = connection.getResponseCode();
                    if (status >= 300 && status < 400) {
                        String location = connection.getHeaderField("Location");
                        if (location == null) throw new IOException("下载跳转缺少地址");
                        try { target = target.resolve(location); }
                        catch (IllegalArgumentException error) { throw new IOException("下载地址无效"); }
                        continue;
                    }
                    if (status != 200) throw new IOException("下载未完成（HTTP " + status + "），请稍后重试");
                    String disposition = connection.getHeaderField("Content-Disposition");
                    String type = connection.getContentType();
                    type = SharedFilePolicy.safeType(type == null ? "" : type.split(";", 2)[0]);
                    if (type.equals("text/html") && (disposition == null
                            || !disposition.toLowerCase(Locale.ROOT).startsWith("attachment"))) {
                        throw new IOException("收到登录页面，请先重新登录再下载");
                    }
                    long expected = -1L;
                    try { expected = Long.parseLong(connection.getHeaderField("Content-Length")); }
                    catch (NumberFormatException ignored) { }
                    if (expected > MAX_BYTES) throw new IOException("文件超过 1 GiB，请使用电脑下载");
                    temporary = File.createTempFile("hub-download-", ".part", directory);
                    long count = 0;
                    try (InputStream input = connection.getInputStream(); FileOutputStream output = new FileOutputStream(temporary)) {
                        byte[] buffer = new byte[64 * 1024];
                        int read;
                        while ((read = input.read(buffer)) != -1) {
                            if (Thread.currentThread().isInterrupted()) throw new IOException("下载已取消");
                            count += read;
                            if (count > MAX_BYTES) throw new IOException("文件超过 1 GiB，请使用电脑下载");
                            output.write(buffer, 0, read);
                        }
                        output.getFD().sync();
                    }
                    if (expected >= 0 && count != expected) throw new IOException("下载内容不完整，请重试");
                    complete = true;
                    return new Result(temporary, filename(disposition, target), type, count);
                } finally { connection.disconnect(); }
            }
            throw new IOException("下载跳转次数过多");
        } finally {
            if (!complete && temporary != null) temporary.delete();
        }
    }

    static String filename(String disposition, URI target) {
        String name = null;
        if (disposition != null) {
            Matcher utf8 = Pattern.compile("(?i)filename\\*=UTF-8''([^;]+)").matcher(disposition);
            if (utf8.find()) {
                try { name = URLDecoder.decode(utf8.group(1).replace("+", "%2B"), StandardCharsets.UTF_8.name()); }
                catch (Exception ignored) { }
            }
            if (name == null) {
                Matcher plain = Pattern.compile("(?i)filename=\"([^\"]*)\"|filename=([^;]+)").matcher(disposition);
                if (plain.find()) name = plain.group(1) != null ? plain.group(1) : plain.group(2);
            }
        }
        if (name == null || name.trim().isEmpty()) name = target.getPath().substring(target.getPath().lastIndexOf('/') + 1);
        return SharedFilePolicy.safeName(name);
    }
    private HubFileDownload() { }
}
