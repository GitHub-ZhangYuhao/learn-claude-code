import asyncio
import time

history = []
new_msg_sem = None  # 在 main 中创建


async def inject_result():
    """每 5 秒注入一次后台结果，不受用户输入阻塞"""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        history.append({"role": "assistant", "content": "注入结果 " + str(now)[:10]})
        new_msg_sem.release()  # 触发 LLM 处理


async def inject_query():
    """读取用户输入，不阻塞事件循环"""
    while True:
        query = await asyncio.to_thread(input, "用户输入：")
        history.append({"role": "user", "content": query})
        new_msg_sem.release()  # 触发 LLM 处理


async def fake_llm(query):
    print(f"\n模拟AI回复：已收到问题 {query}\n")


async def run_agent_loop():
    """等待新消息信号，逐个处理"""
    while True:
        await new_msg_sem.acquire()  # 阻塞直到有新消息
        if history and history[-1].get("content") == "q":
            break
        if history:
            await fake_llm(history[-1].get("content"))


async def run_all():
    global new_msg_sem
    new_msg_sem = asyncio.Semaphore(0)
    await asyncio.gather(
        inject_result(),
        inject_query(),
        run_agent_loop(),
    )


def main():
    asyncio.run(run_all())
    asyncio.to_thread()


if __name__ == "__main__":
    main()
