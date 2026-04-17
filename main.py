import asyncio
import json
import logging
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import websockets

# 企业级日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)
logger = logging.getLogger("FunASRAgent")

app = FastAPI()

# 确保存放前端静态文件的目录存在
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# FunASR的WebSocket内网地址，可通过环境变量覆盖
FUNASR_WS_URL = os.getenv("FUNASR_WS_URL", "ws://127.0.0.1:10095")

@app.get("/")
async def get():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


class ConnectionManager:
    """管理前端 WebSocket 到 FunASR 的连接，包含断线重连和转发策略"""
    def __init__(self):
        self.active_connections = {}

    async def handle_client(self, client_ws: WebSocket):
        await client_ws.accept()
        client_id = id(client_ws)
        logger.info(f"客户端 {client_id} 已连接")
        
        # 初始化一个异步队列用于缓存前端发来的音频流。
        # 这里提前初始化是为了防止后续建立连接等带来的并发竞争漏洞：避免刚连上时浏览器发来首包数据，而后台还未就绪导致丢包。
        client_ws.audio_queue = asyncio.Queue()
        
        # 启动后台代理任务处理与 FunASR 的通信
        funasr_ws_task = asyncio.create_task(self.funasr_proxy(client_ws, client_id))

        try:
            while True:
                # 接收来自前端浏览器的二进制音频数据流（按约定的 16kHz PCM）
                data = await client_ws.receive_bytes()
                if hasattr(client_ws, 'audio_queue'):
                    await client_ws.audio_queue.put(data)
                
        except WebSocketDisconnect:
            logger.info(f"客户端 {client_id} 浏览器主动断开连接")
        except Exception as e:
            logger.error(f"处理客户端 {client_id} 音频输入时发生异常: {e}")
        finally:
            funasr_ws_task.cancel()
            logger.info(f"已清理客户端 {client_id} 的相关转录资源")


    async def funasr_proxy(self, client_ws: WebSocket, client_id: int):
        """负责与内网 FunASR 服务端交互、重连与数据转发"""
        
        while True:
            funasr_ws = None
            try:
                logger.info(f"尝试连接 FunASR 服务: {FUNASR_WS_URL}")
                # 连接到后端的纯异步调用
                # 在这里可以配置超时、心跳等参数以适应网络波动
                # open_timeout: 连接建立超时（秒）
                # ping_interval: 发送心跳探测的间隔时间（秒）
                # ping_timeout: 等待心跳响应的最高超时时间（秒）
                # 
                # 【关于身份验证】如果你们的企业 API 网关要求传递 Token 或其他验证信息，可以取消下面的注释并自行修改：
                # extra_headers = {
                #     "Authorization": "Bearer YOUR_COMPANY_TOKEN",
                #     "X-Client-ID": "xxx"
                # }
                async with websockets.connect(
                    FUNASR_WS_URL,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    # extra_headers=extra_headers  # <- 将上面定义的 headers 传进这里
                ) as funasr_ws:
                    logger.info(f"客户端 {client_id} 成功连接到 FunASR 服务 端点")
                    
                    # FunASR 模型版本常常需要在第一包发 json 格式的 init/config 参数
                    init_msg = {
                        "mode": "2pass", 
                        "chunk_size": [5, 10, 5], 
                        "wav_name": f"client_{client_id}", 
                        "is_speaking": True
                    }
                    await funasr_ws.send(json.dumps(init_msg))

                    # 协程 1：把前端发来的 PCM 推送给 FunASR
                    async def sender():
                        while True:
                            audio_data = await client_ws.audio_queue.get()
                            await funasr_ws.send(audio_data)

                    # 协程 2：接收 FunASR 的 JSON 结果发回给前端
                    async def receiver():
                        while True:
                            response = await funasr_ws.recv()
                            # 集中在控制台按照用户要求打印所有转录文本
                            try:
                                res_data = json.loads(response)
                                text = res_data.get('text', '')
                                is_final = res_data.get('is_final', False)
                                mode_text = "【最终】" if is_final else "【实时】"
                                if text.strip():
                                    if is_final:
                                        logger.info(f"转录完成 (客户端 {client_id}): {text}")
                                    else:
                                        logger.debug(f"{mode_text} {text}")
                            except json.JSONDecodeError:
                                logger.warning(f"未能解析 FunASR 返回结果 JSON: {response}")
                            
                            # 回传给网页前端
                            await client_ws.send_text(response)

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())
                    
                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    for task in pending:
                        task.cancel()

            except asyncio.CancelledError:
                if funasr_ws:
                    await funasr_ws.close()
                break
            except Exception as e:
                # 针对网络波动和服务断线处理：捕获异常，并使用 sleep 做退避重连机制
                logger.error(f"客户端 {client_id} 与 FunASR 的连接出现异常或中断: {e}，2 秒后自动进行重新连接...")
                await asyncio.sleep(2)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket 路由端点。
    当前端网页（也就是浏览器的 Javascript 代码）通过 ws://.../ws 发起 WebSocket 连接请求时，
    会触发这个方法。它将新建立的 websocket 连接对象直接交由 ConnectionManager（manager）集中处理。
    包含了接下来的接收前端音频、转发给后端算力并返回文本结果的整个生命周期。
    """
    await manager.handle_client(websocket)

if __name__ == "__main__":
    import uvicorn
    # 供测试直接运行 python main.py 的入口配置
    logger.info("启动 FastAPI 应用服务...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="warning")
