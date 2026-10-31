package xyz.baicang.codexhub;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;
import java.util.List;

/** Private read-only handles for native attachments and existing imported files. */
public final class SharedAttachmentProvider extends ContentProvider {
    static String authority(Context context) {
        return context.getPackageName() + ".sharedattachments";
    }

    static File directory(Context context) {
        return new File(context.getFilesDir(), "native-attachments");
    }

    static File file(Context context, Uri uri) throws FileNotFoundException {
        List<String> parts = uri == null ? java.util.Collections.emptyList() : uri.getPathSegments();
        if (uri == null || !"content".equals(uri.getScheme())
                || !authority(context).equals(uri.getAuthority()) || parts.size() != 2
                || !parts.get(0).matches("[a-f0-9]{32}")) {
            throw new FileNotFoundException("Invalid attachment handle");
        }
        File file = new File(directory(context), parts.get(0) + ".data");
        if (!file.isFile()) file = new File(new File(context.getCacheDir(), "shared-attachments"), parts.get(0) + ".data");
        if (!file.isFile()) throw new FileNotFoundException("Attachment has expired");
        return file;
    }

    @Override public boolean onCreate() { return true; }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        try {
            File file = file(getContext(), uri);
            String[] columns = projection == null
                    ? new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE} : projection;
            MatrixCursor cursor = new MatrixCursor(columns, 1);
            Object[] row = new Object[columns.length];
            for (int index = 0; index < columns.length; index++) {
                if (OpenableColumns.DISPLAY_NAME.equals(columns[index])) {
                    row[index] = SharedFilePolicy.safeName(uri.getLastPathSegment());
                } else if (OpenableColumns.SIZE.equals(columns[index])) {
                    row[index] = file.length();
                }
            }
            cursor.addRow(row);
            return cursor;
        } catch (FileNotFoundException exception) {
            return null;
        }
    }

    @Override public String getType(Uri uri) {
        return SharedFilePolicy.safeType(uri.getQueryParameter("type"));
    }

    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"r".equals(mode)) throw new FileNotFoundException("Read only");
        return ParcelFileDescriptor.open(file(getContext(), uri), ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("Read only");
    }
    @Override public int delete(Uri uri, String selection, String[] args) {
        throw new UnsupportedOperationException("Read only");
    }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) {
        throw new UnsupportedOperationException("Read only");
    }
}
