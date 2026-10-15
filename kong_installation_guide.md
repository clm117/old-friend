# Kong Gateway 安装指南

## 方法一：使用Docker容器（推荐）

### 步骤1：安装Docker Desktop
1. 下载Docker Desktop：https://www.docker.com/products/docker-desktop
2. 运行安装程序，按照提示完成安装
3. 安装完成后重启电脑
4. 启动Docker Desktop，等待其完全启动（托盘图标变为稳定状态）

### 步骤2：拉取Kong镜像
```powershell
# 打开PowerShell或命令提示符
docker pull kong
```

### 步骤3：运行Kong容器
```powershell
docker run -d --name kong -p 8000:8000 -p 8443:8443 kong
```

### 步骤4：验证Kong是否运行
```powershell
# 检查容器状态
docker ps

# 测试Kong API
curl http://localhost:8000

# 查看Kong管理API
curl http://localhost:8000/admin/api
```

## 方法二：下载Windows版本的Kong Gateway

### 步骤1：下载Windows版本
1. 访问Kong官网下载页面：https://download.konghq.com/gateway-3.x/windows/
2. 选择适合的Windows版本下载（通常是zip压缩包）

### 步骤2：安装和配置
1. 解压下载的zip文件到 `D:\ProgramFile\语伴\models\kong` 目录
2. 打开命令提示符，进入Kong目录
3. 配置环境变量（如果需要）
4. 运行Kong

### 步骤3：启动Kong
```powershell
# 进入Kong目录
cd D:\ProgramFile\语伴\models\kong

# 启动Kong
./kong start
```

### 步骤4：验证Kong是否运行
```powershell
# 测试Kong API
curl http://localhost:8000

# 查看Kong管理API
curl http://localhost:8000/admin/api
```

## 配置文件
无论使用哪种方法，您都可以使用 `D:\ProgramFile\语伴\api-gateway\kong.conf` 作为配置文件。

## 注意事项
- Docker方法更加简单和推荐，因为它已经预装了所有依赖
- Windows版本需要手动处理依赖和环境配置
- 确保端口8000和8443没有被其他服务占用
- 首次启动Kong时，可能需要一些时间来初始化