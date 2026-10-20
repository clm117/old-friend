# CI/CD 部署指南

## 一、代码仓库与分支策略（任务 45）

### Git 分支模型
```
main (生产)
  ↑
release/v1.0 (发布)
  ↑
develop (开发)
  ↑
feature/xxx (功能)
hotfix/xxx (紧急修复)
```

### 保护规则
- **main**: 禁止直接推送，需 PR + 2 人评审
- **release/**: 禁止直接推送，需 PR + 1 人评审
- **develop**: 需 PR 评审

### GitHub 设置
```yaml
# .github/branch-protection.yml
protection_rules:
  main:
    required_reviews: 2
    dismiss_stale_reviews: true
    require_code_owner_reviews: true
    required_status_checks:
      - "build-and-test"
      - "security-scan"
      
  release/*:
    required_reviews: 1
    dismiss_stale_reviews: true
```

## 二、构建流水线（任务 46）

### GitHub Actions 工作流
```yaml
# .github/workflows/build.yml
name: Build and Test

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          
      - name: Cache dependencies
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('**/requirements.txt') }}
          
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-cov
          
      - name: Run tests
        run: |
          pytest tests/ -v --cov=. --cov-report=xml
          
      - name: Upload coverage
        uses: codecov/codecov-action@v4
        
  security-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Run Trivy
        uses: aquasecurity/trivy-action@master
        with:
          scan-type: 'fs'
          scan-ref: '.'
          severity: 'CRITICAL,HIGH'
          
  docker-build:
    needs: [build, security-scan]
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3
        
      - name: Login to Registry
        uses: docker/login-action@v3
        with:
          registry: ${{ secrets.REGISTRY_URL }}
          username: ${{ secrets.REGISTRY_USERNAME }}
          password: ${{ secrets.REGISTRY_PASSWORD }}
          
      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ${{ secrets.REGISTRY_URL }}/yuban-gateway:latest
            ${{ secrets.REGISTRY_URL }}/yuban-gateway:${{ github.sha }}
          cache-from: type=registry,ref=${{ secrets.REGISTRY_URL }}/yuban-gateway:buildcache
          cache-to: type=registry,ref=${{ secrets.REGISTRY_URL }}/yuban-gateway:buildcache,mode=max
```

### 冒烟测试
```yaml
# .github/workflows/smoke-test.yml
name: Smoke Test

on:
  deployment_status:
    states: [success]

jobs:
  smoke-test:
    runs-on: ubuntu-latest
    steps:
      - name: Health check
        run: |
          curl -f https://api.your-domain.com/health || exit 1
          
      - name: Login test
        run: |
          TOKEN=$(curl -s -X POST https://api.your-domain.com/api/auth/login \
            -H "Content-Type: application/json" \
            -d '{"username":"test","password":"test"}' | jq -r .token)
          [ "$TOKEN" != "null" ] || exit 1
          
      - name: TTS test
        run: |
          curl -f -X POST https://api.your-domain.com/api/voice/tts \
            -H "Authorization: Bearer $TOKEN" \
            -F "session_id=smoke-test" \
            -F "text=冒烟测试" || exit 1
```

## 三、镜像仓库与版本标记（任务 47）

### 语义化版本
```
v1.0.0          # 正式版
v1.0.0-rc.1     # 预发布
v1.0.0-beta.1   # 测试版
latest          # 最新稳定
```

### 镜像标记策略
```bash
# 构建时标记
docker build -t registry.example.com/yuban-gateway:v1.0.0 .
docker tag registry.example.com/yuban-gateway:v1.0.0 registry.example.com/yuban-gateway:latest

# 推送
docker push registry.example.com/yuban-gateway:v1.0.0
docker push registry.example.com/yuban-gateway:latest
```

### 保留策略
```yaml
# 保留最近 10 个版本
retention:
  count: 10
  pattern: "v*"
  
# 保留 latest 和当前生产版本
exceptions:
  - "latest"
  - "production"
```

## 四、自动部署与回滚（任务 48）

### 部署脚本
```bash
#!/bin/bash
# deploy.sh

set -e

VERSION=$1
ENV=${2:-production}

echo "部署版本: $VERSION 到环境: $ENV"

# 拉取镜像
docker pull registry.example.com/yuban-gateway:$VERSION
docker pull registry.example.com/yuban-ai:$VERSION

# 滚动更新
docker-compose -f docker-compose.$ENV.yml up -d --no-deps gateway
sleep 10
docker-compose -f docker-compose.$ENV.yml up -d --no-deps ai-service

# 健康检查
for i in {1..30}; do
    if curl -f http://localhost:8005/health; then
        echo "部署成功"
        exit 0
    fi
    sleep 2
done

echo "部署失败，开始回滚"
rollback
```

### 回滚脚本
```bash
#!/bin/bash
# rollback.sh

PREVIOUS_VERSION=$(docker images --format "{{.Tag}}" | grep -E "^v[0-9]" | sort -V | tail -2 | head -1)

echo "回滚到版本: $PREVIOUS_VERSION"

docker-compose -f docker-compose.production.yml up -d --no-deps gateway:$PREVIOUS_VERSION
docker-compose -f docker-compose.production.yml up -d --no-deps ai-service:$PREVIOUS_VERSION
```

### 灰度发布
```yaml
# docker-compose.gray.yml
version: '3.8'

services:
  gateway:
    image: registry.example.com/yuban-gateway:${VERSION}
    deploy:
      replicas: 2
      update_config:
        parallelism: 1
        delay: 10s
        order: start-first
      rollback_config:
        parallelism: 1
        delay: 10s
```

## 五、配置与密钥管理（任务 49）

### 密钥管理方案

**方案 1：Docker Secrets**
```yaml
# docker-compose.yml
version: '3.8'

secrets:
  jwt_secret:
    external: true
  llm_api_key:
    external: true

services:
  gateway:
    secrets:
      - jwt_secret
      - llm_api_key
    environment:
      - JWT_SECRET_FILE=/run/secrets/jwt_secret
      - LLM_API_KEY_FILE=/run/secrets/llm_api_key
```

**方案 2：HashiCorp Vault**
```python
# vault_client.py
import hvac

client = hvac.Client(url='http://vault:8200')
client.token = os.environ['VAULT_TOKEN']

def get_secret(path: str) -> dict:
    return client.secrets.kv.v2.read_secret_version(path=path)['data']['data']

# 使用
secrets = get_secret('yuban/production')
jwt_secret = secrets['jwt_secret']
llm_api_key = secrets['llm_api_key']
```

**方案 3：云厂商 KMS**
```bash
# 阿里云 KMS
aliyun kms Encrypt --KeyId xxx --Plaintext "secret-value"

# 解密
aliyun kms Decrypt --CiphertextBlob "encrypted-value"
```

### 环境变量注入
```bash
# .env.production（由 CI/CD 生成，不入库）
JWT_SECRET=${JWT_SECRET}
LLM_API_KEY=${LLM_API_KEY}
REDIS_PASSWORD=${REDIS_PASSWORD}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
```

## 六、验证清单

- [ ] Git 分支保护规则配置完成
- [ ] CI 流水线（构建+测试+扫描）正常
- [ ] 镜像构建并推送成功
- [ ] 冒烟测试通过
- [ ] 自动部署脚本可执行
- [ ] 回滚脚本可执行
- [ ] 密钥管理方案实施
- [ ] 灰度发布流程验证
