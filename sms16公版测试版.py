import logging
import atexit
import time
import asyncio
import threading
import queue
from typing import Optional
import smpplib.client
import smpplib.consts
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# 配置
TELEGRAM_BOT_TOKEN = "机器人的token"
TELEGRAM_CHAT_ID = "你的telegram的聊天id"
SMPP_SERVER = "网关ip"
SMPP_PORT = 2775
SMPP_USERNAME = "smpp帐号"
SMPP_PASSWORD = "smpp密码"
SMPP_PHONE_NUMBER = "网关上发送短信的手机号码"

# 日志配置
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# 全局变量
client: Optional[smpplib.client.Client] = None
bot = Bot(token=TELEGRAM_BOT_TOKEN)
message_queue = queue.Queue()

def connect_smpp() -> smpplib.client.Client:
    """创建并连接SMPP客户端"""
    global client
    try:
        client = smpplib.client.Client(SMPP_SERVER, SMPP_PORT, timeout=30)
        client.connect()
        client.bind_transceiver(system_id=SMPP_USERNAME, password=SMPP_PASSWORD)
        client.set_message_received_handler(lambda pdu: handle_incoming_sms(pdu))
        threading.Thread(target=client.listen, daemon=True).start()
        logger.info("✅ 成功连接并绑定 SMPP 服务器")
        return client
    except Exception as e:
        logger.error(f"❌ SMPP 连接失败: {e}")
        raise

def smpp_keep_alive():
    """SMPP保活线程"""
    global client
    while True:
        try:
            if client and client.state in [smpplib.consts.SMPP_CLIENT_STATE_OPEN, 
                                         smpplib.consts.SMPP_CLIENT_STATE_BOUND_TRX]:
                client.send_pdu(smpplib.smpp.make_pdu('enquire_link', client=client))
                logger.debug("发送保活请求")
            else:
                logger.warning("SMPP连接断开，尝试重连...")
                if client:
                    client.unbind()
                    client.disconnect()
                client = connect_smpp()
        except Exception as e:
            logger.error(f"保活失败: {e}")
            time.sleep(5)
        time.sleep(30)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 /start 命令"""
    await update.message.reply_text('欢迎使用短信机器人！\n发送短信格式: 目标号码 短信内容')

def send_sms(phone_number: str, message: str) -> bool:
    """发送短信"""
    global client
    try:
        if not client or client.state not in [smpplib.consts.SMPP_CLIENT_STATE_BOUND_TRX]:
            logger.warning("SMPP未连接，尝试重连...")
            client = connect_smpp()

        message_bytes = message.encode('utf-16-be')
        client.send_message(
            source_addr_ton=smpplib.consts.SMPP_TON_INTL,
            source_addr=SMPP_PHONE_NUMBER,
            dest_addr_ton=smpplib.consts.SMPP_TON_INTL,
            destination_addr=phone_number,
            short_message=message_bytes,
            data_coding=8,
            esm_class=smpplib.consts.SMPP_MSGMODE_DEFAULT
        )
        logger.info(f"📤 已发送短信到 {phone_number}: {message}")
        return True
    except Exception as e:
        logger.error(f"❌ 短信发送失败: {e}")
        return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理Telegram消息"""
    user_message = update.message.text
    try:
        phone_number, message = user_message.split(' ', 1)
        success = send_sms(phone_number, message)
        if success:
            await update.message.reply_text(f'📨 已发送 "{message}" 到 {phone_number}')
        else:
            await update.message.reply_text('❌ 发送失败，请稍后重试')
    except ValueError:
        await update.message.reply_text('❌ 格式错误！请使用: 目标号码 短信内容')

def handle_incoming_sms(pdu) -> None:
    """处理接收到的短信"""
    try:
        if pdu.command == 'deliver_sm':
            phone_number = pdu.source_addr.decode('ascii', errors='ignore')
            raw_message = pdu.short_message
            data_coding = getattr(pdu, 'data_coding', 0)

            # 根据data_coding选择解码方式
            if data_coding == 0:  # GSM-7，使用latin-1作为替代
                message_content = raw_message.decode('latin-1', errors='ignore')
            elif data_coding == 8:  # UCS-2
                message_content = raw_message.decode('utf-16-be', errors='ignore')
            else:  # 其他情况使用latin-1
                message_content = raw_message.decode('latin-1', errors='ignore')

            logger.info(f"📩 收到短信: {phone_number}: {message_content} (data_coding={data_coding}, raw={raw_message.hex()})")
            message_queue.put((phone_number, message_content))
    except Exception as e:
        logger.error(f"❌ 处理接收短信失败: {e} (phone={phone_number}, raw={raw_message.hex()})")

async def process_incoming_messages():
    """从队列中处理接收到的短信并发送到Telegram"""
    while True:
        try:
            phone_number, message_content = message_queue.get(timeout=1)
            text = f"📩 短信来自 {phone_number}: {message_content}"
            logger.debug(f"发送到Telegram: {text}")
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text
            )
            message_queue.task_done()
        except queue.Empty:
            await asyncio.sleep(0.1)

def cleanup():
    """程序退出时清理"""
    global client
    if client:
        try:
            client.unbind()
            client.disconnect()
            logger.info("已断开SMPP连接")
        except Exception as e:
            logger.error(f"清理SMPP连接失败: {e}")

def main() -> None:
    """主函数"""
    global client
    atexit.register(cleanup)

    # 连接SMPP
    client = connect_smpp()

    # 启动SMPP保活线程
    keep_alive_thread = threading.Thread(target=smpp_keep_alive, daemon=True)
    keep_alive_thread.start()

    # 初始化Telegram应用
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 获取事件循环并启动消息处理任务
    loop = asyncio.get_event_loop()
    loop.create_task(process_incoming_messages())

    # 启动Telegram机器人
    logger.info("🚀 机器人已启动，监听消息中...")
    application.run_polling()

if __name__ == '__main__':
    main()
