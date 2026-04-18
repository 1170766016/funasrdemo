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
        self.active_connections[client_id] = client_ws
        logger.info(f"客户端 {client_id} 已连接，当前在线人数: {len(self.active_connections)}")
        
        # 初始化一个具备容量限制的异步队列用于缓存前端发来的音频流。
        # 设置 maxsize 可防止在后端拥塞或网络变差时无限缓存而引发内存溢出 (OOM)
        client_ws.audio_queue = asyncio.Queue(maxsize=50)
        
        # 启动后台代理任务处理与 FunASR 的通信
        funasr_ws_task = asyncio.create_task(self.funasr_proxy(client_ws, client_id))

        try:
            while True:
                # 接收来自前端浏览器的二进制音频数据流（按约定的 16kHz PCM）
                data = await client_ws.receive_bytes()
                if hasattr(client_ws, 'audio_queue'):
                    try:
                        client_ws.audio_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        logger.warning(f"客户端 {client_id} 的缓存队列已满，丢弃该部分音频包（可能 FunASR 处理遇到瓶颈）")
                
        except WebSocketDisconnect:
            logger.info(f"客户端 {client_id} 浏览器主动断开连接")
        except Exception as e:
            logger.error(f"处理客户端 {client_id} 音频输入时发生异常: {e}")
        finally:
            funasr_ws_task.cancel()
            if client_id in self.active_connections:
                del self.active_connections[client_id]
            logger.info(f"已清理客户端 {client_id} 的相关转录资源，当前在线人数: {len(self.active_connections)}")


    async def funasr_proxy(self, client_ws: WebSocket, client_id: int):
        """负责与内网 FunASR 服务端交互、重连与数据转发"""
        conn_attempt = 0
        while True:
            conn_attempt += 1
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
                    # 【极度重要】FunASR 官方服务端（特别是基于 C++/Python 混编的后端）通常不响应标准的 WebSocket Ping/Pong。
                    # 强行开启此功能会导致服务端过了 ping_timeout 时间后被判定为掉线从而强行断开连接（报 1011 internal error）。
                    # 因此，对于 FunASR，必须将这两个参数设为 None 来禁用标准心跳探测。
                    ping_interval=None,
                    ping_timeout=None,
                    # extra_headers=extra_headers  # <- 将上面定义的 headers 传进这里
                ) as funasr_ws:
                    logger.info(f"客户端 {client_id} 成功连接到 FunASR 服务 端点")
                    
                    # FunASR 模型版本常常需要在第一包发 json 格式的 init/config 参数
                    # 注意：每次重连使用不同的 wav_name，防止 FunASR 服务端保留上一次的死连接状态而拒绝接收
                    # 严格按照 Fun-ASR-Nano-2512-Docker 文档要求补全所有核心握手参数
                    init_msg = {
                        "mode": "2pass", 
                        "chunk_size": [5, 10, 5], 
                        "chunk_interval": 10,
                        "encoder_chunk_look_back": 4, 
                        "decoder_chunk_look_back": 1, 
                        "audio_fs": 16000, 
                        "wav_name": f"client_{client_id}_{conn_attempt}", 
                        "is_speaking": True,
                        "itn": True
                    }
                    await funasr_ws.send(json.dumps(init_msg))

                    # 清理积压的旧音频数据，避免重连后瞬间发送大量排队数据导致 FunASR 服务端再次崩溃
                    while not client_ws.audio_queue.empty():
                        client_ws.audio_queue.get_nowait()

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
                            try:
                                await client_ws.send_text(response)
                            except Exception as e:
                                logger.warning(f"下发给客户端 {client_id} 时发生错误（可能客户端已断开）: {e}")
                                raise

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())
                    
                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    for task in pending:
                        task.cancel()
                        
                    # 【增加异常屏蔽】等待这些 task 完全退出，防止后台抛出 "Task exception was never retrieved" 警告
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                        
                    # 【关键修复】必须处理所有已完成任务的异常，防止另一个任务抛出 "未检索到的异常"
                    # 因为遇到网络错误时，sender 和 receiver 会同时抛出 ConnectionClosedError 并被同时放入 done 列表。
                    # 如果只用无脑 for 循环，第一个 task.result() 抛出异常后中断循环，第二个 task 的异常就会泄漏。
                    first_exception = None
                    for task in done:
                        try:
                            task.result()
                        except Exception as e:
                            if first_exception is None:
                                first_exception = e
                                
                    if first_exception:
                        raise first_exception

            except asyncio.CancelledError:
                # 依靠 async with websocket 的天然回收机制，无需手动 funasr_ws.close()
                logger.debug(f"代理任务被主动取消 (客户端 {client_id})")
                raise
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
