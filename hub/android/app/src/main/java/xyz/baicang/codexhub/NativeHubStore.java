package xyz.baicang.codexhub;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.Comparator;
import android.util.AtomicFile;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Private native data. Drafts and immutable send envelopes are never evicted as UI cache. */
final class NativeHubStore {
    private final SharedPreferences data;
    private final File cacheRoot;
    private final java.util.LinkedHashMap<String,JSONObject> cacheMemory=new java.util.LinkedHashMap<String,JSONObject>(32,.75f,true){
        protected boolean removeEldestEntry(java.util.Map.Entry<String,JSONObject> entry){return size()>32;}
    };
    private final ExecutorService disk=Executors.newSingleThreadExecutor();
    NativeHubStore(Context context) { data=context.getSharedPreferences("hub_native_v1", Context.MODE_PRIVATE);cacheRoot=new File(context.getFilesDir(),"native-page-cache"); }
    synchronized JSONObject read(String key) {
        try { return new JSONObject(data.getString(key, "{}")); }
        catch (JSONException invalid) { return new JSONObject(); }
    }
    synchronized void put(String key, JSONObject value) { data.edit().putString(key,value.toString()).apply(); }
    synchronized void remove(String key) { data.edit().remove(key).apply(); }
    synchronized boolean putDurable(String key,JSONObject value){return data.edit().putString(key,value.toString()).commit();}
    synchronized boolean referenced(String uri){for(Object value:data.getAll().values())if(value instanceof String&&((String)value).contains(uri))return true;return false;}
    synchronized JSONObject cache(String user, String key) { return cacheMemory.get(user+":"+key); }
    synchronized void cache(String user, String key, JSONObject value) {
        cacheMemory.put(user+":"+key,value);
        disk.execute(()->{
            try {
                byte[] bytes=new JSONObject().put("key",key).put("saved_at",System.currentTimeMillis()).put("data",value).toString().getBytes(StandardCharsets.UTF_8);
                if(bytes.length>1500000)return;
                File directory=new File(cacheRoot,user);if(!directory.isDirectory()&&!directory.mkdirs())return;
                AtomicFile file=new AtomicFile(new File(directory,NativeHubState.digest(key)+".json"));FileOutputStream output=null;
                try{output=file.startWrite();output.write(bytes);file.finishWrite(output);}catch(Exception failure){if(output!=null)file.failWrite(output);return;}
                File[] stored=directory.listFiles((dir,name)->name.endsWith(".json"));
                if(stored!=null){java.util.Arrays.sort(stored,Comparator.comparingLong(File::lastModified));for(int i=0;i<stored.length-20;i++)new AtomicFile(stored[i]).delete();}
            } catch(Exception ignored) { }
        });
    }
    void warm(String user,Runnable completion) {
        disk.execute(()->{
            File[] stored=new File(cacheRoot,user).listFiles((dir,name)->name.endsWith(".json"));
            if(stored!=null)for(File file:stored)if(file.length()<=1500000)try(FileInputStream input=new AtomicFile(file).openRead();ByteArrayOutputStream output=new ByteArrayOutputStream()){
                byte[] buffer=new byte[8192];int n;while((n=input.read(buffer))!=-1){if(output.size()+n>1500000)throw new java.io.IOException("bounded cache");output.write(buffer,0,n);}
                JSONObject record=new JSONObject(output.toString("UTF-8"));JSONObject value=record.optJSONObject("data");String key=record.optString("key");
                if(value!=null&&!key.isEmpty())synchronized(this){cacheMemory.putIfAbsent(user+":"+key,value);}
            }catch(Exception ignored){}
            completion.run();
        });
    }
    void close(){disk.shutdown();}
    synchronized JSONObject draft(String scope) { return read("draft:"+scope); }
    synchronized void draft(String scope, String text, JSONArray files, long agent) {
        try { put("draft:"+scope,new JSONObject().put("text",text).put("files",files).put("agent",agent)); }
        catch (JSONException ignored) { }
    }
    synchronized JSONArray jobs(String userScope) {
        JSONArray result=new JSONArray();
        for(String key:data.getAll().keySet()) if(key.startsWith("outbox:"+userScope+":")) result.put(read(key));
        return result;
    }
    synchronized boolean job(String userScope, JSONObject value) {
        return data.edit().putString("outbox:"+userScope+":"+value.optString("request_key"),value.toString()).commit();
    }
    synchronized void removeJob(String userScope, String requestKey) { remove("outbox:"+userScope+":"+requestKey); }
    synchronized void rememberLocation(NativeHubState state) {
        try { put("location:"+state.userScope(),new JSONObject().put("project",state.project).put("conversation",state.conversation));put("room:"+state.userScope()+":"+state.project,new JSONObject().put("conversation",state.conversation)); }
        catch(JSONException ignored) { }
    }
}
