import asyncio
import json
import logging
import math
import os
import struct
import time
from dataclasses import dataclass, field
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

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

FUNASR_WS_URL = os.getenv("FUNASR_WS_URL", "ws://192.168.16.105:10095")

# ==================== 静音检测 (VAD) 参数 ====================
SILENCE_RMS_THRESHOLD = 500
SILENCE_DURATION_SECONDS = 0.2

# 【修复问题2】静音期间向 FunASR 发送静音维持帧的间隔（秒）
# 防止 FunASR 服务端因长时间无数据而触发读超时并主动断开连接
SILENCE_KEEPALIVE_INTERVAL = 0.5

# 静音维持帧：160 个采样点 × 2 字节 = 320 字节，对应 16kHz 下 10ms 的全零 PCM
SILENCE_KEEPALIVE_FRAME = b'\x00' * 320
# ===============================================================


def calculate_rms(pcm_bytes: bytes) -> float:
    """计算 16-bit PCM 音频数据的 RMS（均方根）音量值"""
    if len(pcm_bytes) < 2:
        return 0.0
    sample_count = len(pcm_bytes) // 2
    samples = struct.unpack(f'<{sample_count}h', pcm_bytes[:sample_count * 2])
    if not samples:
        return 0.0
    sum_sq = sum(s * s for s in samples)
    return math.sqrt(sum_sq / len(samples))


@app.get("/")
async def get():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# 【修复问题6】将原本猴子补丁式附加到 WebSocket 对象上的状态，
# 统一封装到独立的数据类中，避免与 FastAPI 内部属性冲突，也便于调试和维护。
@dataclass
class ClientSession:
    client_id: int
    audio_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=200))
    last_drop_log_time: float = 0.0
    drop_count: int = 0


