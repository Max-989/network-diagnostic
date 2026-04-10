# -*- coding: utf-8 -*-
"""Native Messaging Host - 网络诊断+修复，浏览器插件自动唤起"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def read_msg():
    """Read a native messaging message from stdin."""
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) == 0:
        sys.exit(0)
    length = int.from_bytes(raw_length, 'little')
    data = sys.stdin.buffer.read(length)
    return json.loads(data.decode('utf-8'))

def send_msg(msg):
    """Send a native messaging message to stdout."""
    data = json.dumps(msg, ensure_ascii=False).encode('utf-8')
    sys.stdout.buffer.write(len(data).to_bytes(4, 'little') + data)
    sys.stdout.buffer.flush()

def run_cmd(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=True)
        out = r.stdout.decode('gbk', errors='replace') + r.stderr.decode('gbk', errors='replace')
        return out.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return f'[TIMEOUT] {timeout}s', -1
    except Exception as e:
        return f'[ERROR] {e}', -1

def run_ps(cmd, timeout=15):
    try:
        r = subprocess.run(['powershell', '-Command', cmd], capture_output=True, timeout=timeout)
        out = r.stdout.decode('utf-8', errors='replace') + r.stderr.decode('utf-8', errors='replace')
        return out.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return f'[TIMEOUT] {timeout}s', -1
    except Exception as e:
        return f'[ERROR] {e}', -1

# ---- DNS ----
def _resolve_dns(domain, dns_server=None, timeout=3):
    try:
        t0 = time.time()
        if dns_server:
            import struct
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            txid = os.urandom(2)
            query = txid + b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
            for part in domain.split('.'):
                query += bytes([len(part)]) + part.encode()
            query += b'\x00\x00\x01'
            sock.sendto(query, (dns_server, 53))
            data, _ = sock.recvfrom(1024)
            sock.close()
            ips = re.findall(rb'\xc0\x0c\x00\x01.{6}((?:\d{1,3}\.){3}\d{1,3})', data)
            ips = [ip.decode() for ip in ips if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip.decode())]
            cost = int((time.time() - t0) * 1000)
            return {'status': 'ok', 'ips': ips, 'cost_ms': cost} if ips else {'status': 'error', 'detail': '无结果', 'cost_ms': cost}
        else:
            addrs = socket.getaddrinfo(domain, None, socket.AF_INET)
            cost = int((time.time() - t0) * 1000)
            unique = list(set(a[4][0] for a in addrs))
            return {'status': 'ok', 'ips': unique, 'cost_ms': cost} if unique else {'status': 'error', 'detail': '无结果', 'cost_ms': cost}
    except Exception as e:
        return {'status': 'error', 'detail': str(e)[:80], 'cost_ms': 0}

def check_dns():
    targets = [('baidu.com',None,'默认'),('qq.com',None,'默认'),('baidu.com','223.5.5.5','阿里'),('qq.com','223.5.5.5','阿里'),('baidu.com','119.29.29.29','腾讯'),('qq.com','119.29.29.29','腾讯')]
    def do_one(d, dns, lbl):
        r = _resolve_dns(d, dns)
        return {'label':f'{d}({lbl})','status':r['status'],'detail':', '.join(r.get('ips',[])) if r['status']=='ok' else r.get('detail',''),'cost_ms':r.get('cost_ms',0)}
    results = []
    with ThreadPoolExecutor(6) as p:
        results = [f.result() for f in [p.submit(do_one,d,dns,l) for d,dns,l in targets]]
    ok = all(r['status']=='ok' for r in results)
    return {'name':'DNS检测','status':'ok' if ok else 'warn','items':results,'total_cost_ms':max(r['cost_ms'] for r in results)}

# ---- Gateway ----
def check_gateway():
    out, _ = run_cmd('route print -4 0.0.0.0', 5)
    gws = [l.strip().split()[2] for l in out.split('\n') if l.strip().split()[:1]==['0.0.0.0'] and len(l.strip().split())>=5]
    results = []
    gw_str = ', '.join(gws) if gws else 'unknown'
    results.append({'label':'默认网关','status':'ok' if gws else 'error','detail':gw_str,'cost_ms':0})
    for gw in gws[:1]:
        try:
            s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(2);t0=time.time()
            s.connect((gw,80));cost=int((time.time()-t0)*1000);s.close()
            results.append({'label':f'Ping {gw}','status':'ok','detail':f'可达({cost}ms)','cost_ms':cost})
        except: results.append({'label':f'Ping {gw}','status':'error','detail':'不可达','cost_ms':2000})
    return {'name':'网关检测','status':'ok' if gws else 'error','items':results,'total_cost_ms':sum(r['cost_ms'] for r in results)}

# ---- External ----
def check_external():
    targets = [('114.114.114.114',53),('8.8.8.8',53)]
    results = []
    def do_ping(ip,port):
        try:
            s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(3);t0=time.time()
            s.connect((ip,port));cost=int((time.time()-t0)*1000);s.close()
            return {'label':ip,'status':'ok','detail':f'可达({cost}ms)','cost_ms':cost}
        except: return {'label':ip,'status':'error','detail':'超时/不可达','cost_ms':3000}
    with ThreadPoolExecutor(2) as p:
        results = [f.result() for f in [p.submit(do_ping,ip,port) for ip,port in targets]]
    return {'name':'外网连通','status':'ok' if all(r['status']=='ok' for r in results) else 'error','items':results,'total_cost_ms':sum(r['cost_ms'] for r in results)}

# ---- Websites ----
def check_websites():
    import urllib.request
    sites = [('https://www.baidu.com','Baidu'),('https://weixin.qq.com','WeChat'),('https://www.taobao.com','Taobao')]
    def do_check(url,name):
        try:
            t0=time.time();req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
            resp=urllib.request.urlopen(req,timeout=5);cost=int((time.time()-t0)*1000);data=resp.read()
            return {'label':name,'status':'ok','detail':f'HTTP {resp.status}({cost}ms)','cost_ms':cost}
        except Exception as e: return {'label':name,'status':'error','detail':str(e)[:80],'cost_ms':5000}
    with ThreadPoolExecutor(3) as p:
        results = [f.result() for f in [p.submit(do_check,u,n) for u,n in sites]]
    ok = sum(1 for r in results if r['status']=='ok')
    return {'name':'网站可用性','status':'ok' if ok==3 else ('warn' if ok>0 else 'error'),'items':results,'total_cost_ms':sum(r['cost_ms'] for r in results)}

# ---- Ports ----
def check_ports():
    ports = [80,443,22,3389]; target='114.114.114.114'; results=[]
    for port in ports:
        try:
            s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(3);t0=time.time()
            r=s.connect_ex((target,port));cost=int((time.time()-t0)*1000);s.close()
            results.append({'label':f'{target}:{port}','status':'ok' if r==0 else 'warn','detail':f'开放({cost}ms)' if r==0 else '关闭/过滤','cost_ms':cost})
        except: results.append({'label':f'{target}:{port}','status':'error','detail':'error','cost_ms':3000})
    return {'name':'端口检测','status':'ok','items':results,'total_cost_ms':sum(r['cost_ms'] for r in results)}

# ---- Proxy ----
def check_proxy():
    keys=['ProxyEnable','ProxyServer','ProxyOverride','AutoConfigURL']
    reg=r'HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings'; results=[]
    for k in keys:
        out,rc=run_cmd(f'reg query "{reg}" /v {k}',5)
        val=out.split('REG_')[-1].strip() if 'REG_' in out else (out.strip() if rc==0 else '未设置')
        results.append({'label':k,'status':'ok','detail':val,'cost_ms':0})
    enabled=any('0x1' in r['detail'] for r in results if r['label']=='ProxyEnable')
    return {'name':'代理检测','status':'warn' if enabled else 'ok','items':results,'total_cost_ms':0}

# ---- WiFi ----
def check_wifi():
    out,_=run_cmd('netsh wlan show interfaces',5)
    if '没有' in out or 'WLAN' not in out.upper() and 'WiFi' not in out:
        return {'name':'WiFi信息','status':'warn','detail':'未检测到WiFi（可能有线连接）','cost_ms':0}
    info={}
    for l in out.split('\n'):
        if ':' in l:
            k,v=l.split(':',1);k=k.strip();v=v.strip()
            if k in ('SSID','信号','接收速率 (Mbps)','连接状态','信道'): info[k]=v
    return {'name':'WiFi信息','status':'ok','detail':json.dumps(info,ensure_ascii=False) if info else out[-300:],'cost_ms':0}

# ---- Bandwidth ----
def check_bandwidth():
    import urllib.request
    urls=[('http://speedtest.tele2.net/1MB.zip','Tele2'),('http://cachefly.cachefly.net/1mb.test','CacheFly')]
    for url,label in urls:
        try:
            t0=time.time();resp=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=15)
            total=0
            while True:
                chunk=resp.read(65536)
                if not chunk or time.time()-t0>10: break
                total+=len(chunk)
            elapsed=time.time()-t0;speed=(total*8/1024/1024)/elapsed if elapsed>0 else 0
            return {'name':'带宽测速','status':'ok','detail':f'{label}: {speed:.2f} Mbps','cost_ms':int(elapsed*1000)}
        except Exception as e: continue
    return {'name':'带宽测速','status':'error','detail':'所有测速源不可用','cost_ms':0}

# ---- Traceroute ----
def check_traceroute():
    out,rc=run_cmd('tracert -d -h 8 -w 2000 baidu.com',30)
    hops=[l for l in out.split('\n') if re.match(r'\s*\d+',l)][:8]
    return {'name':'路由追踪','status':'ok' if hops else 'warn','detail':'\n'.join(hops) if hops else out[-300:],'cost_ms':0}

ALL_CHECKS = [('dns',check_dns),('gateway',check_gateway),('external',check_external),('websites',check_websites),('ports',check_ports),('proxy',check_proxy),('wifi',check_wifi),('bandwidth',check_bandwidth),('traceroute',check_traceroute)]

# ---- Repair ----
REPAIR_MAP = {
    'flush_dns':('刷新DNS缓存','ipconfig /flushdns',10),
    'reset_winsock':('重置Winsock','netsh winsock reset',15),
    'reset_tcp':('重置TCP/IP','netsh int ip reset',15),
    'release_renew':('释放重获IP','ipconfig /release && ipconfig /renew',30),
    'flush_arp':('清空ARP缓存','arp -d',10),
    'reset_proxy':('关闭系统代理',r'reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f',10),
}
ITEM_REPAIR = {
    'dns':['flush_dns','set_dns'],'gateway':['reset_adapter','release_renew'],
    'external':['flush_dns','reset_proxy','reset_tcp'],'websites':['flush_dns','reset_proxy'],
    'ports':['reset_tcp','reset_adapter'],'proxy':['reset_proxy'],'wifi':['reset_adapter'],
    'bandwidth':['flush_dns','reset_tcp'],'traceroute':['flush_dns','reset_tcp'],
}

def _get_active_interfaces():
    """Get list of active network interface names using netsh."""
    out, rc = run_cmd('netsh interface show interface', 5)
    ifaces = []
    lines = out.split('\n')
    for i, line in enumerate(lines):
        # Find lines containing "Connected" (the Chinese text)
        if '\u5df2\u8fde\u63a5' in line or 'Connected' in line:
            # Interface name is in the 4th column
            parts = line.strip().split()
            if len(parts) >= 4:
                # The interface name might have been consumed by earlier columns
                # Try to get it from the last column
                iface = parts[-1]
                if iface and iface not in ('专用', 'Dedicated', '状态', 'State', '类型', 'Type'):
                    ifaces.append(iface)
    return ifaces

def _set_dns_iface(iface, dns1, dns2):
    """Set DNS for an interface using netsh."""
    out1, rc1 = run_cmd(f'netsh interface ip set dnsservers name="{iface}" static {dns1} primary validate=no', 15)
    out2, rc2 = run_cmd(f'netsh interface ip add dnsservers name="{iface}" {dns2} index=2 validate=no', 15)
    return (rc1 == 0 and rc2 == 0), f'{out1}\n{out2}'

def _reset_adapter_iface(iface):
    """Reset adapter using netsh."""
    out1, rc1 = run_cmd(f'netsh interface set interface name="{iface}" admin=disable', 15)
    time.sleep(3)
    out2, rc2 = run_cmd(f'netsh interface set interface name="{iface}" admin=enable', 15)
    return (rc1 == 0 and rc2 == 0), f'Disable:{out1}\nEnable:{out2}'

def run_repair(action, failed_items=None):
    if action=='repair_all':
        failed_items=failed_items or []
        if failed_items:
            needed=set()
            for i in failed_items: needed.update(ITEM_REPAIR.get(i,[]))
            keys=list(needed)
        else:
            keys=['flush_dns','reset_winsock','reset_tcp','release_renew','flush_arp','reset_proxy','set_dns']
        results=[]
        for k in keys: results.append(run_repair(k))
        return {'status':'ok','action':'repair_all','output':f'执行了{len(keys)}项修复','details':results,'matched_repairs':keys}
    if action=='set_dns':
        ifaces = _get_active_interfaces()
        if not ifaces: return {'status':'error','action':action,'output':'未找到网络接口'}
        results = []
        all_ok = True
        for iface in ifaces:
            ok, detail = _set_dns_iface(iface, '223.5.5.5', '223.6.6.6')
            results.append(f'{iface}: {"成功" if ok else detail[:80]}')
            if not ok: all_ok = False
        return {'status':'ok' if all_ok else 'error','action':action,'output':'\n'.join(results)}
    if action=='reset_adapter':
        ifaces = _get_active_interfaces()
        if not ifaces: return {'status':'error','action':action,'output':'未找到网络接口'}
        results = []
        all_ok = True
        for iface in ifaces:
            ok, detail = _reset_adapter_iface(iface)
            results.append(f'{iface}: {"成功" if ok else detail[:80]}')
            if not ok: all_ok = False
        return {'status':'ok' if all_ok else 'error','action':action,'output':'\n'.join(results)}
    if action in REPAIR_MAP:
        name,cmd,timeout=REPAIR_MAP[action]
        out,rc=run_cmd(cmd,timeout)
        return {'status':'ok' if rc==0 else 'error','action':action,'output':out}
    return {'status':'error','action':action,'output':f'未知操作:{action}'}

# ---- Main ----
def handle_diagnose():
    t0=time.time()
    results=[]
    with ThreadPoolExecutor(9) as pool:
        futs={pool.submit(fn):key for key,fn in ALL_CHECKS}
        for f in as_completed(futs):
            try:
                r=f.result();r['key']=futs[f]
                results.append(r)
            except Exception as e:
                results.append({'key':futs[f],'name':futs[f],'status':'error','detail':str(e),'cost_ms':0})
    total=int((time.time()-t0)*1000)
    send_msg({'type':'diagnose_result','items':results,'total_time_ms':total,'timestamp':time.strftime('%Y-%m-%d %H:%M:%S')})

def handle(msg):
    action=msg.get('action')
    if action=='diagnose':
        handle_diagnose()
    elif action=='repair':
        failed=msg.get('failed_items',[])
        repair_action=msg.get('repair_action','repair_all')
        result=run_repair(repair_action,failed)
        send_msg({'type':'repair_result','data':result})
    elif action=='ping':
        send_msg({'type':'pong'})
    else:
        send_msg({'type':'error','message':f'未知操作:{action}'})

while True:
    msg=read_msg()
    handle(msg)
