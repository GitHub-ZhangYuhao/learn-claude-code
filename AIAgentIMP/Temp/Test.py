import random
import threading
import time
from queue import Queue

q = Queue()
def putmessage(msg: str):
    randValue = random.randint(1, 20)
    time.sleep(randValue)
    q.put(f"{msg}, 睡眠:{randValue}s")

threads = []
for i in range(10):
    t = threading.Thread(target=putmessage, args=(f"消息:__({i})__",))
    t.start()
    threads.append(t)

# for t in threads:
#     t.join()

def fake_agent_loop(query: list[str]):
    global is_Agent_Idle

    for _ in range(1):
        is_Agent_Idle = False
        time.sleep(10)
        print(f"Agent_Working: {'\n '.join(query) }")
    is_Agent_Idle = True

is_Agent_Idle = True

while True:
    # 等待消息传入队列，然后 子Agent 再继续。
    while q.empty() and is_Agent_Idle:
        time.sleep(1)

    msg = []
    while not q.empty():
        msg.append(q.get())
        print(f"获取消息: {msg[-1]}")

    fake_agent_loop(msg)




print(msg)

time.sleep(50)


