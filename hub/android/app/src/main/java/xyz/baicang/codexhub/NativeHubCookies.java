package xyz.baicang.codexhub;

import java.net.HttpCookie;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Native-only private cookie jar; migration never opens the legacy browser UI. */
final class NativeHubCookies {
    static String update(String current,List<String> headers) {
        LinkedHashMap<String,String> values=new LinkedHashMap<>();
        if(current!=null)for(String part:current.split(";")) {
            String[] pair=part.trim().split("=",2);
            if(pair.length==2&&allowed(pair[0]))values.put(pair[0],pair[1]);
        }
        for(String header:headers)try {
            for(HttpCookie cookie:HttpCookie.parse(header))if(allowed(cookie.getName())) {
                if(cookie.hasExpired()||cookie.getValue().isEmpty()) values.remove(cookie.getName());
                else values.put(cookie.getName(),cookie.getValue());
            }
        } catch(IllegalArgumentException invalid){ }
        StringBuilder result=new StringBuilder();
        for(Map.Entry<String,String> entry:values.entrySet()) {if(result.length()>0)result.append("; ");result.append(entry.getKey()).append('=').append(entry.getValue());}
        return result.toString();
    }
    private static boolean allowed(String name) {return name.equals("codex_hub_session")||name.equals("codex_hub_csrf");}
}
