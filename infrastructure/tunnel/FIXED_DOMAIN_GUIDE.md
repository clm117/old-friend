# 语伴固定域名配置方案

> 目标：手机/客户通过一个**永不变化的 HTTPS 域名**访问语伴，替代每次重启都变的临时隧道地址。

## 三条路线对比

| | A. Cloudflare 命名隧道 ⭐推荐 | B. cpolar 固定子域名 | C. 备案域名+服务器（生产） |
|---|---|---|---|
| 固定地址 | ✅ 自有子域名 | ✅ xxx.cpolar.io | ✅ 自有域名 |
| 费用 | 域名 ~¥60/年，隧道 0 元 | ¥12+/月 | 域名+服务器+证书+备案 |
| 需要什么 | Cloudflare 免费账号+1个域名 | cpolar 账号 | 企业主体+ICP备案+SSL证书 |
| 国内速度 | 中等（Cloudflare 海外节点） | 快（国内节点） | 最快 |
| 适用阶段 | 现在的内测/客户演示 | 临时快速验证 | 正式商用 |

**建议**：现在用 A（成本最低、地址永久固定）；商用时切 C（备案后把同一域名的 DNS 指向正式服务器即可，域名不浪费）。

---

## 方案 A：Cloudflare 命名隧道（本次实施的方案）

### 准备（你来做，约 10 分钟 + NS 生效等待）

1. **买域名**：阿里云/腾讯云任何 `*.com/.cn/.top` 均可（¥10-70/年）
2. **注册 Cloudflare**：https://dash.cloudflare.com/sign-up （免费计划）
3. **托管域名**：Cloudflare 控制台 → Add a site → 输入域名 → Free 计划 → 得到 2 个 NS 地址（形如 `xxx.ns.cloudflare.com`）→ 回到注册商后台把 **DNS 服务器改为这 2 个 NS**
4. 等待生效：通常几分钟～2 小时（控制台显示 "Active" 即生效）

### 安装（脚本自动做，5 步）

域名生效后，在本项目 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File infrastructure\tunnel\setup_fixed_tunnel.ps1 -Domain yuban.你的域名.com
```

脚本自动完成：

| 步骤 | 内容 |
|---|---|
| 1 | 浏览器弹窗登录 Cloudflare 授权 |
| 2 | 创建命名隧道 `yuban`（得到固定 Tunnel ID + 凭证 json） |
| 3 | 生成 `C:\Users\<你>\.cloudflared\config.yml`（[模板](cloudflared-config.template.yml)） |
| 4 | 绑定 DNS：`yuban.你的域名.com → 隧道`（自动建 CNAME） |
| 5 | 注册 **Windows 服务 `cloudflared`**（开机自启，重启电脑地址不变） |

完成后固定地址：`https://yuban.你的域名.com`（自动 HTTPS，无需证书操作）。

### 链路（不变）

```
手机 → https://yuban.你的域名.com → Cloudflare 边缘
     → 本机 cloudflared 服务 → nginx:8080 → 网关:8005 / AI:8084 / WebSocket
```

### 日常运维

| 操作 | 命令 |
|---|---|
| 状态 | `Get-Service cloudflared` |
| 重启 | `Restart-Service cloudflared` |
| 停止 | `Stop-Service cloudflared` |
| 换子域名 | 重新跑脚本传新 `-Domain`，或改 `config.yml` 后重启服务 |
| 删隧道 | `cloudflared tunnel delete yuban` |
| 服务日志 | `C:\Windows\System32\config\systemprofile\.cloudflared\cloudflared.log` |

注意：cloudflared 服务只负责隧道；nginx/网关/AI 服务仍需用 [start_all.bat](../../start_all.bat) 启动（或后续注册为 Windows 服务）。

---

## 方案 B：cpolar（备选，国内速度快）

1. 注册 https://www.cpolar.cn → 控制台复制 **authtoken**
2. 免费版地址随机；**¥12/月起**才有固定二级子域名
3. 配置：
   ```powershell
   cpolar authtoken <你的token>
   cpolar http 8080 --subdomain=yuban --region=cn
   ```
   固定地址：`https://yban.cpolar.io`（以实际购买套餐为准）

---

## 方案 C：生产部署（商用，与之前评估衔接）

前置：企业主体、ICP 备案（2-4 周）、云服务器。

1. 域名备案后解析到服务器公网 IP（A 记录）
2. 证书：阿里云免费 DV 证书 或 Let's Encrypt（certbot 自动续期）
3. nginx 用已备好的 [yuban_prod.conf](../nginx/nginx-1.26.3/conf/yuban_prod.conf)：
   - HTTP→HTTPS 301 跳转、TLS 1.2/1.3、HSTS
   - WebSocket Upgrade 头（WSS 实时对话必需）
   - TTS/ASR 长超时（10 分钟，CPU 场景）
4. 后端（ai-service + 网关 + nginx）部署到 GPU 服务器，RTF≈0.2 实现实时语音

---

## 常见问题

**Q：没备案的域名走 Cloudflare 隧道合规吗？**
内测/演示/个人使用没有问题；对公众提供服务或商用，必须走方案 C（备案）。Cloudflare 隧道不是规避备案的合规手段，商用前请切换。

**Q：为什么手机访问 CF 隧道速度一般？**
Cloudflare 免费计划的边缘节点在海外，大陆访问延迟 100-300ms。内测可用；商用方案 C 的国内服务器没有这个问题。

**Q：域名 NS 托管到 Cloudflare 后影响国内解析吗？**
影响：所有解析都走 Cloudflare。若域名还有其他国内业务，可只在 CF 添加 `yuban` 子域名的 CNAME 记录指向 `<TunnelID>.cfargotunnel.com`（脚本已自动创建），主域名 NS 可不迁移。

**Q：换了 Wi-Fi / IP 变了地址会变吗？**
不会。命名隧道绑定的是域名和 Tunnel ID，与电脑的 IP、网络无关；电脑重启后 Windows 服务自动重连。