class ConnectionManager:
    """管理前端 WebSocket 到 FunASR 的连接，包含断线重连和转发策略"""

    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}

    async def handle_client(self, client_ws: WebSocket):
        await client_ws.accept()
        client_id = id(client_ws)
        self.active_connections[client_id] = client_ws
        logger.info(f"客户端 {client_id} 已连接，当前在线人数: {len(self.active_connections)}")

        # 【修复问题6】使用独立的 ClientSession 对象管理会话状态，不再污染 WebSocket 对象
        session = ClientSession(client_id=client_id)

        funasr_ws_task = asyncio.create_task(self.funasr_proxy(client_ws, session))
        try:
            while True:
                data = await client_ws.receive_bytes()

                if session.audio_queue.full():
                    # 丢弃最旧的音频而非最新的，保持语音流的连续性和时效性
                    try:
                        session.audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass

                    session.drop_count += 1
                    now = time.monotonic()
                    if now - session.last_drop_log_time > 5:
                        logger.warning(
                            f"客户端 {client_id} 缓存拥塞，最近 5 秒丢弃了 {session.drop_count} 个旧音频包（FunASR 处理滞后）"
                        )
                        session.drop_count = 0
                        session.last_drop_log_time = now

                # 【说明】get_nowait() 与 put_nowait() 之间无 await，asyncio 单线程模型下不会被打断，
                # 结合上方 try/except QueueEmpty 的防护，此处逻辑是安全的。
                session.audio_queue.put_nowait(data)

        except WebSocketDisconnect:
            logger.info(f"客户端 {client_id} 浏览器主动断开连接")
        except Exception as e:
            logger.error(f"处理客户端 {client_id} 音频输入时发生异常: {e}")
        finally:
            funasr_ws_task.cancel()
            # 等待代理任务完全退出，确保资源被彻底释放
            await asyncio.gather(funasr_ws_task, return_exceptions=True)
            if client_id in self.active_connections:
                del self.active_connections[client_id]
            logger.info(f"已清理客户端 {client_id} 的相关转录资源，当前在线人数: {len(self.active_connections)}")

    def _build_init_msg(self, client_id: int, conn_attempt: int, segment: int) -> dict:
        """构建 FunASR 握手初始化消息"""
        return {
            "mode": "2pass",
            "chunk_size": [5, 10, 5],
            "chunk_interval": 10,
            "encoder_chunk_look_back": 4,
            "decoder_chunk_look_back": 1,
            "audio_fs": 16000,
            "wav_name": f"client_{client_id}_{conn_attempt}_seg{segment}",
            "is_speaking": True,
            "itn": True,
            "hotword": ""
        }

    async def funasr_proxy(self, client_ws: WebSocket, session: ClientSession):
        """负责与内网 FunASR 服务端交互、重连与数据转发"""
        client_id = session.client_id
        conn_attempt = 0

        while True:
            conn_attempt += 1
            try:
                logger.info(f"尝试连接 FunASR 服务 (第 {conn_attempt} 次): {FUNASR_WS_URL}")

                async with websockets.connect(
                    FUNASR_WS_URL,
                    open_timeout=10,
                    # FunASR 服务端通常不响应标准 WebSocket Ping/Pong，必须禁用，
                    # 否则会因 ping_timeout 超时被判定掉线并强行断开（报 1011 internal error）
                    ping_interval=None,
                    ping_timeout=None,
                ) as funasr_ws:
                    logger.info(f"客户端 {client_id} 成功连接到 FunASR 服务端点")

                    # 段号使用列表包装，使嵌套函数可以通过闭包修改它（兼容 Python 3.9 及以下）
                    segment = [0]

                    # 发送首次握手初始化配置
                    init_msg = self._build_init_msg(client_id, conn_attempt, segment[0])
                    await funasr_ws.send(json.dumps(init_msg))

                    # 清理重连前积压的旧音频数据，避免瞬间发送大量排队数据导致 FunASR 再次崩溃
                    while not session.audio_queue.empty():
                        try:
                            session.audio_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                    # ==================== 协程 1：sender（含静音检测 VAD） ====================
                    # 【修复问题7】通过显式参数传入所有依赖，不再依赖隐式闭包捕获，
                    # 状态更清晰，调试更方便，也消除了潜在的内存泄漏风险。
                    async def sender(
                        _funasr_ws=funasr_ws,
                        _session=session,
                        _segment=segment,
                        _client_id=client_id,
                        _conn_attempt=conn_attempt,
                    ):
                        last_speech_time = time.monotonic()
                        is_speaking = True
                        # 【修复问题2】记录上次发送静音维持帧的时间戳
                        last_keepalive_time = time.monotonic()

                        while True:
                            # 【修复问题2】使用带超时的等待，确保静音期间能定期发送维持帧
                            try:
                                audio_data = await asyncio.wait_for(
                                    _session.audio_queue.get(),
                                    timeout=SILENCE_KEEPALIVE_INTERVAL
                                )
                                has_data = True
                            except asyncio.TimeoutError:
                                has_data = False
                                audio_data = None

                            now = time.monotonic()

                            if not has_data:
                                # 队列超时：无新音频数据
                                if not is_speaking:
                                    # 静音等待期间，定期向 FunASR 发送静音维持帧，防止服务端读超时断连
                                    if now - last_keepalive_time >= SILENCE_KEEPALIVE_INTERVAL:
                                        await _funasr_ws.send(SILENCE_KEEPALIVE_FRAME)
                                        last_keepalive_time = now
                                continue

                            # 贪婪地取出队列中所有已排队的数据，合并后批量处理
                            chunks = [audio_data]
                            while not _session.audio_queue.empty():
                                try:
                                    chunks.append(_session.audio_queue.get_nowait())
                                except asyncio.QueueEmpty:
                                    break
                            combined = b''.join(chunks)

                            # ---- 静音检测核心逻辑 ----
                            rms = calculate_rms(combined)

                            if rms > SILENCE_RMS_THRESHOLD:
                                # 检测到语音活动
                                last_speech_time = now
                                last_keepalive_time = now

                                if not is_speaking:
                                    # 【修复问题5】用户重新开始说话时，等待一小段时间再发新 init，
                                    # 给 FunASR 留出完成上一段离线结算的时间，避免两段数据混淆。
                                    await asyncio.sleep(0.1)
                                    _segment[0] += 1
                                    new_init = self._build_init_msg(_client_id, _conn_attempt, _segment[0])
                                    await _funasr_ws.send(json.dumps(new_init))
                                    is_speaking = True
                                    logger.info(f"客户端 {_client_id} 重新开始说话，启动第 {_segment[0]} 段识别")

                                await _funasr_ws.send(combined)

                            else:
                                # 当前批次是静音
                                if is_speaking:
                                    # 仍在说话状态 → 继续发送音频（可能是句中短暂停顿）
                                    await _funasr_ws.send(combined)

                                    silence_elapsed = now - last_speech_time
                                    if silence_elapsed >= SILENCE_DURATION_SECONDS:
                                        # 触发断句，发送 is_speaking:false 让 FunASR 启动离线精修
                                        logger.info(
                                            f"客户端 {_client_id} 静音 {silence_elapsed:.1f}s，"
                                            f"触发断句结算 (段 {_segment[0]})"
                                        )
                                        await _funasr_ws.send(json.dumps({"is_speaking": False}))
                                        is_speaking = False
                                        last_keepalive_time = now
                                # else: 已在静音等待状态，数据已取出直接丢弃，等待用户重新开口

                    # ==================== 协程 2：receiver ====================
                    async def receiver(
                        _funasr_ws=funasr_ws,
                        _client_ws=client_ws,
                        _client_id=client_id,
                    ):
                        while True:
                            response = await _funasr_ws.recv()
                            try:
                                res_data = json.loads(response)
                                text = res_data.get('text', '')
                                is_final = res_data.get('is_final', False)
                                mode = res_data.get('mode', '')

                                is_sentence_end = is_final or mode == "2pass-offline"
                                mode_text = "【最终】" if is_sentence_end else "【实时】"

                                if text.strip():
                                    if is_sentence_end:
                                        logger.info(f">>> {mode_text} (客户端 {_client_id}): {text}")
                                    else:
                                        logger.debug(f"{mode_text} {text}")
                            except json.JSONDecodeError:
                                logger.warning(f"未能解析 FunASR 返回结果 JSON: {response}")

                            # 【修复问题4】回传给前端失败时，先记录日志再 raise，
                            # 让 asyncio.wait 能感知到 receiver 退出并取消 sender，
                            # 避免 sender 在已断开的连接上继续无效发送。
                            try:
                                await _client_ws.send_text(response)
                            except Exception as e:
                                logger.warning(
                                    f"下发给客户端 {_client_id} 时发生错误（可能客户端已断开）: {e}"
                                )
                                raise

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())

                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    for task in pending:
                        task.cancel()

                    # 等待所有 pending 任务完全退出，防止后台抛出 "Task exception was never retrieved" 警告
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)

                    # 处理所有已完成任务的异常，防止异常泄漏
                    # 当网络错误时，sender 和 receiver 可能同时进入 done 集合，
                    # 必须遍历所有任务而不能在第一个异常处中断。
                    first_exception = None
                    for task in done:
                        try:
                            task.result()
                        except Exception as e:
                            if first_exception is None:
                                first_exception = e

                    if first_exception:
                        raise first_exception

            # 【修复问题3】必须在 except Exception 之前单独捕获 CancelledError 并重新抛出。
            # 在 Python 3.7 中 CancelledError 继承自 Exception，若被外层 except Exception 吞掉，
            # handle_client 的 funasr_ws_task.cancel() 信号将永远无法生效，导致任务泄漏。
            except asyncio.CancelledError:
                logger.debug(f"代理任务被主动取消 (客户端 {client_id})")
                raise

            except Exception as e:
                logger.error(
                    f"客户端 {client_id} 与 FunASR 的连接出现异常或中断: {e}，"
                    f"2 秒后自动进行重新连接..."
                )
                await asyncio.sleep(2)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket 路由端点。
    当前端网页通过 ws://.../ws 发起连接请求时触发，
    将连接交由 ConnectionManager 集中处理完整生命周期。
    """
    await manager.handle_client(websocket)


if __name__ == "__main__":
    import uvicorn
    logger.info("启动 FastAPI 应用服务...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="warning")
