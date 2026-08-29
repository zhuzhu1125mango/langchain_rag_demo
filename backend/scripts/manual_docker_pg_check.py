"""Docker PostgreSQL 连通性验证脚本。

直接连接 Docker 中运行的 PostgreSQL，用于排查容器化环境下的连接问题。
"""

import psycopg2

try:
    conn = psycopg2.connect(
        dbname='app_dev',
        user='dev_user',
        password='dev_password',
        host='localhost',
        port='5433',
        connect_timeout=5
    )
    print('SUCCESS: Connected to Docker PostgreSQL!')
    conn.close()
except Exception as e:
    print(f'Error: {type(e).__name__}: {e}')
