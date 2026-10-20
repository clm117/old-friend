# 网络接入与 HTTPS 部署指南

## 一、域名与 DNS 解析（任务 34）

### 域名配置
| 记录类型 | 主机记录 | 记录值 | TTL | 说明 |
|---------|---------|--------|-----|------|
| A | @ | 服务器公网 IP | 600 | 主域名 |
| A | www | 服务器公网 IP | 600 | www 子域名 |
| A | api | 服务器公网 IP | 600 | API 子域名（可选） |

### 备案要求
- **ICP 备案**：域名指向中国大陆服务器必须备案
- **备案周期**：通常 15-20 个工作日
- **备案期间**：域名无法解析到国内服务器

### 临时方案（未备案）
```nginx
# 使用 Cloudflare 隧道或境外 CDN 中转
# 或使用已备案的域名做 CNAME 指向
```

## 二、证书申请与自动续期（任务 35）

### Let's Encrypt（推荐）
```bash
# 安装 certbot
sudo apt install -y certbot python3-certbot-nginx

# 申请证书
sudo certbot --nginx -d your-domain.com -d www.your-domain.com

# 自动续期
sudo certbot renew --dry-run

# 续期定时任务（已自动添加）
# /etc/cron.d/certbot
```

### 阿里云/腾讯云免费证书
1. 控制台申请 DV 证书
2. 下载 nginx 格式证书（.pem + .key）
3. 上传到服务器 `/etc/nginx/ssl/`

### 证书路径配置
```nginx
ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;
```

## 三、nginx TLS 生产配置（任务 36）

### 完整配置
```nginx
# /etc/nginx/sites-available/yuban
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    # SSL 证书
    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    # TLS 配置
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;
    ssl_session_tickets off;

    # HSTS
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;

    # 其他安全头
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header Permissions-Policy "microphone=(self), camera=(), geolocation=()" always;

    # 前端静态文件
    root /var/www/yuban/frontend;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 反代
    location /api/ {
        proxy_pass http://127.0.0.1:8005;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://127.0.0.1:8005;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 3600s;
    }

    # 健康检查
    location = /health {
        proxy_pass http://127.0.0.1:8005/health;
    }
}

# HTTP 跳转 HTTPS
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}
```

## 四、WebSocket 升级反代（任务 37）

### WebSocket 配置要点
```nginx
# 必须配置 Upgrade 头
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

location /ws/ {
    proxy_pass http://127.0.0.1:8005;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    
    # 超时调优（长连接）
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    
    # 缓冲关闭（实时性）
    proxy_buffering off;
}
```

## 五、公网带宽与限速（任务 38）

### 带宽评估
| 场景 | 并发数 | 带宽需求 | 说明 |
|------|--------|---------|------|
| 语音对话 | 10 | 10 Mbps | 每路约 1 Mbps |
| TTS 合成 | 5 | 5 Mbps | 音频文件下载 |
| 峰值 | 20 | 25 Mbps | 建议预留 50% 余量 |

### nginx 限速
```nginx
# 全局限速
limit_conn_zone $binary_remote_addr zone=conn_limit:10m;
limit_req_zone $binary_remote_addr zone=req_limit:10m rate=10r/s;

server {
    # 每 IP 最大连接数
    limit_conn conn_limit 20;
    
    # 请求频率限制
    limit_req zone=req_limit burst=20 nodelay;
    
    # 带宽限制（每连接）
    limit_rate 1m;  # 1MB/s
}
```

## 六、端口收敛（任务 39）

### 端口规划
| 端口 | 服务 | 暴露范围 | 说明 |
|------|------|---------|------|
| 80 | nginx | 公网 | HTTP 跳转 HTTPS |
| 443 | nginx | 公网 | HTTPS 主服务 |
| 8005 | api-gateway | 内网 | 仅 nginx 可达 |
| 8084 | ai-service | 内网 | 仅 api-gateway 可达 |
| 6379 | redis | 内网 | 仅容器内网 |
| 5432 | postgres | 内网 | 仅容器内网 |

### Docker 网络隔离
```yaml
# docker-compose.yml
version: '3.8'

networks:
  frontend:
    driver: bridge
  backend:
    driver: bridge
    internal: true  # 不暴露到公网

services:
  nginx:
    ports:
      - "80:80"
      - "443:443"
    networks:
      - frontend
      - backend

  api-gateway:
    expose:
      - "8005"
    networks:
      - backend
    # 不映射到宿主机

  ai-service:
    expose:
      - "8084"
    networks:
      - backend
    # 不映射到宿主机
```

## 七、验证清单

- [ ] 域名解析正常
- [ ] 证书申请成功
- [ ] HTTPS 访问正常
- [ ] HTTP 自动跳转 HTTPS
- [ ] WebSocket 连接正常
- [ ] 端口收敛（仅 80/443 暴露）
- [ ] 限速规则生效
- [ ] 证书自动续期配置成功
