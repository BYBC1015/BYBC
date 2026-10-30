package xyz.baicang.codexhub;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.OpenableColumns;
import android.webkit.MimeTypeMap;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;

final class SharedAttachmentImporter {
    private final Context context;

    SharedAttachmentImporter(Context context) { this.context = context.getApplicationContext(); }

    boolean isExternalContent(Uri uri) {
        return uri != null && "content".equals(uri.getScheme())
                && uri.getAuthority() != null && !uri.getAuthority().isEmpty()
                && uri.getUserInfo() == null && uri.getPort() == -1
                && !uri.getAuthority().startsWith(context.getPackageName());
    }

    Uri copy(Uri source, String fallbackType) throws IOException {
        if (!isExternalContent(source)) throw new IOException("文件来源无效");
        File directory = SharedAttachmentProvider.directory(context);
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("无法创建附件缓存");
        long usedBytes = 0L;
        File[] stored = directory.listFiles();
        if (stored != null) for (File file : stored) usedBytes += file.length();
        long remaining = Math.max(0L, SharedFilePolicy.MAX_CACHE_BYTES - usedBytes);
        if (remaining == 0L) throw new IOException("附件缓存已满");

        ContentResolver resolver = context.getContentResolver();
        String name = source.getLastPathSegment();
        long declaredSize = -1L;
        try (Cursor cursor = resolver.query(source,
                new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                int sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE);
                if (nameIndex >= 0 && !cursor.isNull(nameIndex)) name = cursor.getString(nameIndex);
                if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) declaredSize = cursor.getLong(sizeIndex);
            }
        } catch (SecurityException exception) {
            throw new IOException("来源应用没有授予文件读取权限");
        } catch (RuntimeException ignored) {
            // Some providers have no metadata; streaming still enforces both byte limits.
        }
        if (declaredSize > Math.min(SharedFilePolicy.MAX_FILE_BYTES, remaining)) {
            throw new IOException("文件超过导入大小限制");
        }
        name = SharedFilePolicy.safeName(name);
        String type = fallbackType;
        try {
            String detected = resolver.getType(source);
            if (detected != null) type = detected;
        } catch (RuntimeException ignored) { }
        type = SharedFilePolicy.safeType(type);
        if ("application/octet-stream".equals(type) && name.contains(".")) {
            String extension = name.substring(name.lastIndexOf('.') + 1).toLowerCase(Locale.ROOT);
            type = SharedFilePolicy.safeType(MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension));
        }
        String id = UUID.randomUUID().toString().replace("-", "");
        File destination = new File(directory, id + ".data");
        try (InputStream input = resolver.openInputStream(source);
             FileOutputStream output = new FileOutputStream(destination)) {
            if (input == null) throw new IOException("来源文件不可读取");
            SharedFilePolicy.copy(input, output, remaining);
        } catch (IOException | RuntimeException exception) {
            destination.delete();
            throw exception;
        }
        return new Uri.Builder().scheme("content").authority(SharedAttachmentProvider.authority(context))
                .appendPath(id).appendPath(name).appendQueryParameter("type", type).build();
    }

    boolean exists(Uri uri) {
        try { SharedAttachmentProvider.file(context, uri); return true; }
        catch (IOException exception) { return false; }
    }

    void remove(Uri uri) {
        try { SharedAttachmentProvider.file(context, uri).delete(); }
        catch (IOException ignored) { }
    }

    /** Only this feature's private cache; retained handles cover activity recreation. */
    void removeUnreferenced(List<Uri> retained) {
        Set<String> keep = new HashSet<>();
        for (Uri uri : retained) {
            try { keep.add(SharedAttachmentProvider.file(context, uri).getName()); }
            catch (IOException ignored) { }
        }
        File[] files = SharedAttachmentProvider.directory(context).listFiles();
        if (files != null) for (File file : files) {
            if (file.isFile() && !keep.contains(file.getName())) file.delete();
        }
    }
}
