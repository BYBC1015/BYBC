package xyz.baicang.codexhub;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** JSON transport only. Never follows a login/HTML redirect and never retries POST. */
final class NativeHubApi {
    static boolean fixtureBase(String base) {
        if(!BuildConfig.DEBUG||base==null)return false;
        try {URI u=URI.create(base);return "http".equals(u.getScheme())&&("10.0.2.2".equals(u.getHost())||"127.0.0.1".equals(u.getHost()))
                &&u.getPort()>0&&u.getPort()<65536&&"/hub/".equals(u.getPath())&&u.getRawUserInfo()==null&&u.getRawQuery()==null&&u.getRawFragment()==null;
        }catch(RuntimeException invalid){return false;}
    }
    static boolean internal(String base,String url) {
        if(HubNavigationPolicy.internal(base,url))return true;
        if(!fixtureBase(base))return false;
        try{URI b=URI.create(base),u=URI.create(url);return u.getScheme().equals(b.getScheme())&&b.getHost().equals(u.getHost())&&b.getPort()==u.getPort()
                &&u.getRawUserInfo()==null&&u.getPath().startsWith(b.getPath())&&!u.getPath().contains("\\")&&!u.getPath().matches(".*(?:/\\.\\.?)(?:/|$).*");
        }catch(RuntimeException invalid){return false;}
    }
    static HubFileDownload.Result file(String base,String value,String cookies,File directory,long limit)throws IOException {
        String url;
        try{url=URI.create(base).resolve(value).toString();}catch(RuntimeException invalid){throw new IOException("文件地址无效");}
        if(!internal(base,url))throw new IOException("文件地址不属于当前 Hub");
        String route=URI.create(url).getPath().substring(URI.create(base).getPath().length());
        if(!route.matches("projects/[^/]+/attachments/[1-9][0-9]*/?")&&!route.matches("downloads/[a-z][a-z0-9_-]*/?"))throw new IOException("文件地址无效");
        if(!directory.isDirectory()&&!directory.mkdirs())throw new IOException("无法准备本机文件空间");
        HttpURLConnection c=(HttpURLConnection)URI.create(url).toURL().openConnection();File temporary=null;boolean complete=false;
        try{
            c.setInstanceFollowRedirects(false);c.setConnectTimeout(12000);c.setReadTimeout(30000);c.setRequestProperty("Cookie",cookies);c.setRequestProperty("Accept-Encoding","identity");
            int status=c.getResponseCode();if(status!=200)throw new ApiError(status,"文件连接未完成，请重试");
            String type=SharedFilePolicy.safeType(c.getContentType()==null?"":c.getContentType().split(";",2)[0]);
            String disposition=c.getHeaderField("Content-Disposition");
            if(type.equals("text/html")&&(disposition==null||!disposition.toLowerCase().startsWith("attachment")))throw new IOException("收到登录页面，请重新连接");
            long expected=c.getContentLengthLong();if(expected>limit)throw new IOException("文件较大，请在电脑上下载");
            temporary=File.createTempFile("native-",".part",directory);long total=0;
            try(InputStream in=c.getInputStream();java.io.FileOutputStream out=new java.io.FileOutputStream(temporary)){
                byte[] buffer=new byte[65536];int n;while((n=in.read(buffer))!=-1){if(Thread.currentThread().isInterrupted())throw new IOException("下载已取消");total+=n;if(total>limit)throw new IOException("文件超过本机下载限制");out.write(buffer,0,n);}out.getFD().sync();
            }
            if(expected>=0&&expected!=total)throw new IOException("文件未完整下载，请重试");
            complete=true;return new HubFileDownload.Result(temporary,HubFileDownload.filename(disposition,URI.create(url)),type,total);
        }finally{c.disconnect();if(!complete&&temporary!=null)temporary.delete();}
    }
    static final int MAX_JSON_BYTES=8*1024*1024;
    static final class ApiError extends IOException {
        final int status;
        ApiError(int status,String message) { super(message);this.status=status; }
    }
    static final class Reply {
        final JSONObject body; final List<String> cookies;
        Reply(JSONObject body,List<String> cookies) { this.body=body;this.cookies=cookies; }
    }
    static final class Upload {
        final File file;final String name,type;
        Upload(File file,String name,String type) {this.file=file;this.name=SharedFilePolicy.safeName(name);this.type=SharedFilePolicy.safeType(type);}
    }
    static Reply json(String base,String path,String method,JSONObject body,String cookies,String csrf) throws IOException {
        HttpURLConnection connection=open(base,path,method,cookies,csrf);
        try {
            if(body!=null) {
                byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8);
                connection.setDoOutput(true);connection.setRequestProperty("Content-Type","application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(bytes.length);
                try(OutputStream output=connection.getOutputStream()) {output.write(bytes);}
            }
            return response(connection);
        } finally {connection.disconnect();}
    }
    static Reply send(String base,String path,JSONObject job,List<Upload> files,String cookies,String csrf) throws IOException {
        String boundary="HubNative"+job.optString("request_key").replace("-","");
        HttpURLConnection connection=open(base,path,"POST",cookies,csrf);
        try {
            connection.setDoOutput(true);connection.setChunkedStreamingMode(64*1024);
            connection.setRequestProperty("Content-Type","multipart/form-data; boundary="+boundary);
            try(OutputStream output=connection.getOutputStream()) {
                field(output,boundary,"description",job.optString("description"));
                field(output,boundary,"request_key",job.optString("request_key"));
                field(output,boundary,"conversation_id",job.optLong("conversation_id")>0?String.valueOf(job.optLong("conversation_id")):"");
                if(job.optLong("agent_id")>0) field(output,boundary,"agent_ids",String.valueOf(job.optLong("agent_id")));
                field(output,boundary,"_csrf",csrf);
                long total=0;
                for(Upload file:files) {
                    if(!file.file.isFile()||file.file.length()>SharedFilePolicy.MAX_FILE_BYTES) throw new ApiError(0,"附件已不可读，请保留这条消息并重新选择文件");
                    write(output,"--"+boundary+"\r\nContent-Disposition: form-data; name=\"attachments\"; filename=\""+file.name.replace("\"","_")+"\"\r\nContent-Type: "+file.type+"\r\n\r\n");
                    try(InputStream input=new FileInputStream(file.file)) {
                        byte[] buffer=new byte[64*1024];int count;
                        while((count=input.read(buffer))!=-1) {
                            total+=count;if(total>SharedFilePolicy.MAX_CACHE_BYTES)throw new ApiError(0,"附件总大小超过限制");
                            output.write(buffer,0,count);
                        }
                    }
                    write(output,"\r\n");
                }
                write(output,"--"+boundary+"--\r\n");
            }
            return response(connection);
        } finally {connection.disconnect();}
    }
    private static HttpURLConnection open(String base,String path,String method,String cookies,String csrf) throws IOException {
        String url=base+path;
        if(!(path.startsWith("api/mobile/")||path.equals("api/releases")&&method.equals("GET"))
                ||!internal(base,url)) throw new IOException("请求地址不属于当前 Hub");
        HttpURLConnection connection=(HttpURLConnection)URI.create(url).toURL().openConnection();
        connection.setConnectTimeout(12000);connection.setReadTimeout("POST".equals(method)?90000:20000);
        connection.setInstanceFollowRedirects(false);connection.setRequestMethod(method);
        connection.setRequestProperty("Accept","application/json");connection.setRequestProperty("Cache-Control","no-store");
        connection.setRequestProperty("User-Agent","BYBCHubAndroidNative/0.2.0");
        if(cookies!=null&&!cookies.isEmpty())connection.setRequestProperty("Cookie",cookies);
        if(csrf!=null&&!csrf.isEmpty())connection.setRequestProperty("X-CSRF-Token",csrf);
        return connection;
    }
    private static Reply response(HttpURLConnection connection) throws IOException {
        int status=connection.getResponseCode();
        if(status>=300&&status<400)throw new ApiError(status,"服务端返回旧页面跳转，请更新 Hub 服务后重试");
        String type=connection.getContentType();
        if(type==null||!type.toLowerCase().contains("application/json"))throw new ApiError(status,"尚未连接到原生客户端接口，本机页面和草稿仍然保留");
        InputStream source=status<400?connection.getInputStream():connection.getErrorStream();
        if(source==null)throw new ApiError(status,"连接暂时未完成");
        JSONObject body;
        try(InputStream input=source;ByteArrayOutputStream output=new ByteArrayOutputStream()) {
            byte[] buffer=new byte[8192];int count;
            while((count=input.read(buffer))!=-1) {if(output.size()+count>MAX_JSON_BYTES)throw new ApiError(status,"返回记录过大，请稍后重试");output.write(buffer,0,count);}
            try {body=new JSONObject(output.toString("UTF-8"));}catch(JSONException invalid){throw new ApiError(status,"接口数据暂时无法读取");}
        }
        if(status>=400)throw new ApiError(status,body.optString("detail",status==401?"请重新登录":"请求未完成，请重试"));
        ArrayList<String> cookies=new ArrayList<>();
        for(Map.Entry<String,List<String>> header:connection.getHeaderFields().entrySet())
            if("Set-Cookie".equalsIgnoreCase(header.getKey()))cookies.addAll(header.getValue());
        return new Reply(body,cookies);
    }
    private static void field(OutputStream output,String boundary,String name,String value)throws IOException {
        write(output,"--"+boundary+"\r\nContent-Disposition: form-data; name=\""+name+"\"\r\n\r\n"+value+"\r\n");
    }
    private static void write(OutputStream output,String value)throws IOException {output.write(value.getBytes(StandardCharsets.UTF_8));}
}
