将这个基于 FastAPI + WebSocket + FunASR 的语音识别服务部署到公司的 Linux 服务器上，并且需要保证高可用和开机自启，业界标准的做法有两套：**Systemd 裸机部署** 或 **Docker 容器化部署**。

由于你需要明确“设置开机启动”，我将重点为你讲解最经典、最稳定的 **Systemd + Gunicorn/Uvicorn + Nginx 方案**，并在后面附带针对你这个实时语音项目的**避坑指南**。

---

### 第一步：服务器环境准备

1. **将代码上传到服务器**：推荐放在 `/opt/` 或你自己建的 `/var/www/` 等标准目录下（比如 `/opt/funasrdemo`）。
2. **创建 Python 虚拟环境并安装依赖**：
   ```bash
   cd /opt/funasrdemo
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
   *注意：生产环境强烈建议用 `gunicorn` 来管理 `uvicorn` 工作进程，所以需要额外安装：`pip install gunicorn`*

### 第二步：配置 Systemd 实现开机自启和进程守护

Linux 中的 `systemd` 是用来统管系统服务和开机启动的。我们需要为你的服务创建一个专用的 descriptor file。

1. **创建服务文件**：
   使用 `sudo vim /etc/systemd/system/funasr-proxy.service` 创建文件，并填入以下内容：

   ```ini
   [Unit]
   Description=FunASR FastAPI Proxy Service
   After=network.target

   [Service]
   # 强烈建议改成你服务器上的普通用户名，不要用 root 跑服务
   User=your_linux_username 
   Group=your_linux_usergroup
   
   # 项目根目录
   WorkingDirectory=/opt/funasrdemo
   # 让系统知道在哪里找 Python 环境
   Environment="PATH=/opt/funasrdemo/venv/bin"
   
   # 启动命令：使用 Gunicorn 配合 Uvicorn worker，这里启动 4 个 worker
   ExecStart=/opt/funasrdemo/venv/bin/gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8000
   
   # 意外崩溃后自动重启
   Restart=always
   RestartSec=3

   # 标准输出日志重定向交由 systemd 的 journalctl 接管
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```

2. **激活并启动服务**：
   ```bash
   # 重新加载 systemd 配置
   sudo systemctl daemon-reload
   
   # 设置为开机启动！(核心解答)
   sudo systemctl enable funasr-proxy.service
   
   # 立即启动服务
   sudo systemctl start funasr-proxy.service
   
   # 查看运行状态
   sudo systemctl status funasr-proxy.service
   ```
   *(此时你可以用 `journalctl -u funasr-proxy.service -f` 来实时查看你的 FastAPI 打印的日志)*

### 第三步：配置 Nginx 反向代理 (必须)

在企业级部署中，我们**绝不会**把 Uvicorn (8000端口) 直接暴露给公网。通常会加一层 Nginx，由 Nginx 处理 SSL 证书、限流和静态文件，然后再代理给后端的 8000 端口。

1. 安装 Nginx 并编辑配置文件 (`sudo vim /etc/nginx/conf.d/funasr.conf`):

   ```nginx
   server {
       listen 80;
       server_name your_domain_or_ip;

       # 代理普通的 HTTP API 请求
       location / {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_addrs;
       }

       # 【极其关键】代理 WebSocket 请求
       location /ws {
           proxy_pass http://127.0.0.1:8000;
           proxy_http_version 1.1;
           # 这两行告诉 Nginx 支持 WebSocket 协议升级
           proxy_set_header Upgrade $http_upgrade;
           proxy_set_header Connection "upgrade";
           proxy_set_header Host $host;
       }
   }
   ```
2. 重启 Nginx 并设置开机启动：
   ```bash
   sudo systemctl enable nginx
   sudo systemctl restart nginx
   ```

---

### ⚠️ 核心注意事项 (针对你的 WebSocket & 语音实时传输业务)

结合你这个项目（长连接、语音流式传输、依赖 FunASR），在 Linux 服务端部署时有几个致命点必须注意：

#### 1. Nginx 的 WebSocket 默认 60 秒会断开 (1011/1006 错误)
Nginx 默认的读写超时时间是 60 秒。如果你在开会，或者 60 秒内没有讲话（前端没发包或者没发心跳包），Nginx 会认为连接死了，从而主动掐断 WebSocket，这就导致前端报错断连。
**对策**：在 Nginx 的 `location /ws` 中增加超时时间配置：
```nginx
location /ws {
    # ... 前面的 upgrade 等配置 ...
    
    # 将读写超时设置大一点，比如 3600 秒 (1小时)
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

#### 2. Gunicorn Worker 数量与高并发
如果你使用 `gunicorn -w 4 ...`，意味着启动了 4 个进程。由于 WebSocket 是长连接，每个连接会一直占用一个 Worker 中的一个协程。普通的同步 Worker 会被直接堵死，因此**必须使用** `uvicorn.workers.UvicornWorker` 这类异步 Worker。Worker 的数量（`-w` 参数）推荐设置为 `(服务器 CPU 核心数 * 2) + 1`。

#### 3. 与 FunASR 引擎服务器的网络连通
你的 Python 服务作为代理，需要去连真正的 FunASR 引擎所在的 WebSocket。
* 如果 FunASR 也是部署在这台 Linux 服务器上（比如通过 Docker 起了 FunASR），那么你的 Python 代码里连接 FunASR 的地址应该是 `ws://127.0.0.1:10095`。
* 如果如果是另外一台显卡服务器跑的 FunASR，请确保这台 Linux 服务器能 `ping` 或 `telnet` 通那台机器的端口，并且注意**内网防火墙**的设置。

#### 4. 安全性：绝对不要用 Root 跑服务！
在 `systemd.service` 文件中，一定要用普通的 user 执行应用。因为如果你的服务存在漏洞，黑客黑进你的 Python 服务后，拿到的如果是 root 权限，整个机器就沦陷了。如果有读写音频/文件日志的需求，请给普通用户该文件夹的 `chown` 权限。

#### 5. 日志轮转 (Log Rotation)
你的服务可能会产生大量的调试日志和音频文件临时处理日志。由于我们交给了 `systemd`，所以不会自动撑爆硬盘。但如果是代码里自己写了文件日志（比如 `logging.FileHandler`），一定要用 `logging.handlers.TimedRotatingFileHandler` 按天切割日志，否则 Linux 磁盘迟早会被写满。
