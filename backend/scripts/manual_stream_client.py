"""SSE 流式接口手动测试客户端。

通过命令行参数传入问题与可选断连时间，连接本地服务的 /api/chat/stream 接口打印流式响应。
"""

import requests
import json
import time
import sys

question = sys.argv[1] if len(sys.argv) > 1 else '测试流式消息保存8'
disconnect_after = float(sys.argv[2]) if len(sys.argv) > 2 else 0

url = 'http://localhost:8000/api/chat/stream'
print(f'Starting SSE request to {url}')
if disconnect_after > 0:
    print(f'Will disconnect after {disconnect_after}s')
start = time.time()
try:
    with requests.post(url, json={'question': question, 'use_web_search': False}, stream=True, timeout=300) as r:
        print(f'Status: {r.status_code}')
        for line in r.iter_lines():
            if line:
                decoded = line.decode('utf-8')
                elapsed = time.time() - start
                print(f'[{elapsed:.2f}s] {decoded}')
                if disconnect_after > 0 and elapsed > disconnect_after:
                    print(f'[{elapsed:.2f}s] Disconnecting early...')
                    break
except Exception as e:
    print(f'Error: {e}')
print(f'Total time: {time.time() - start:.2f}s')
