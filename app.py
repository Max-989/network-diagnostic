# -*- coding: utf-8 -*-
"""Network Diagnostic + One-Click Repair Web Tool"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import urllib.request
import urllib.error

try:
    import requests as req_lib
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

PORT = 19500
WEB_DIR = os.path.dirname(os.path.abspath(__file__))


def run_cmd(cmd, timeout=15, encoding='gbk'):
    """Run a cmd command and return output."""
    try:
        result = subprocess.run(
            f'cmd /c {cmd}',
            capture_output=True, timeout=timeout,
            shell=True
        )
        out = result.stdout.decode(encoding, errors='replace') + result.stderr.decode(encoding, errors='replace')
        return out.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return f'[TIMEOUT] Command exceeded {timeout}s', -1
    except Exception as e:
        return f'[ERROR] {e}', -1


def time_ms(fn):
    """Time a function call in milliseconds."""
    t0 = time.time()
    result = fn()
    cost = int((time.time() - t0) * 1000)
    return result, cost


# --------------- Diagnostic Checks ---------------

def _resolve_dns(domain, dns_server=None, timeout=3):
    """Fast DNS resolve using Python socket (no subprocess)."""
    try:
        t0 = time.time()
        if dns_server:
            # Use custom DNS via socket with a simple UDP query
            import struct
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            # Build a simple DNS query
            txid = os.urandom(2)
            flags = b'\x01\x00'  # standard query, recursion desired
            counts = b'\x00\x01\x00\x00\x00\x00\x00\x00'
            qname = b''
            for part in domain.split('.'):
                qname += bytes([len(part)]) + part.encode()
            qname += b'\x00'
            qtype = b'\x00\x01'  # A record
            query = txid + flags + counts + qname + qtype
            sock.sendto(query, (dns_server, 53))
            data, _ = sock.recvfrom(1024)
            sock.close()
            # Parse IPs from response (simple: extract all IPv4 patterns)
            ips = re.findall(rb'\xc0\x0c\x00\x01.{6}((?:\d{1,3}\.){3}\d{1,3})', data)
            if not ips:
                # Try harder: any 4 bytes after answer section
                ips = [b'.'.join(bytes([b]) for b in data[i:i+4])
                       for i in range(12, len(data)-4, 12)]
            ips = [ip.decode() if isinstance(ip, bytes) else ip for ip in ips
                   if ip and re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip.decode() if isinstance(ip, bytes) else ip)]
            cost = int((time.time() - t0) * 1000)
            if ips:
                return {'status': 'ok', 'ips': ips, 'cost_ms': cost}
            return {'status': 'error', 'detail': '无解析结果', 'cost_ms': cost}
        else:
            ips = socket.getaddrinfo(domain, None, socket.AF_INET)
            cost = int((time.time() - t0) * 1000)
            if ips:
                unique = list(set(addr[4][0] for addr in ips))
                return {'status': 'ok', 'ips': unique, 'cost_ms': cost}
            return {'status': 'error', 'detail': '无解析结果', 'cost_ms': cost}
    except socket.timeout:
        return {'status': 'error', 'detail': '超时', 'cost_ms': timeout * 1000}
    except Exception as e:
        return {'status': 'error', 'detail': str(e)[:80], 'cost_ms': 0}


def check_dns():
    """DNS resolution check - fully parallel."""
    targets = [
        ('baidu.com', None, '系统默认'),
        ('qq.com', None, '系统默认'),
        ('baidu.com', '223.5.5.5', '阿里DNS'),
        ('qq.com', '223.5.5.5', '阿里DNS'),
        ('baidu.com', '119.29.29.29', '腾讯DNS'),
        ('qq.com', '119.29.29.29', '腾讯DNS'),
    ]

    def do_one(domain, dns, label):
        r = _resolve_dns(domain, dns)
        return {
            'label': f'{domain} ({label})',
            'status': r['status'],
            'detail': ', '.join(r.get('ips', [])) if r['status'] == 'ok' else r.get('detail', ''),
            'cost_ms': r.get('cost_ms', 0),
        }

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(do_one, d, dns, lbl): (d, lbl) for d, dns, lbl in targets}
        for f in as_completed(futs):
            results.append(f.result())

    all_ok = all(r['status'] == 'ok' for r in results)
    return {
        'name': 'DNS 检测', 'status': 'ok' if all_ok else 'warn',
        'items': results, 'total_cost_ms': max(r['cost_ms'] for r in results)
    }


def check_gateway():
    """Gateway detection and ping - socket-based, fast."""
    results = []
    # Get default gateway
    def do_get_gw():
        out, rc = run_cmd('route print -4 0.0.0.0', timeout=5)
        gateways = []
        for line in out.split('\n'):
            parts = line.strip().split()
            if len(parts) >= 5 and parts[0] == '0.0.0.0':
                gateways.append(parts[2])
        return gateways if gateways else ['unknown']

    gateways, cost_gw = time_ms(do_get_gw)
    gw_str = ', '.join(gateways)
    results.append({'label': '默认网关', 'status': 'ok' if gw_str != 'unknown' else 'error',
                    'detail': gw_str, 'cost_ms': cost_gw})

    # Ping gateway using socket (faster than subprocess ping -n 3)
    for gw in gateways[:2]:
        if gw == 'unknown':
            continue
        def do_ping():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                t0 = time.time()
                sock.connect((gw, 80))
                cost = int((time.time() - t0) * 1000)
                sock.close()
                return {'status': 'ok', 'detail': f'可达 ({cost}ms)'}
            except (socket.timeout, OSError):
                return {'status': 'error', 'detail': '不可达'}

        res, cost = time_ms(do_ping)
        results.append({'label': f'Ping {gw}', 'status': res['status'],
                        'detail': res['detail'], 'cost_ms': cost})

    return {'name': '网关检测', 'status': 'ok' if gw_str != 'unknown' else 'error',
            'items': results, 'total_cost_ms': sum(r['cost_ms'] for r in results)}


def check_external():
    """External connectivity - socket-based, parallel, fast."""
    targets = [('114.114.114.114', 53), ('8.8.8.8', 53)]
    results = []
    all_ok = True

    def do_ping(ip, port):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            t0 = time.time()
            sock.connect((ip, port))
            cost = int((time.time() - t0) * 1000)
            sock.close()
            return {'status': 'ok', 'detail': f'可达 ({cost}ms)', 'cost_ms': cost}
        except (socket.timeout, OSError):
            return {'status': 'error', 'detail': '超时/不可达', 'cost_ms': 3000}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(do_ping, ip, port): ip for ip, port in targets}
        for f in as_completed(futs):
            r = f.result()
            if r['status'] != 'ok':
                all_ok = False
            results.append({'label': futs[f], 'status': r['status'],
                            'detail': r['detail'], 'cost_ms': r['cost_ms']})

    return {'name': '外网连通', 'status': 'ok' if all_ok else 'error', 'items': results,
            'total_cost_ms': sum(r['cost_ms'] for r in results)}


def check_websites():
    """Website accessibility check - parallel."""
    sites = [
        ('https://www.baidu.com', 'Baidu'),
        ('https://weixin.qq.com', 'WeChat'),
        ('https://www.taobao.com', 'Taobao'),
    ]

    def do_check(url, name):
        try:
            if HAS_REQUESTS:
                t0 = time.time()
                r = req_lib.get(url, timeout=5, allow_redirects=True)
                cost = int((time.time() - t0) * 1000)
                code = r.status_code
                if code < 400:
                    return {'label': name, 'url': url, 'status': 'ok',
                            'detail': f'HTTP {code} ({cost}ms)', 'cost_ms': cost}
                else:
                    return {'label': name, 'url': url, 'status': 'error',
                            'detail': f'HTTP {code}', 'cost_ms': cost}
            else:
                t0 = time.time()
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                resp = urllib.request.urlopen(req, timeout=5)
                cost = int((time.time() - t0) * 1000)
                content = resp.read()
                return {'label': name, 'url': url, 'status': 'ok',
                        'detail': f'HTTP {resp.status} ({cost}ms)', 'cost_ms': cost}
        except Exception as e:
            return {'label': name, 'url': url, 'status': 'error',
                    'detail': str(e)[:100], 'cost_ms': 5000}

    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(do_check, url, name): name for url, name in sites}
        for f in as_completed(futs):
            results.append(f.result())

    ok_count = sum(1 for r in results if r['status'] == 'ok')
    status = 'ok' if ok_count == len(results) else ('warn' if ok_count > 0 else 'error')
    return {'name': '网站可用性', 'status': status, 'items': results,
            'total_cost_ms': sum(r['cost_ms'] for r in results)}


def check_ports():
    """Port connectivity check."""
    ports = [80, 443, 22, 3389]
    target = '114.114.114.114'
    # For 80/443, use a web target; for 22/3389, use the IP
    results = []

    for port in ports:
        def do_check():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                t0 = time.time()
                result = sock.connect_ex((target, port))
                cost = int((time.time() - t0) * 1000)
                sock.close()
                if result == 0:
                    return {'status': 'ok', 'detail': f'开放 ({cost}ms)'}
                else:
                    return {'status': 'warn', 'detail': f'关闭/过滤'}
            except Exception as e:
                return {'status': 'error', 'detail': str(e)[:80]}

        res, cost = time_ms(do_check)
        results.append({'label': f'{target}:{port}', 'status': res['status'],
                        'detail': res['detail'], 'cost_ms': cost})

    return {'name': '端口检测', 'status': 'ok', 'items': results,
            'total_cost_ms': sum(r['cost_ms'] for r in results)}


def check_proxy():
    """System proxy settings check."""
    results = []
    keys = ['ProxyEnable', 'ProxyServer', 'ProxyOverride', 'AutoConfigURL']
    reg_path = r'HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings'

    for key in keys:
        def do_query():
            out, rc = run_cmd(f'reg query "{reg_path}" /v {key}', timeout=5)
            if rc == 0:
                val = out.split('REG_')[-1].strip() if 'REG_' in out else out.strip()
                return {'status': 'ok', 'detail': val}
            return {'status': 'warn', 'detail': '未设置'}

        res, cost = time_ms(do_query)
        results.append({'label': key, 'status': res['status'],
                        'detail': res['detail'], 'cost_ms': cost})

    # Check if proxy is enabled
    enabled = any('0x1' in r['detail'] for r in results if r['label'] == 'ProxyEnable')
    return {'name': '代理检测', 'status': 'warn' if enabled else 'ok',
            'items': results, 'total_cost_ms': sum(r['cost_ms'] for r in results)}


def check_traceroute():
    """Traceroute to baidu.com (first 10 hops)."""
    def do_trace():
        out, rc = run_cmd('tracert -d -h 8 -w 2000 baidu.com', timeout=30)
        lines = out.split('\n')
        hops = []
        for line in lines:
            if re.match(r'\s*\d+', line):
                line = re.sub(r'\s{2,}', '|', line.strip())
                hops.append(line)
            if len(hops) >= 10:
                break
        if hops:
            return {'status': 'ok', 'detail': '\n'.join(hops)}
        return {'status': 'warn', 'detail': out[-300:]}

    res, cost = time_ms(do_trace)
    return {'name': '路由追踪', 'status': res['status'],
            'detail': res['detail'], 'cost_ms': cost}


def check_bandwidth():
    """Bandwidth speed test by downloading a small file."""
    test_urls = [
        ('http://speedtest.tele2.net/1MB.zip', 'Tele2 1MB'),
        ('http://cachefly.cachefly.net/1mb.test', 'CacheFly 1MB'),
    ]

    for url, label in test_urls:
        def do_download():
            try:
                if HAS_REQUESTS:
                    t0 = time.time()
                    r = req_lib.get(url, timeout=15, stream=True)
                    total = 0
                    for chunk in r.iter_content(chunk_size=65536):
                        total += len(chunk)
                        if time.time() - t0 > 10:  # bail after 10s
                            break
                    elapsed = time.time() - t0
                    speed = (total * 8 / 1024 / 1024) / elapsed if elapsed > 0 else 0
                    return {'status': 'ok',
                            'detail': f'{label}: {speed:.2f} Mbps ({total//1024}KB in {elapsed:.1f}s)'}
                else:
                    t0 = time.time()
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    resp = urllib.request.urlopen(req, timeout=15)
                    total = len(resp.read())
                    elapsed = time.time() - t0
                    speed = (total * 8 / 1024 / 1024) / elapsed if elapsed > 0 else 0
                    return {'status': 'ok',
                            'detail': f'{label}: {speed:.2f} Mbps ({total//1024}KB in {elapsed:.1f}s)'}
            except Exception as e:
                return {'status': 'error', 'detail': str(e)[:100]}

        res, cost = time_ms(do_download)
        return {'name': '带宽测速', 'status': res['status'],
                'detail': res['detail'], 'cost_ms': cost}

    return {'name': '带宽测速', 'status': 'error', 'detail': '所有测速源不可用', 'cost_ms': 0}


def check_wifi():
    """WiFi interface info."""
    def do_check():
        out, rc = run_cmd('netsh wlan show interfaces', timeout=5)
        if '没有' in out or 'WLAN' not in out.upper() and 'WiFi' not in out:
            return {'status': 'warn', 'detail': '未检测到WiFi接口（可能是有线连接）'}
        # Parse useful info
        info = {}
        for line in out.split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                key = key.strip()
                val = val.strip()
                if key in ('SSID', 'BSSID', '信号', '接收速率 (Mbps)', '连接状态', '信道'):
                    info[key] = val
        if info:
            return {'status': 'ok', 'detail': json.dumps(info, ensure_ascii=False, indent=2)}
        return {'status': 'ok', 'detail': out[-500:]}

    res, cost = time_ms(do_check)
    return {'name': 'WiFi 信息', 'status': res['status'],
            'detail': res['detail'], 'cost_ms': cost}


ALL_CHECKS = [
    ('dns', check_dns),
    ('gateway', check_gateway),
    ('external', check_external),
    ('websites', check_websites),
    ('ports', check_ports),
    ('proxy', check_proxy),
    ('wifi', check_wifi),
    ('bandwidth', check_bandwidth),
    ('traceroute', check_traceroute),
]

# --------------- Repair Actions ---------------

REPAIR_ACTIONS = {
    'flush_dns': {
        'name': '刷新 DNS 缓存',
        'desc': '清除系统 DNS 解析缓存',
        'cmd': 'ipconfig /flushdns',
        'timeout': 10,
    },
    'reset_winsock': {
        'name': '重置 Winsock',
        'desc': '修复 Winsock 目录，解决网络连接问题',
        'cmd': 'netsh winsock reset',
        'timeout': 15,
    },
    'reset_tcp': {
        'name': '重置 TCP/IP 协议栈',
        'desc': '重置 TCP/IP 为安装时状态',
        'cmd': 'netsh int ip reset',
        'timeout': 15,
    },
    'release_renew': {
        'name': '释放并重新获取 IP',
        'desc': '释放当前 IP 并重新从 DHCP 获取',
        'cmd': 'ipconfig /release && ipconfig /renew',
        'timeout': 30,
    },
    'flush_arp': {
        'name': '清空 ARP 缓存',
        'desc': '清除地址解析协议缓存表',
        'cmd': 'arp -d',
        'timeout': 10,
    },
    'reset_proxy': {
        'name': '关闭系统代理',
        'desc': '禁用 Windows 系统代理设置',
        'cmd': r'reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f',
        'timeout': 10,
    },
    'set_dns': {
        'name': '设置阿里 DNS',
        'desc': '将 DNS 设为 223.5.5.5 + 223.6.6.6',
        'cmd': None,  # special handling
        'timeout': 15,
    },
    'reset_adapter': {
        'name': '重置网卡',
        'desc': '禁用再启用网络适配器',
        'cmd': None,  # special handling
        'timeout': 30,
    },
    'repair_all': {
        'name': '一键全部修复',
        'desc': '按顺序执行所有修复操作',
        'cmd': None,  # special handling
        'timeout': 120,
    },
}


def run_ps(cmd, timeout=15):
    """Run a PowerShell command directly (avoids cmd /c encoding issues)."""
    try:
        result = subprocess.run(
            ['powershell', '-Command', cmd],
            capture_output=True, timeout=timeout
        )
        out = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
        return out.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return f'[TIMEOUT] {timeout}s', -1
    except Exception as e:
        return f'[ERROR] {e}', -1


def run_repair(action, failed_items=None):
    """Execute a repair action."""
    if action not in REPAIR_ACTIONS:
        return {'status': 'error', 'action': action, 'output': f'未知操作: {action}'}

    info = REPAIR_ACTIONS[action]

    # 诊断项 → 对应修复动作映射
    ITEM_REPAIR_MAP = {
        'dns':       ['flush_dns', 'set_dns'],
        'gateway':   ['reset_adapter', 'release_renew'],
        'external':  ['flush_dns', 'reset_proxy', 'reset_tcp'],
        'websites':  ['flush_dns', 'reset_proxy'],
        'ports':     ['reset_tcp', 'reset_adapter'],
        'proxy':     ['reset_proxy'],
        'wifi':      ['reset_adapter'],
        'bandwidth': ['flush_dns', 'reset_tcp'],
        'traceroute':['flush_dns', 'reset_tcp'],
    }

    if action == 'repair_all':
        failed_items = failed_items or []
        failed_items = [x for x in failed_items if isinstance(x, str)]

        # Determine which repairs to run
        if failed_items:
            needed = set()
            for item in failed_items:
                needed.update(ITEM_REPAIR_MAP.get(item, []))
            repair_keys = list(needed)
        else:
            repair_keys = ['flush_dns', 'reset_winsock', 'reset_tcp',
                           'release_renew', 'flush_arp', 'reset_proxy', 'set_dns']

        results = []
        for key in repair_keys:
            r = run_repair(key)
            results.append(r)
        return {'status': 'ok', 'action': 'repair_all',
                'output': f'已执行 {len(repair_keys)} 项针对性修复', 'details': results,
                'matched_repairs': repair_keys, 'failed_items': failed_items}

    if action == 'set_dns':
        out_iface, _ = run_ps("Get-NetAdapter | Where-Object Status -eq Up | Select-Object -ExpandProperty Name", timeout=5)
        interfaces = [line.strip() for line in out_iface.split('\n') if line.strip()]
        if not interfaces:
            return {'status': 'error', 'action': action, 'output': '未找到已连接的网络接口'}
        results_dns = []
        all_ok = True
        for iface in interfaces:
            out, rc = run_ps(f"Set-DnsClientServerAddress -InterfaceAlias '{iface}' -ServerAddresses ('223.5.5.5','223.6.6.6')", timeout=15)
            results_dns.append(f'{iface}: {"成功" if rc == 0 else out}')
            if rc != 0:
                all_ok = False
        return {'status': 'ok' if all_ok else 'error',
                'action': action, 'output': '\n'.join(results_dns)}

    if action == 'reset_adapter':
        out_iface, _ = run_ps("Get-NetAdapter | Where-Object Status -eq Up | Select-Object -ExpandProperty Name", timeout=5)
        interfaces = [line.strip() for line in out_iface.split('\n') if line.strip()]
        if not interfaces:
            return {'status': 'error', 'action': action, 'output': '未找到已连接的网络接口'}
        results_ad = []
        all_ok = True
        for iface in interfaces:
            run_ps(f"Disable-NetAdapter -Name '{iface}' -Confirm:$false", timeout=15)
            time.sleep(3)
            out, rc = run_ps(f"Enable-NetAdapter -Name '{iface}' -Confirm:$false", timeout=15)
            results_ad.append(f'{iface}: {"重置成功" if rc == 0 else out[:100]}')
            if rc != 0:
                all_ok = False
        return {'status': 'ok' if all_ok else 'error',
                'action': action, 'output': '\n'.join(results_ad)}

    # Standard command
    out, rc = run_cmd(info['cmd'], timeout=info['timeout'])
    return {'status': 'ok' if rc == 0 else 'error',
            'action': action, 'output': out}


# --------------- HTTP Handler ---------------

class NetDiagHandler(SimpleHTTPRequestHandler):
    """HTTP request handler."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        parsed = self.path.split('?')
        path = parsed[0]
        qs = parsed[1] if len(parsed) > 1 else ''

        if path == '/' or path == '/index.html':
            self.serve_html('index.html')
        elif path == '/api/diagnose':
            item = None
            if qs:
                params = dict(p.split('=') for p in qs.split('&') if '=' in p)
                item = params.get('item')
            self.handle_diagnose(item)
        elif path == '/api/repair/list':
            self.serve_json(list(REPAIR_ACTIONS.keys()))
        elif path.endswith('.html'):
            self.serve_html(path[1:])
        elif path.endswith(('.js', '.css', '.png', '.ico', '.svg')):
            super().do_GET()
        else:
            self.serve_html('index.html')

    def do_POST(self):
        if self.path == '/api/diagnose':
            self.handle_diagnose()
        elif self.path == '/api/repair':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length > 0 else b''
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            action = data.get('action', '')
            failed_items = data.get('failed_items', [])
            result = run_repair(action, failed_items)
            self.serve_json(result)
        else:
            self.send_error(404)

    def handle_diagnose(self, item=None):
        """Run diagnostic checks - SSE streaming, results arrive in real-time."""
        checks = ALL_CHECKS
        if item:
            checks = [(k, v) for k, v in ALL_CHECKS if k == item]
            if not checks:
                self.serve_json({'error': f'Unknown item: {item}'})
                return

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        t0 = time.time()

        def send_event(data):
            self.wfile.write(f'data: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8'))
            self.wfile.flush()

        def run_check(key, fn):
            try:
                result = fn()
                result['key'] = key
                return result
            except Exception as e:
                return {'key': key, 'name': key, 'status': 'error',
                        'detail': str(e), 'cost_ms': 0}

        # Run ALL checks in parallel using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=9) as pool:
            futures = {pool.submit(run_check, key, fn): key for key, fn in checks}
            for future in as_completed(futures):
                result = future.result()
                send_event({'type': 'item', 'data': result})

        total_time = int((time.time() - t0) * 1000)
        send_event({'type': 'done', 'total_time_ms': total_time,
                     'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')})

    def serve_html(self, filename):
        filepath = os.path.join(WEB_DIR, filename)
        if not os.path.exists(filepath):
            self.send_error(404)
            return
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(content.encode('utf-8'))

    def serve_json(self, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress default logging
        pass


def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    server = HTTPServer(('0.0.0.0', PORT), NetDiagHandler)
    print(f'Network Diagnostic Tool running at http://localhost:{PORT}')
    print(f'   Press Ctrl+C to stop')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServer stopped.')
        server.server_close()


if __name__ == '__main__':
    main()
