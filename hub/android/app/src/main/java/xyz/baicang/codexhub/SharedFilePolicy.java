package xyz.baicang.codexhub;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.Locale;

/** Limits are enforced on bytes read, including providers with no reliable size metadata. */
final class SharedFilePolicy {
    static final int MAX_FILES = 10;
    static final long MAX_FILE_BYTES = 50L * 1024 * 1024;
    static final long MAX_CACHE_BYTES = 100L * 1024 * 1024;

    static long copy(InputStream input, OutputStream output, long remainingBytes)
            throws IOException {
        long limit = Math.min(MAX_FILE_BYTES, remainingBytes);
        if (limit < 0) throw new IOException("附件缓存已满");
        byte[] buffer = new byte[64 * 1024];
        long copied = 0;
        while (true) {
            if (Thread.currentThread().isInterrupted()) throw new IOException("已取消导入");
            int count = input.read(buffer, 0, (int) Math.min(buffer.length, limit - copied + 1));
            if (count < 0) return copied;
            if (count == 0) continue;
            if (copied + count > limit) throw new IOException("文件超过导入大小限制");
            output.write(buffer, 0, count);
            copied += count;
        }
    }

    static String safeName(String value) {
        String name = value == null ? "" : value.replaceAll("[\\p{Cntrl}/\\\\]", "_").trim();
        if (name.isEmpty() || ".".equals(name) || "..".equals(name)) name = "attachment";
        if (name.length() > 180) {
            int end = Character.isHighSurrogate(name.charAt(179)) ? 179 : 180;
            name = name.substring(0, end);
        }
        return name;
    }

    static String safeType(String value) {
        String type = value == null ? "" : value.trim().toLowerCase(Locale.ROOT);
        return type.matches("[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+")
                ? type : "application/octet-stream";
    }

    private SharedFilePolicy() { }
}
